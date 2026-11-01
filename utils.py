import datetime as dt
import re
import time
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import discord
from discord import app_commands

from config import COMPANIES, CURRENCY, STAFF_ROLE_IDS, TIMEZONE, Company, get_company

# Digital products the invoice form understands (type=... on an item line).
PRODUCT_TYPES = ("VPS", "Game Panel", "Bot", "Plugin", "Fork", "Web Hosting", "Other")
PRODUCT_ALIASES = {
    "vps": "VPS", "virtual server": "VPS", "vm": "VPS", "vds": "VPS",
    "panel": "Game Panel", "game panel": "Game Panel", "pterodactyl": "Game Panel",
    "bot": "Bot", "bot file": "Bot", "discord bot": "Bot",
    "plugin": "Plugin", "plugins": "Plugin", "addon": "Plugin",
    "fork": "Fork", "forks": "Fork", "forked": "Fork",
    "hosting": "Web Hosting", "web hosting": "Web Hosting", "website": "Web Hosting",
    "other": "Other", "custom": "Other",
}

TICKET_KINDS = ("purchase", "enquiry", "support")
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")

# Sales stages, in order, with the win probability used for the forecast.
DEAL_STAGES = ("new", "contacted", "quoted", "negotiation", "won", "lost")
STAGE_META = {
    "new": ("🆕 New enquiry", 0.10),
    "contacted": ("📞 Contacted", 0.25),
    "quoted": ("🧾 Quoted", 0.50),
    "negotiation": ("🤝 Negotiation", 0.75),
    "won": ("✅ Won", 1.00),
    "lost": ("❌ Lost", 0.00),
}
STAGE_ALIASES = {
    "new": "new", "open": "new", "enquiry": "new", "lead": "new",
    "contacted": "contacted", "contact": "contacted", "called": "contacted",
    "quoted": "quoted", "quote": "quoted", "invoiced": "quoted",
    "negotiation": "negotiation", "negotiating": "negotiation", "followup": "negotiation",
    "won": "won", "paid": "won", "closed-won": "won", "sold": "won",
    "lost": "lost", "closed-lost": "lost", "cancelled": "lost",
}


# ---------- money (stored as integer cents everywhere) ----------
def to_cents(value) -> int:
    try:
        d = Decimal(str(value).replace(",", "").strip())
        if not d.is_finite() or abs(d) > 10_000_000_000:
            raise InvalidOperation
        return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise ValueError(f"Invalid amount: {value!r}")


SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "AED ", "AUD": "A$", "CAD": "C$"}


def _group(whole: int, cur: str) -> str:
    s = str(whole)
    if cur != "INR" or len(s) <= 3:
        return f"{whole:,}" if cur != "INR" else s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt(cents: int, cur: str = CURRENCY) -> str:
    """₹500.00, ₹1,250.50, $12.50 ... (always two decimals; Indian grouping for INR)."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    num = _group(whole, cur) + f".{frac:02d}"
    return f"{sign}{SYMBOLS.get(cur, cur + ' ')}{num}"


# Recognised key=value tokens inside the 4th (details) column of an item line.
_ITEM_KEYS = {
    "type": "product_type", "product": "product_type", "product_type": "product_type",
    "service_type": "product_type", "kind": "product_type",
    "server": "service_name", "server_name": "service_name", "service": "service_name",
    "node": "node", "location": "node",
    "plan": "plan", "ram": "plan",
    "id": "server_id", "server_id": "server_id", "sid": "server_id",
    "link": "delivery_link", "panel": "delivery_link", "delivery": "delivery_link",
    "url": "delivery_link", "access": "delivery_link",
}


def normalize_product(value: str) -> str:
    """'vps' -> VPS, 'bot file' -> Bot, anything else is title-cased (max 40 chars)."""
    raw = (value or "").strip()
    if not raw:
        return "Other"
    return PRODUCT_ALIASES.get(raw.lower(), raw[:40].title())


def normalize_stage(value: str) -> str:
    stage = STAGE_ALIASES.get((value or "").strip().lower())
    if not stage:
        raise ValueError("Stage must be one of: " + ", ".join(DEAL_STAGES))
    return stage


def parse_items(text: str) -> list[dict]:
    """Each line: `name | qty | unit price | type=VPS; node=Ryzen; plan=4GB; link=https://...`.

    The 4th column is optional. `key=value` tokens fill the digital-service fields; any
    other token becomes a bullet point on the invoice/PDF.
    """
    items = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|", 3)]
        if len(parts) < 3 or not parts[0]:
            raise ValueError(f"Line {n}: use `name | quantity | unit price | type=VPS; plan=4GB`")
        desc, qty_s, price_s = parts[:3]
        product_type = ""
        service_name = node = plan = server_id = delivery_link = ""
        details = []
        tokens = [s.strip() for s in (parts[3] if len(parts) == 4 else "").split(";") if s.strip()]
        for token in tokens:
            if "=" in token:
                key, value = token.split("=", 1)
                field = _ITEM_KEYS.get(key.strip().lower())
                if field and value.strip():
                    value = value.strip()[:300]
                    if field == "product_type":
                        product_type = normalize_product(value)
                    elif field == "service_name":
                        service_name = value
                    elif field == "node":
                        node = value
                    elif field == "plan":
                        plan = value
                    elif field == "server_id":
                        server_id = value
                    elif field == "delivery_link":
                        delivery_link = value
                    continue
            details.append(token[:120])
        try:
            qty = Decimal(qty_s)
        except InvalidOperation:
            raise ValueError(f"Line {n}: invalid quantity {qty_s!r}")
        if not qty.is_finite() or qty > 1_000_000:
            raise ValueError(f"Line {n}: invalid quantity {qty_s!r}")
        if qty <= 0:
            raise ValueError(f"Line {n}: quantity must be above zero")
        price = to_cents(price_s)
        if price < 0:
            raise ValueError(f"Line {n}: price cannot be negative")
        items.append({
            "description": desc[:200],
            "quantity": float(qty),
            "unit_price": price,
            "details": "\n".join(details[:15]) or None,
            "product_type": product_type or None,
            "service_name": service_name or None,
            "node": node or None,
            "plan": plan or None,
            "server_id": server_id or None,
            "delivery_link": delivery_link or None,
        })
    if not items:
        raise ValueError("Add at least one item.")
    if len(items) > 50:
        raise ValueError("Maximum 50 items per invoice.")
    return items


# ---------- dates (UTC) ----------
def _tz():
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:  # unknown zone or tzdata missing in the container
        from config import UTC_OFFSET_HOURS

        return dt.timezone(dt.timedelta(hours=UTC_OFFSET_HOURS))


def today() -> dt.date:
    return dt.datetime.now(_tz()).date()


def current_month() -> str:
    return today().strftime("%Y-%m")


def valid_month(value: str | None) -> str:
    if not value:
        return current_month()
    try:
        return dt.datetime.strptime(value.strip(), "%Y-%m").strftime("%Y-%m")
    except ValueError:
        raise ValueError("Month must look like `2026-10`.")


def parse_date(value: str | None) -> str:
    if not value:
        return today().isoformat()
    try:
        return dt.datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError("Date must look like `2026-10-17`.")


def parse_when(value: str | None) -> str:
    """Follow-up date: `2026-10-24`, `today`, `tomorrow`, `+3d` or `3 days`."""
    if not value or not value.strip():
        return today().isoformat()
    raw = value.strip().lower()
    if raw in ("today", "now"):
        return today().isoformat()
    if raw == "tomorrow":
        return (today() + dt.timedelta(days=1)).isoformat()
    match = re.fullmatch(r"\+?(\d{1,3})\s*(?:d|day|days)", raw)
    if match:
        return (today() + dt.timedelta(days=int(match.group(1)))).isoformat()
    return parse_date(raw)


def dmy(value: str | None) -> str:
    """2026-10-18 -> 18-10-26 (the date style used in the finance channel)."""
    if not value:
        return "—"
    try:
        return dt.date.fromisoformat(value).strftime("%d-%m-%y")
    except ValueError:
        return str(value)[:20]


def now_iso() -> str:
    return dt.datetime.now(_tz()).replace(microsecond=0).isoformat(sep=" ")


def days_since(value: str | None) -> int | None:
    if not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value).date()
    except ValueError:
        return None
    return (today() - stamp).days


def safe_channel_name(text: str, limit: int = 80) -> str:
    """Discord channel names: lowercase, dashes, no decoration."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or "ticket")[:limit].strip("-")


def shift_month(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


# ---------- permissions ----------
def is_staff(user) -> bool:
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.administrator or any(
        r.id in STAFF_ROLE_IDS for r in user.roles
    )


def is_ticket_staff(user, guild=None) -> bool:
    """Ticket staff: this server's configured ticket / department roles, or admins.

    Pass ``guild`` for ticket workflows so the check uses that server's own
    roles (``ticket_guilds.json``). Without it the flat ``TICKET_STAFF_ROLE_IDS``
    (falling back to ``STAFF_ROLE_IDS``) applies, which is the single-server
    behaviour used by the older commands.
    """
    from config import TICKET_STAFF_ROLE_IDS, tickets_for

    if not isinstance(user, discord.Member):
        return False
    if guild is not None:
        return tickets_for(guild).is_ticket_staff(user)
    if user.guild_permissions.administrator or user.guild_permissions.manage_channels:
        return True
    return any(r.id in STAFF_ROLE_IDS or r.id in TICKET_STAFF_ROLE_IDS for r in user.roles)


# ---------- companies ----------
def company_choices() -> list[app_commands.Choice]:
    """The configured companies as Discord choices (key as the value, display name as the label)."""
    return [app_commands.Choice(name=c.name, value=c.key) for c in COMPANIES.values()]


def require_company(value) -> Company:
    """Company from a key or name; ValueError (for the user) when it is missing or unknown."""
    company = get_company(value)
    if company is None:
        names = ", ".join(c.name for c in COMPANIES.values())
        raise ValueError(f"Company must be one of: {names}.")
    return company


_recent_submissions: dict[tuple, float] = {}


def duplicate_submission(key: tuple, window: float = 20.0) -> bool:
    """True when the same staff member just submitted the same figures.

    A cheap in-memory guard against double-clicks and repeated modal submits;
    it never blocks a legitimate second entry after ``window`` seconds.
    """
    now = time.monotonic()
    for old_key, stamp in list(_recent_submissions.items()):
        if now - stamp > 120:
            _recent_submissions.pop(old_key, None)
    last = _recent_submissions.get(key)
    _recent_submissions[key] = now
    return last is not None and (now - last) < window


async def staff_interaction_check(interaction: discord.Interaction) -> bool:
    if is_staff(interaction.user):
        return True
    import ui  # local import: ui is the shared Components V2 helper module

    await ui.say(interaction, "🚫 You need a staff role to use this.")
    return False


# ---------- amount in words ----------
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_100(n: int) -> str:
    return _ONES[n] if n < 20 else _TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")


def _below_1000(n: int) -> str:
    h, r = divmod(n, 100)
    return " ".join(p for p in ((_ONES[h] + " Hundred") if h else "", _below_100(r) if r else "") if p)


def _int_words(n: int, indian: bool) -> str:
    if n == 0:
        return "Zero"
    parts = []
    scales = ((10**7, "Crore"), (10**5, "Lakh"), (10**3, "Thousand")) if indian else \
             ((10**9, "Billion"), (10**6, "Million"), (10**3, "Thousand"))
    for div, name in scales:
        q, n = divmod(n, div)
        if q:
            parts.append((_int_words(q, True) if (indian and div == 10**7) else (_below_100(q) if indian else _below_1000(q))) + " " + name)
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def amount_in_words(cents: int, cur: str = CURRENCY) -> str:
    names = {"INR": ("Rupees", "Paise"), "USD": ("Dollars", "Cents"), "EUR": ("Euros", "Cents"),
             "GBP": ("Pounds", "Pence"), "AUD": ("Dollars", "Cents"), "CAD": ("Dollars", "Cents")}
    major_name, minor_name = names.get(cur, (cur, "Cents"))
    major, minor = divmod(abs(cents), 100)
    if major == 0 and minor:
        return f"{_int_words(minor, False)} {minor_name} Only"
    text = f"{major_name} {_int_words(major, cur == 'INR')}"
    if minor:
        text += f" and {_int_words(minor, False)} {minor_name}"
    return text + " Only"
