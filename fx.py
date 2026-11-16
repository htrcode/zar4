"""Live USD/INR: add the USD equivalent next to INR prices, and keep the exchange rate fresh.

Two independent pieces:

* :func:`convert_text` rewrites the *text* of a pricing card. The INR amounts are never changed or
  recalculated; the USD figure is ``INR / rate`` (2 decimals, half-up) and is always derived from the
  original INR-only text, so converting twice (or at a new rate) can never stack USD amounts.
* :class:`RateKeeper` fetches ``USD -> INR`` with a lightweight aiohttp request, validates it, and keeps
  the last good rate in memory *and* in the database so a restart or an API outage never loses it.

The free feeds only publish one rate a day, so the keeper asks the network only when a new rate can
exist (the feed's own next-update time, else once per refresh interval) and backs off on HTTP 429.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Callable, Optional

import aiohttp

import config

log = logging.getLogger("invoice-bot.fx")

PAIR = "USD/INR"
DEFAULT_URLS = (
    # ECB reference rates, no key. (v1 is marked deprecated by its author but still served.)
    "https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR",
    # ExchangeRate-API open endpoint, no key, once a day. Its terms ask for the credit
    # "Rates By Exchange Rate API" (exchangerate-api.com), which /price status shows.
    "https://open.er-api.com/v6/latest/USD",
)
MIN_RATE, MAX_RATE = Decimal("20"), Decimal("500")  # INR per USD: anything outside is a feed error
MAX_JUMP = Decimal("0.25")                          # a >25% move between two checks is not believable
REQUEST_TIMEOUT = 10                                # seconds
FORCED_COOLDOWN = 300                               # a manual refresh at most every 5 minutes
RATE_LIMIT_BACKOFF = 20 * 60 + 60                   # open.er-api.com blocks a throttled IP for 20 minutes
MAX_WAIT = 12 * 3600                                # never trust a "next update" hint for longer than this
TEXT_LIMIT = 3950                                   # Discord: ~4000 characters of text per Components V2 message


def refresh_minutes() -> int:
    """How often the background task wakes up: FX_REFRESH_MINUTES, kept within 30-60."""
    return min(60, max(30, int(config.FX_REFRESH_MINUTES)))


# ---------------------------------------------------------------------------
# text conversion
# ---------------------------------------------------------------------------
_NUMBER = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT = r"month(?:ly)?|mo|year(?:ly)?|yr|annum|week(?:ly)?|day|hour"
PRICE = re.compile(
    rf"(?P<price>(?:₹|\bRs\.?|\bINR)\s*(?P<number>{_NUMBER}))"
    rf"(?:\s*(?:/|\bper\b)\s*(?P<unit>{_UNIT})\b)?",
    re.IGNORECASE,
)
_UNIT_NAME = {"mo": "month", "monthly": "month", "yr": "year", "yearly": "year", "annum": "year",
              "weekly": "week"}


def valid_rate(rate) -> bool:
    return isinstance(rate, Decimal) and rate.is_finite() and MIN_RATE <= rate <= MAX_RATE


def usd_amount(inr: Decimal, rate: Decimal) -> Decimal:
    """``USD = INR / USD_INR_RATE`` rounded half-up to 2 decimals. Refuses a zero / invalid rate."""
    if not valid_rate(rate):
        raise ValueError("invalid exchange rate")
    return (inr / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def has_inr_price(text: str) -> bool:
    return bool(PRICE.search(text or ""))


def convert_text(text: str, rate: Optional[Decimal]) -> str:
    """Append the USD price to every INR price in ``text``; the INR part is left exactly as written.

    ``₹999 / month`` -> ``₹999 / $10.64 per month`` and ``₹999`` -> ``₹999 / $10.64``.
    Prices that already have a USD amount right after them are left alone.
    Without a valid rate the text is returned unchanged (never a made-up number).
    """
    if not text or not valid_rate(rate):
        return text or ""

    def one(match: re.Match) -> str:
        if re.match(r"\s*/\s*\$", text[match.end():]):  # someone already wrote the USD price by hand
            return match.group(0)
        try:
            usd = usd_amount(Decimal(match.group("number").replace(",", "")), rate)
        except (InvalidOperation, ValueError):
            return match.group(0)
        unit = (match.group("unit") or "").lower()
        suffix = f" per {_UNIT_NAME.get(unit, unit)}" if unit else ""
        return f"{match.group('price')} / ${usd:,.2f}{suffix}"

    return PRICE.sub(one, text)


def card_text_length(title: str, body: str, footer: str = "") -> int:
    """Characters a card uses in Discord's per-message text budget (brand tagline included)."""
    import ui  # local: ui pulls in the whole bot's rendering helpers

    return len(title) + len(body) + len(footer or "") + len(ui.BRAND_TAGLINE or "") + 12


def card_fingerprint(title: str, body: str, footer: str) -> str:
    """Identifies what a card currently shows, so an unchanged card is never edited."""
    return hashlib.sha1(json.dumps([title, body, footer or ""]).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# rate feeds
# ---------------------------------------------------------------------------
def parse_payload(data) -> tuple[Optional[Decimal], Optional[float]]:
    """(rate, next_update_unix) from a feed's JSON, or (None, None) when it has no usable USD->INR rate.

    Understands ``{"rates": {"INR": 93.8}}`` (Frankfurter, ExchangeRate-API) and ``{"usd": {"inr": 93.8}}``.
    """
    if not isinstance(data, dict):
        return None, None
    if data.get("result") not in (None, "success"):  # ExchangeRate-API reports failures in the body
        return None, None
    rates = data.get("rates")
    raw = rates.get("INR") if isinstance(rates, dict) else None
    if raw is None and isinstance(data.get("usd"), dict):
        raw = data["usd"].get("inr")
    try:
        rate = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None, None
    if not valid_rate(rate):
        return None, None
    hint = data.get("time_next_update_unix")
    return rate, float(hint) if isinstance(hint, (int, float)) and hint > 0 else None


class RateLimited(Exception):
    def __init__(self, retry_after: float = RATE_LIMIT_BACKOFF):
        super().__init__("rate limited")
        self.retry_after = retry_after


class RateKeeper:
    """Holds the latest good USD->INR rate and refreshes it politely.

    ``refresh()`` never raises: a failure keeps the previous rate. ``rate`` is None only until the first
    successful fetch on a brand-new install, so a zero / invalid rate can never reach a price.
    """

    def __init__(self, db, urls=None, *, interval_minutes: Optional[int] = None,
                 clock: Callable[[], float] = time.time, timeout: float = REQUEST_TIMEOUT) -> None:
        self.db = db
        self.urls = tuple(urls or config.FX_API_URLS or DEFAULT_URLS)
        self.interval = (interval_minutes or refresh_minutes()) * 60
        self.clock, self.timeout = clock, timeout
        self.rate: Optional[Decimal] = None
        self.source = ""
        self.updated_at: Optional[str] = None  # when the stored rate was fetched (UTC text from the database)
        self.next_check = 0.0
        self.last_attempt = 0.0

    async def load(self) -> None:
        """Start from the rate saved by the previous run, so prices work even if the API is down."""
        row = await self.db.fx_get(PAIR)
        if not row:
            return
        try:
            rate = Decimal(row["rate"])
        except (InvalidOperation, ValueError, TypeError):
            return
        if valid_rate(rate):
            self.rate, self.source, self.updated_at = rate, row.get("source") or "", row.get("fetched_at")

    def _acceptable(self, rate: Decimal) -> bool:
        if self.rate is not None and abs(rate / self.rate - 1) > MAX_JUMP:
            log.warning("Ignoring USD/INR %s: more than %d%% away from the current %s", rate,
                        MAX_JUMP * 100, self.rate)
            return False
        return True

    async def _fetch(self, session: aiohttp.ClientSession, url: str):
        async with session.get(url, headers={"Accept": "application/json"}) as response:
            if response.status == 429:
                try:
                    wait = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    wait = RATE_LIMIT_BACKOFF
                raise RateLimited(max(wait, RATE_LIMIT_BACKOFF))
            response.raise_for_status()
            return await response.json(content_type=None)

    async def refresh(self, force: bool = False) -> str:
        """One check. Returns ``updated`` (new rate), ``unchanged``, ``skipped`` (no new rate can exist yet),
        ``cooldown`` (manual refresh too soon) or ``failed`` (previous rate kept)."""
        now = self.clock()
        if force:
            if now - self.last_attempt < FORCED_COOLDOWN:
                return "cooldown"
        elif now < self.next_check:  # also while there is no rate yet: never hammer a failing or throttling feed
            return "skipped"
        self.last_attempt = now
        backoff = None
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for url in self.urls:
                    try:
                        rate, hint = parse_payload(await self._fetch(session, url))
                    except RateLimited as limited:
                        backoff = limited.retry_after
                        log.warning("Exchange-rate feed %s is rate limiting us; backing off", url)
                        continue
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                        log.warning("Exchange-rate feed %s failed: %s", url, exc)
                        continue
                    if rate is None or not self._acceptable(rate):
                        log.warning("Exchange-rate feed %s returned no usable rate", url)
                        continue
                    return await self._accept(rate, hint, url, now)
        except Exception:  # never let the background task die
            log.exception("Unexpected error while refreshing the exchange rate")
        self.next_check = now + (backoff if backoff else self.interval)
        return "failed"

    async def _accept(self, rate: Decimal, hint: Optional[float], url: str, now: float) -> str:
        changed = rate != self.rate
        self.rate, self.source = rate, url
        # Next check: when the feed says a new rate is due (never later than MAX_WAIT), else after one interval.
        self.next_check = min(hint + 60, now + MAX_WAIT) if hint and hint > now else now + self.interval
        try:
            await self.db.fx_set(PAIR, str(rate), url)
            row = await self.db.fx_get(PAIR)
            self.updated_at = row["fetched_at"] if row else None
        except Exception:
            log.exception("Could not save the exchange rate (it is still used from memory)")
        return "updated" if changed else "unchanged"
