"""Live USD prices: conversion, exchange-rate keeper (real HTTP against a local server), card updates."""
import os as _os
import tempfile as _tempfile

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_os.environ.setdefault("INVOICE_BOT_DATA", _os.path.join(_tempfile.mkdtemp(prefix="zarbot-ledger-"), "data.json"))

import asyncio  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
import warnings  # noqa: E402
from decimal import Decimal  # noqa: E402
from unittest import mock  # noqa: E402

import discord  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

import config  # noqa: E402
import ui  # noqa: E402
from cogs.embed import EmbedModal, announcement_card  # noqa: E402
from cogs.pricing import Pricing, card_parts  # noqa: E402
from desk import fx  # noqa: E402
from desk.database import Database  # noqa: E402
from desk.fx import RateKeeper, convert_text, parse_payload, refresh_minutes, usd_amount  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)
R = Decimal("93.87")


def text_of(view):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == 10:
                out.append(node["content"])
            for child in node.get("components", []) or []:
                walk(child)
    for top in view.to_components():
        walk(top)
    return "\n".join(out)


def http_error(cls, status=500):
    return cls(types.SimpleNamespace(status=status, reason="x"), "boom")


# ---------------------------------------------------------------------------
class ConversionTests(unittest.TestCase):
    def test_the_requested_formats(self):
        self.assertEqual(convert_text("₹999 / month", R), "₹999 / $10.64 per month")
        self.assertEqual(convert_text("₹999", R), "₹999 / $10.64")
        self.assertEqual(convert_text("₹1,499/mo", R), "₹1,499 / $15.97 per month")
        self.assertEqual(convert_text("Rs. 500 per year", R), "Rs. 500 / $5.33 per year")
        self.assertEqual(convert_text("INR 2,50,000", R), "INR 2,50,000 / $2,663.26")
        self.assertEqual(convert_text("₹99.50 a day", R), "₹99.50 / $1.06 a day")

    def test_usd_is_inr_divided_by_the_rate_with_two_decimals_half_up(self):
        self.assertEqual(usd_amount(Decimal(999), R), Decimal("10.64"))
        self.assertEqual(usd_amount(Decimal("1.005"), Decimal(100)), Decimal("0.01"))
        self.assertEqual(usd_amount(Decimal("1.5"), Decimal(100)), Decimal("0.02"))   # half-up, not banker's
        self.assertEqual(str(usd_amount(Decimal(100), Decimal(50))), "2.00")

    def test_inr_prices_are_never_changed(self):
        text = "Starter ₹999 / month · Pro ₹1,499/mo · Rs. 500 per year · INR 2,50,000 · ₹99.50"
        out = convert_text(text, R)
        for price in ("₹999", "₹1,499", "Rs. 500", "INR 2,50,000", "₹99.50"):
            self.assertIn(price, out)
        before = [m.group("price") for m in fx.PRICE.finditer(text)]
        after = [m.group("price") for m in fx.PRICE.finditer(out)]
        self.assertEqual(before, after)

    def test_every_price_gets_its_own_usd_figure(self):
        self.assertEqual(convert_text("₹499 - ₹1,999", R), "₹499 / $5.32 - ₹1,999 / $21.30")
        self.assertEqual(convert_text("from ₹499\nup to ₹999 / month", R),
                         "from ₹499 / $5.32\nup to ₹999 / $10.64 per month")

    def test_text_without_inr_prices_is_untouched(self):
        for text in ("4GB RAM · 2 vCPU", "$5 only", "₹ only", "Rs", "call 9999999999", "", "INRIA 5"):
            self.assertEqual(convert_text(text, R), text)

    def test_converting_again_never_stacks_usd(self):
        once = convert_text("₹999 / month", R)
        self.assertEqual(convert_text(once, R), once)
        self.assertEqual(convert_text("₹999 / $10.64 per month", Decimal("100")), "₹999 / $10.64 per month")
        # the cog always starts from the stored original, so a new rate simply replaces the figure
        self.assertEqual(convert_text("₹999 / month", Decimal("90")), "₹999 / $11.10 per month")

    def test_no_rate_or_a_bad_rate_never_produces_a_price(self):
        for bad in (None, Decimal(0), Decimal(-5), Decimal("NaN"), Decimal("Infinity"), Decimal("0.5"),
                    Decimal("100000")):
            self.assertEqual(convert_text("₹999 / month", bad), "₹999 / month")
        with self.assertRaises(ValueError):
            usd_amount(Decimal(999), Decimal(0))

    def test_refresh_interval_stays_within_30_and_60_minutes(self):
        for configured, expected in ((5, 30), (30, 30), (45, 45), (60, 60), (500, 60)):
            with mock.patch.object(config, "FX_REFRESH_MINUTES", configured):
                self.assertEqual(refresh_minutes(), expected)

    def test_the_setting_is_clamped_when_the_config_is_read(self):
        code = "import config; print(config.FX_REFRESH_MINUTES, config.FX_ENABLED, config.FX_API_URLS)"
        run = lambda **env: subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,  # noqa: E731
                                           env={**_os.environ, **env}, cwd=_os.getcwd()).stdout.strip()
        self.assertEqual(run(FX_REFRESH_MINUTES="5"), "30 True []")
        self.assertEqual(run(FX_REFRESH_MINUTES="999", FX_ENABLED="off"), "60 False []")
        self.assertEqual(run(FX_API_URLS="https://a.example/x, https://b.example/y"),
                         "60 True ['https://a.example/x', 'https://b.example/y']")


class PayloadTests(unittest.TestCase):
    def test_known_feed_shapes(self):
        self.assertEqual(parse_payload({"amount": 1.0, "base": "USD", "rates": {"INR": 93.87}}), (R, None))
        rate, hint = parse_payload({"result": "success", "rates": {"INR": 93.87}, "time_next_update_unix": 1800000000})
        self.assertEqual((rate, hint), (R, 1800000000.0))
        self.assertEqual(parse_payload({"date": "x", "usd": {"inr": 93.87}})[0], R)

    def test_unusable_payloads_are_rejected(self):
        for bad in (None, [], "x", {}, {"rates": {}}, {"rates": {"INR": 0}}, {"rates": {"INR": -3}},
                    {"rates": {"INR": "abc"}}, {"rates": {"INR": None}}, {"rates": {"INR": float("nan")}},
                    {"rates": {"INR": float("inf")}}, {"rates": {"INR": 1e9}}, {"rates": {"INR": 0.01}},
                    {"result": "error", "rates": {"INR": 93.87}}):
            self.assertEqual(parse_payload(bad), (None, None), bad)


# ---------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Feed:
    """A local HTTP server standing in for the exchange-rate API."""

    def __init__(self):
        self.hits, self.mode, self.payload, self.delay = 0, "ok", {"rates": {"INR": 93.87}}, 0
        self.headers = {}
        app = web.Application()
        app.router.add_get("/rate", self.handle)
        self.server = TestServer(app)

    async def handle(self, request):
        self.hits += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "500":
            return web.Response(status=500, text="down")
        if self.mode == "429":
            return web.Response(status=429, text="slow down", headers=self.headers)
        if self.mode == "html":
            return web.Response(text="<html>maintenance</html>", content_type="text/html")
        return web.json_response(self.payload)

    @property
    def url(self):
        return str(self.server.make_url("/rate"))


class DbBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-fx-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = Database(_os.path.join(self.dir, "t.db"))
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)


class RateKeeperTests(DbBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.feed, self.second, self.clock = Feed(), Feed(), Clock()
        for server in (self.feed, self.second):
            await server.server.start_server()
            self.addAsyncCleanup(server.server.close)

    def keeper(self, *feeds, **kw):
        return RateKeeper(self.db, [f.url for f in feeds], interval_minutes=60, clock=self.clock, **kw)

    async def test_a_good_rate_is_validated_stored_and_survives_a_restart(self):
        keeper = self.keeper(self.feed)
        self.assertEqual(await keeper.refresh(), "updated")
        self.assertEqual(keeper.rate, R)
        restarted = self.keeper(self.feed)
        await restarted.load()
        self.assertEqual(restarted.rate, R)                       # prices work before any network call
        self.assertEqual(self.feed.hits, 1)

    async def test_api_failures_keep_the_last_rate(self):
        keeper = self.keeper(self.feed)
        await keeper.refresh()
        for mode in ("500", "html"):
            self.feed.mode = mode
            self.clock.now += 3 * 3600
            self.assertEqual(await keeper.refresh(), "failed", mode)
            self.assertEqual(keeper.rate, R, mode)

    async def test_invalid_rates_are_never_accepted(self):
        keeper = self.keeper(self.feed)
        for payload in ({"rates": {"INR": 0}}, {"rates": {"INR": -1}}, {"rates": {"INR": "abc"}}, {"rates": {}}, {}):
            self.feed.payload = payload
            self.clock.now += 3 * 3600
            self.assertEqual(await keeper.refresh(), "failed", payload)
            self.assertIsNone(keeper.rate)                        # nothing to show rather than something wrong
        self.assertIsNone(await self.db.fx_get(fx.PAIR))

    async def test_an_implausible_jump_is_ignored(self):
        keeper = self.keeper(self.feed)
        await keeper.refresh()
        self.feed.payload = {"rates": {"INR": 200}}
        self.clock.now += 3 * 3600
        self.assertEqual(await keeper.refresh(), "failed")
        self.assertEqual(keeper.rate, R)
        self.feed.payload = {"rates": {"INR": 94.5}}              # a normal move is fine
        self.clock.now += 3 * 3600
        self.assertEqual(await keeper.refresh(), "updated")
        self.assertEqual(keeper.rate, Decimal("94.5"))

    async def test_the_next_feed_is_tried_when_one_is_down(self):
        self.feed.mode = "500"
        keeper = self.keeper(self.feed, self.second)
        self.assertEqual(await keeper.refresh(), "updated")
        self.assertEqual((keeper.rate, keeper.source), (R, self.second.url))
        self.assertEqual((self.feed.hits, self.second.hits), (1, 1))

    async def test_a_slow_feed_times_out_instead_of_hanging(self):
        self.feed.delay = 2
        keeper = self.keeper(self.feed, timeout=0.2)
        started = asyncio.get_running_loop().time()
        self.assertEqual(await keeper.refresh(), "failed")
        self.assertLess(asyncio.get_running_loop().time() - started, 1.5)

    async def test_rate_limiting_backs_off_even_before_the_first_rate_was_ever_fetched(self):
        self.feed.mode = "429"
        keeper = self.keeper(self.feed)
        self.assertEqual(await keeper.refresh(), "failed")
        self.assertIsNone(keeper.rate)
        self.clock.now += 10 * 60
        self.assertEqual(await keeper.refresh(), "skipped")        # e.g. /price track must not hit the API again
        self.assertEqual(self.feed.hits, 1)
        self.clock.now += 12 * 60                                  # 22 minutes: the block is over
        self.assertEqual(await keeper.refresh(), "failed")
        self.assertEqual(self.feed.hits, 2)
        self.feed.mode = "ok"
        self.clock.now += 3600
        self.assertEqual(await keeper.refresh(), "updated")

    async def test_429_with_a_known_rate_makes_no_calls_until_the_block_is_over(self):
        keeper = self.keeper(self.feed)
        await keeper.refresh()
        self.feed.mode = "429"
        self.clock.now += 13 * 3600
        self.assertEqual(await keeper.refresh(), "failed")        # hit once, got throttled
        hits = self.feed.hits
        self.clock.now += 15 * 60
        self.assertEqual(await keeper.refresh(), "skipped")       # still inside the 21 minute back-off
        self.assertEqual(self.feed.hits, hits)
        self.feed.mode = "ok"
        self.clock.now += 10 * 60
        self.assertIn(await keeper.refresh(), ("updated", "unchanged"))
        self.assertEqual(keeper.rate, R)

    async def test_the_api_is_not_called_while_no_new_rate_can_exist(self):
        keeper = self.keeper(self.feed)
        await keeper.refresh()
        for _ in range(3):
            self.clock.now += 15 * 60
            self.assertEqual(await keeper.refresh(), "skipped")
        self.assertEqual(self.feed.hits, 1)
        self.clock.now += 20 * 60                                  # an hour after the last check
        self.assertEqual(await keeper.refresh(), "unchanged")
        self.assertEqual(self.feed.hits, 2)

    async def test_the_feeds_own_next_update_time_decides_when_to_ask_again(self):
        self.feed.payload = {"result": "success", "rates": {"INR": 93.87},
                             "time_next_update_unix": self.clock.now + 6 * 3600}
        keeper = self.keeper(self.feed)
        await keeper.refresh()
        self.clock.now += 5 * 3600
        self.assertEqual(await keeper.refresh(), "skipped")
        self.clock.now += 3600 + 120
        self.assertEqual(await keeper.refresh(), "unchanged")
        self.assertEqual(self.feed.hits, 2)

    async def test_a_far_off_hint_is_capped_so_a_wrong_hint_cannot_freeze_the_rate(self):
        self.feed.payload = {"rates": {"INR": 93.87}, "time_next_update_unix": self.clock.now + 5 * 86400}
        keeper = self.keeper(self.feed)
        await keeper.refresh()
        self.assertLessEqual(keeper.next_check - self.clock.now, fx.MAX_WAIT)

    async def test_a_manual_refresh_skips_the_wait_but_not_the_cooldown(self):
        keeper = self.keeper(self.feed)
        await keeper.refresh()
        self.clock.now += 3 * 60
        self.assertEqual(await keeper.refresh(force=True), "cooldown")   # checked 3 minutes ago
        self.clock.now += 3 * 60
        self.assertEqual(await keeper.refresh(force=True), "unchanged")
        self.assertEqual(self.feed.hits, 2)

    async def test_a_corrupt_saved_rate_is_ignored_on_start(self):
        await self.db.fx_set(fx.PAIR, "0", "x")
        keeper = self.keeper(self.feed)
        await keeper.load()
        self.assertIsNone(keeper.rate)
        await self.db.fx_set(fx.PAIR, "banana", "x")
        await keeper.load()
        self.assertIsNone(keeper.rate)


# ---------------------------------------------------------------------------
class FakePartial:
    def __init__(self, channel, message_id):
        self.channel, self.id = channel, message_id

    async def edit(self, view=None):
        if self.channel.error:
            raise self.channel.error
        self.channel.edits.append((self.id, view))


class FakeChannel:
    def __init__(self, cid=500):
        self.id, self.error, self.edits, self.sent = cid, None, [], []
        self.guild = types.SimpleNamespace(id=10, me=None)
        self.mention = f"<#{cid}>"
        self.name = "prices"

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)

    async def send(self, view=None, **kw):
        self.sent.append(view)
        return types.SimpleNamespace(id=9000 + len(self.sent), channel=self, guild=self.guild)

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class FakeBot:
    def __init__(self, db):
        self.db, self.channels = db, {}
        self.user = types.SimpleNamespace(id=77)

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        raise http_error(discord.NotFound, 404)

    async def wait_until_ready(self):
        await asyncio.Event().wait()


class FakeInteraction:
    def __init__(self, bot, channel=None):
        self.client, self.channel, self.guild_id = bot, channel, 10
        self.user = types.SimpleNamespace(id=1)
        self.sent, self._done = [], False
        self.response = types.SimpleNamespace(defer=self._defer, is_done=lambda: self._done, send_message=self._send)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self, **kw):
        self._done = True

    async def _send(self, *a, **kw):
        self._done = True
        self.sent.append(kw["view"])

    @property
    def text(self):
        return "\n".join(text_of(v) for v in self.sent)


class CardBase(DbBase):
    PRICES = "**Starter** ₹999 / month\n**Pro** ₹1,999 / month"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        ui.set_tagline(None)
        self.bot = FakeBot(self.db)
        self.channel = self.bot.channels[500] = FakeChannel(500)
        self.cog = Pricing(self.bot)
        self.addCleanup(self.cog.watcher.cancel)
        self.cog.rates.refresh = mock.AsyncMock(return_value="skipped")   # no network in these tests
        self.cog.rates.rate = R

    async def post(self, title="VPS Plans", body=None, footer="Prices in INR"):
        """What /embed does: post the card, then ask the pricing cog to follow it."""
        modal = EmbedModal(self.channel, title, footer)
        modal.message_body._value = body or self.PRICES
        modal.heading._value, modal.footer_text._value = title, footer
        interaction = FakeInteraction(self.bot)
        await modal.on_submit(interaction)
        return interaction

    def edits(self):
        return [text_of(view) for _, view in self.channel.edits]


class EmbedIntegrationTests(CardBase):
    async def test_prices_are_converted_and_the_original_is_stored(self):
        done = await self.post()
        posted = text_of(self.channel.sent[0])
        self.assertIn("₹999 / $10.64 per month", posted)
        self.assertIn("₹1,999 / $21.30 per month", posted)
        self.assertIn("USD prices were added", done.text)
        cards = await self.db.price_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]["title"], cards[0]["body"], cards[0]["channel_id"]),
                         ("VPS Plans", self.PRICES, 500))             # the INR-only original, not the USD text
        self.assertEqual(cards[0]["message_id"], 9001)

    async def test_title_and_footer_prices_are_converted_too(self):
        await self.post(title="VPS from ₹499/month", footer="Pro ₹1,999")
        posted = text_of(self.channel.sent[0])
        self.assertIn("VPS from ₹499 / $5.32 per month", posted)
        self.assertIn("Pro ₹1,999 / $21.30", posted)

    async def test_posts_without_inr_prices_are_exactly_as_before(self):
        done = await self.post(body="Maintenance tonight at 10pm", footer="")
        self.assertEqual(text_of(self.channel.sent[0]), text_of(announcement_card("VPS Plans", "Maintenance tonight at 10pm")))
        self.assertEqual(await self.db.price_cards(), [])
        self.assertNotIn("USD", done.text)

    async def test_without_a_rate_the_card_is_posted_as_is_and_converted_when_one_arrives(self):
        self.cog.rates.rate = None
        done = await self.post()
        self.assertNotIn("$", text_of(self.channel.sent[0]))
        self.assertIn("No exchange rate yet", done.text)
        self.assertEqual(len(await self.db.price_cards()), 1)         # still followed
        self.cog.rates.rate = R
        await self.cog.cycle()
        self.assertEqual(len(self.channel.edits), 1)
        self.assertIn("₹999 / $10.64 per month", self.edits()[0])

    async def test_a_card_too_long_for_usd_is_posted_as_inr_only(self):
        body = "\n".join(f"Plan {i}: ₹{1000 + i} / month" for i in range(150))[:3490]
        done = await self.post(title="Big list", body=body, footer="")
        self.assertNotIn("$", text_of(self.channel.sent[0]))
        self.assertIn("too long", done.text)
        self.assertEqual(await self.db.price_cards(), [])

    async def test_the_feature_can_be_switched_off(self):
        with mock.patch.object(config, "FX_ENABLED", False):
            await self.post()
        self.assertNotIn("$", text_of(self.channel.sent[0]))
        self.assertEqual(await self.db.price_cards(), [])

    async def test_embed_works_when_the_pricing_cog_is_not_loaded(self):
        del self.bot.pricing
        await self.post()
        self.assertEqual(text_of(self.channel.sent[0]), text_of(announcement_card("VPS Plans", self.PRICES, "Prices in INR")))


class AutomaticUpdateTests(CardBase):
    async def test_cards_are_edited_only_when_the_usd_figures_change(self):
        await self.post()
        await self.cog.cycle()
        self.assertEqual(self.channel.edits, [])                      # nothing changed since posting
        self.cog.rates.rate = Decimal("94.50")
        await self.cog.cycle()
        self.assertEqual(len(self.channel.edits), 1)
        shown = self.edits()[0]
        self.assertIn("₹999 / $10.57 per month", shown)               # new USD ...
        self.assertIn("₹1,999 / $21.15 per month", shown)
        self.assertIn("₹999", shown)                                  # ... same INR
        await self.cog.cycle()
        self.cog.rates.rate = Decimal("94.5001")                      # moves, but no displayed figure changes
        await self.cog.cycle()
        self.assertEqual(len(self.channel.edits), 1)
        self.assertEqual(len(self.channel.sent), 1)                   # never a second message

    async def test_the_original_inr_text_in_the_database_is_never_modified(self):
        await self.post()
        for rate in ("94.50", "91.20", "96.00"):
            self.cog.rates.rate = Decimal(rate)
            await self.cog.cycle()
        card = (await self.db.price_cards())[0]
        self.assertEqual((card["title"], card["body"], card["footer"]), ("VPS Plans", self.PRICES, "Prices in INR"))
        self.assertIn("$20.82", self.edits()[-1])                     # 1999 / 96.00, no stacked amounts
        self.assertEqual(self.edits()[-1].count("$"), 2)

    async def test_updates_survive_a_restart_without_duplicate_edits(self):
        await self.post()
        restarted = Pricing(self.bot)
        self.addCleanup(restarted.watcher.cancel)
        restarted.rates.refresh = mock.AsyncMock(return_value="skipped")
        restarted.rates.rate = R
        await restarted.cycle()
        self.assertEqual(self.channel.edits, [])                      # same figures: no edit, no new message
        restarted.rates.rate = Decimal("95")
        await restarted.cycle()
        self.assertEqual(len(self.channel.edits), 1)

    async def test_the_saved_rate_is_used_after_a_restart_even_if_the_api_is_down(self):
        await self.db.fx_set(fx.PAIR, "93.87", "https://x.example")
        restarted = Pricing(self.bot)
        self.addCleanup(restarted.watcher.cancel)
        await restarted.cog_load()
        self.assertEqual(restarted.rates.rate, R)
        self.assertIn("₹999 / $10.64", restarted.prepare("t", "₹999", "")[1])

    async def test_a_deleted_message_is_dropped_and_other_cards_still_update(self):
        await self.post(title="Gone")
        await self.post(title="Stays")
        gone, stays = await self.db.price_cards()
        self.channel.error = http_error(discord.NotFound, 404)
        self.cog.rates.rate = Decimal("90")
        await self.cog.cycle()                                        # both edits fail with NotFound
        self.assertEqual({c["status"] for c in await self.db.price_cards(None)}, {"missing"})
        self.channel.error = None
        self.cog.rates.rate = Decimal("91")
        await self.cog.cycle()
        self.assertEqual(self.channel.edits, [])                      # missing cards are left alone

    async def test_a_temporary_discord_failure_is_retried_on_the_next_pass(self):
        await self.post()
        self.channel.error = http_error(discord.HTTPException, 500)
        self.cog.rates.rate = Decimal("90")
        await self.cog.cycle()
        self.assertEqual(self.channel.edits, [])
        self.assertEqual((await self.db.price_cards())[0]["status"], "active")
        self.channel.error = None
        await self.cog.cycle()                                        # same rate, but the pass is repeated
        self.assertEqual(len(self.channel.edits), 1)
        self.assertIn("$11.10", self.edits()[0])
        await self.cog.cycle()
        self.assertEqual(len(self.channel.edits), 1)

    async def test_a_failed_rate_check_keeps_the_cards_as_they_are(self):
        await self.post()
        self.cog.rates.refresh = mock.AsyncMock(return_value="failed")
        await self.cog.cycle()
        self.assertEqual(self.channel.edits, [])
        self.assertEqual(self.cog.rates.rate, R)

    async def test_stopped_cards_are_not_touched(self):
        await self.post()
        card = (await self.db.price_cards())[0]
        await self.db.price_card_set(card["id"], status="stopped")
        self.cog.rates.rate = Decimal("90")
        await self.cog.cycle()
        self.assertEqual(self.channel.edits, [])

    async def test_there_is_only_one_background_task(self):
        await self.cog.cog_load()
        task = self.cog.watcher._task
        await self.cog.cog_load()
        self.assertIs(self.cog.watcher._task, task)
        self.assertTrue(self.cog.watcher.is_running())
        await self.cog.cog_unload()
        await asyncio.sleep(0)
        self.assertFalse(self.cog.watcher.is_running())

    async def test_no_background_task_when_the_feature_is_off(self):
        with mock.patch.object(config, "FX_ENABLED", False):
            await self.cog.cog_load()
        self.assertFalse(self.cog.watcher.is_running())


# ---------------------------------------------------------------------------
class CommandTests(CardBase):
    def adopt_message(self, title="Old plans", body=None, footer="", author=77):
        card = announcement_card(title, body or self.PRICES, footer)
        real = [discord.components._component_factory(p) for p in card.to_components()]  # discord.py's own classes
        message = types.SimpleNamespace(id=4242, author=types.SimpleNamespace(id=author), components=real,
                                        guild=self.channel.guild, channel=self.channel)
        self.channel.messages = {4242: message}
        return message

    async def run(self, command, **kw):
        interaction = FakeInteraction(self.bot, self.channel)
        await command.callback(self.cog, interaction, **kw)
        return interaction

    def test_a_card_is_read_back_exactly(self):
        for tagline in (None, "Hector Hosting"):
            ui.set_tagline(tagline)
            for footer in ("", "Prices include GST"):
                view = announcement_card("VPS Plans", self.PRICES, footer)
                real = [discord.components._component_factory(p).to_dict() for p in view.to_components()]
                self.assertEqual(card_parts(real), ("VPS Plans", self.PRICES, footer), (tagline, footer))
        ui.set_tagline(None)
        self.assertIsNone(card_parts([]))
        self.assertIsNone(card_parts([{"type": 17, "components": [{"type": 10, "content": "plain"}]}]))

    async def test_an_existing_card_can_be_adopted_and_gets_usd_prices(self):
        self.adopt_message()
        done = await self.run(Pricing.track_cmd, message="https://discord.com/channels/10/500/4242")
        self.assertIn("Tracking it", done.text)
        self.assertEqual(len(self.channel.edits), 1)
        self.assertIn("₹999 / $10.64 per month", self.edits()[0])
        card = (await self.db.price_cards())[0]
        self.assertEqual((card["message_id"], card["body"]), (4242, self.PRICES))   # original INR text kept
        again = await self.run(Pricing.track_cmd, message="4242", channel=self.channel)
        self.assertIn("Tracking it", again.text)
        self.assertEqual(len(await self.db.price_cards(None)), 1)                  # no duplicate record

    async def test_adoption_refuses_what_it_cannot_follow(self):
        self.adopt_message(author=5)
        self.assertIn("not posted by me", (await self.run(Pricing.track_cmd, message="4242", channel=self.channel)).text)
        self.adopt_message(body="No prices in this one")
        self.assertIn("No INR prices", (await self.run(Pricing.track_cmd, message="4242", channel=self.channel)).text)
        self.assertIn("message link", (await self.run(Pricing.track_cmd, message="nonsense")).text)
        self.assertEqual(await self.db.price_cards(None), [])

    async def test_untrack_restores_the_inr_only_card(self):
        await self.post()
        done = await self.run(Pricing.untrack_cmd, message="9001", channel=self.channel)
        self.assertIn("Stopped", done.text)
        restored = self.edits()[-1]
        self.assertIn("₹999 / month", restored)
        self.assertNotIn("$", restored)
        self.cog.rates.rate = Decimal("90")
        await self.cog.cycle()
        self.assertEqual(len(self.channel.edits), 1)                  # stays stopped
        self.assertIn("Not tracked", (await self.run(Pricing.untrack_cmd, message="9001", channel=self.channel)).text)

    async def test_status_and_refresh(self):
        await self.post()
        status = await self.run(Pricing.status)
        for expected in ("1 USD = ₹93.87", "VPS Plans", "1 active"):
            self.assertIn(expected, status.text)
        self.cog.rates.refresh = mock.AsyncMock(return_value="cooldown")
        self.assertIn("Checked a moment ago", (await self.run(Pricing.refresh)).text)
        self.cog.rates.refresh = mock.AsyncMock(return_value="failed")
        self.assertIn("last good rate", (await self.run(Pricing.refresh)).text)
        self.cog.rates.rate = None
        self.assertIn("No exchange rate yet", (await self.run(Pricing.refresh)).text)

    async def test_commands_are_staff_only(self):
        interaction = FakeInteraction(self.bot, self.channel)
        interaction.user = types.SimpleNamespace(id=2)               # not a guild member with a staff role
        self.assertFalse(await self.cog.interaction_check(interaction))


if __name__ == "__main__":
    unittest.main()
