"""Tests for service expiry management: storage, expiry changes, reminder scheduling, restarts, commands."""
import asyncio
import datetime as dt
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import discord

import cogs.services as svc_mod
import config
from cogs.services import Services
from desk.database import Database, service_number
from desk.utils import _tz

IST = ZoneInfo("Asia/Kolkata")
TODAY = dt.date(2026, 11, 1)
GUILD = 10
CLIENT = 500000000000000500      # realistic 18-digit ids (the parser expects real snowflakes)
CHAN = 900000000000000900
LOGCHAN = 777000000000000777


def at(hour=9, minute=30, day=TODAY):
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def plus(days, base=TODAY):
    return (base + dt.timedelta(days=days)).isoformat()


def text_of(view) -> str:
    """All text in a Components V2 view (nested containers included)."""
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


def http_error(cls, status):
    return cls(types.SimpleNamespace(status=status, reason="x"), "boom")


class FakeUser:
    def __init__(self, uid, fail=None):
        self.id, self.mention, self.fail, self.sent = uid, f"<@{uid}>", fail, []

    async def send(self, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(kwargs["view"])


class FakeChannel(FakeUser):
    def __init__(self, cid, fail=None):
        super().__init__(cid, fail)
        self.mention = f"<#{cid}>"


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.users, self.channels = {}, {}
        self.ready = asyncio.Event()

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        if uid in self.users:
            return self.users[uid]
        raise http_error(discord.NotFound, 404)

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        raise http_error(discord.NotFound, 404)

    async def wait_until_ready(self):
        await self.ready.wait()


class FakeInteraction:
    def __init__(self, user_id=1, guild_id=GUILD):
        self.guild_id = guild_id
        self.user = types.SimpleNamespace(id=user_id)
        self.sent, self._done = [], False
        self.response = types.SimpleNamespace(defer=self._defer, is_done=lambda: self._done,
                                              send_message=self._send)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self, **kwargs):
        self._done = True

    async def _send(self, *args, **kwargs):
        self._done = True
        self.sent.append(kwargs["view"])

    @property
    def text(self):
        return "\n".join(text_of(v) for v in self.sent)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-services-")
        self.path = os.path.join(self.dir, "services.db")
        self.db = Database(self.path)
        await self.db.connect()
        self.bot = FakeBot(self.db)
        self.cog = Services(self.bot)
        self.client = self.bot.users[CLIENT] = FakeUser(CLIENT)
        self.channel = self.bot.channels[CHAN] = FakeChannel(CHAN)

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def add(self, expiry, *, server="srv-1", client=CLIENT, ticket=CHAN, status_cancel=False, **kw):
        sid = await self.db.create_service(
            1, guild_id=GUILD, client_discord_id=client, client_name="Ann", service_name="VPS",
            server_ref=server, start_date=plus(-30), expiry_date=expiry, ticket_channel_id=ticket, **kw)
        if status_cancel:
            await self.db.cancel_service(1, sid, "test")
        return sid


# ---------------------------------------------------------------------------
class ServiceStorageTests(Base):
    async def test_unique_ids_and_duplicate_active_server_guard(self):
        a = await self.add(plus(10), server="UUID-A")
        b = await self.add(plus(10), server="UUID-B")
        self.assertEqual((a, b), (1, 2))
        self.assertEqual(service_number(a), "SVC-0001")
        self.assertIsNone(await self.add(plus(10), server="uuid-a"))  # same server (case-insensitive)
        await self.db.cancel_service(1, a)
        again = await self.add(plus(10), server="UUID-A")             # cancelled one no longer blocks
        self.assertEqual(again, 3)

    async def test_lookup_by_id_uuid_and_prefix(self):
        a = await self.add(plus(10), server="3f2a9c1e-aaaa-bbbb-cccc-111122223333")
        await self.add(plus(10), server="other-server")
        for query in ("1", "SVC-0001", "svc-1", "3F2A9C1E-AAAA-BBBB-CCCC-111122223333", "3f2a9c1e"):
            self.assertEqual([r["id"] for r in await self.db.find_services(GUILD, query)], [a], query)
        self.assertEqual(await self.db.find_services(GUILD, "nope"), [])
        self.assertEqual(await self.db.find_services(99, "SVC-0001"), [])  # other server cannot see it
        self.assertEqual(await self.db.find_services(GUILD, "3f2a9"), [])  # prefix needs 6+ characters

    async def test_expiry_change_is_logged_with_days_added_or_removed(self):
        sid = await self.add(plus(10))
        longer = await self.db.change_service_expiry(7, sid, plus(25), "Renewed", TODAY.isoformat())
        self.assertEqual((longer["previous"], longer["new"], longer["days_changed"]), (plus(10), plus(25), 15))
        shorter = await self.db.change_service_expiry(7, sid, plus(20), None, TODAY.isoformat())
        self.assertEqual(shorter["days_changed"], -5)
        self.assertEqual((await self.db.get_service(sid))["expiry_date"], plus(20))
        history = await self.db.service_expiry_history(sid)
        self.assertEqual([(h["days_changed"], h["reason"]) for h in history], [(-5, None), (15, "Renewed")])

    async def test_cancel_keeps_history_and_blocks_changes(self):
        sid = await self.add(plus(10))
        await self.db.change_service_expiry(7, sid, plus(12), "x", TODAY.isoformat())
        self.assertTrue(await self.db.cancel_service(7, sid, "client left"))
        self.assertFalse(await self.db.cancel_service(7, sid))
        row = await self.db.get_service(sid)
        self.assertEqual((row["status"], row["cancel_reason"], row["cancelled_by"]), ("cancelled", "client left", 7))
        self.assertEqual(len(await self.db.service_expiry_history(sid)), 1)  # history stays
        self.assertIsNone(await self.db.change_service_expiry(7, sid, plus(30), None, TODAY.isoformat()))

    async def test_upgrade_adds_tables_to_an_existing_database(self):
        sid = await self.add(plus(10))
        await self.db.conn.executescript(
            "DROP TABLE service_reminders; DROP TABLE service_expiry_log; DROP TABLE services;")
        await self.db.close()
        self.db = Database(self.path)            # what a restart of an older install does
        await self.db.connect()
        self.assertEqual(await self.db.get_service(sid), None)
        self.assertEqual(await self.add(plus(10)), 1)


# ---------------------------------------------------------------------------
class ReminderClaimTests(Base):
    async def test_claim_happens_once_even_after_a_restart(self):
        sid = await self.add(plus(3))
        self.assertTrue(await self.db.claim_service_reminder(sid, plus(3), 3))
        self.assertFalse(await self.db.claim_service_reminder(sid, plus(3), 3))
        await self.db.finish_service_reminder(sid, plus(3), 3, "sent", "sent")
        await self.db.close()
        self.db = Database(self.path)            # bot restart
        await self.db.connect()
        self.assertFalse(await self.db.claim_service_reminder(sid, plus(3), 3))

    async def test_temporary_failures_are_retried_a_limited_number_of_times(self):
        sid = await self.add(plus(3))
        results = []
        for _ in range(5):
            claimed = await self.db.claim_service_reminder(sid, plus(3), 3)
            results.append(claimed)
            if claimed:
                await self.db.finish_service_reminder(sid, plus(3), 3, "error", "none", retry=True)
        self.assertEqual(results, [True, True, True, False, False])

    async def test_changing_the_expiry_starts_a_fresh_schedule(self):
        sid = await self.add(plus(3))
        self.assertTrue(await self.db.claim_service_reminder(sid, plus(3), 3))
        await self.db.change_service_expiry(1, sid, plus(9), None, TODAY.isoformat())
        self.assertFalse(await self.db.claim_service_reminder(sid, plus(3), 3))   # old date: stale
        self.assertEqual(await self.db.due_service_candidates(TODAY.isoformat()), [])  # 9 days away
        later = dt.date.fromisoformat(plus(4))                                    # 5 days before the new date
        self.assertTrue(await self.db.claim_service_reminder(sid, plus(9), 5))
        self.assertEqual([r["id"] for r in await self.db.due_service_candidates(later.isoformat())], [sid])

    async def test_cancelled_service_cannot_claim(self):
        sid = await self.add(plus(2), status_cancel=True)
        self.assertEqual(await self.db.due_service_candidates(TODAY.isoformat()), [])
        self.assertFalse(await self.db.claim_service_reminder(sid, plus(2), 2))


# ---------------------------------------------------------------------------
class ReminderRunTests(Base):
    async def test_only_the_reminder_days_are_sent_once_per_day(self):
        ids = {off: await self.add(plus(off), server=f"s{off}") for off in (-1, 0, 1, 3, 5, 6)}
        self.assertEqual(await self.cog.run_reminders(at()), 4)           # 0, 1, 3, 5
        self.assertEqual(len(self.client.sent), 4)
        self.assertEqual(len(self.channel.sent), 4)
        texts = [text_of(v) for v in self.client.sent]
        self.assertTrue(any("expires today" in t for t in texts))
        self.assertTrue(any("5 days left" in t for t in texts))
        self.assertTrue(any(f"SVC-{ids[3]:04d}" in t and "3 days left" in t for t in texts))
        self.assertFalse(any("expired" in t and "ago" in t for t in texts))
        # the loop ticks again, and the bot "restarts": nothing is sent a second time
        self.assertEqual(await self.cog.run_reminders(at(15)), 0)
        await self.db.close()
        self.db = self.bot.db = Database(self.path)
        await self.db.connect()
        self.assertEqual(await Services(self.bot).run_reminders(at(18)), 0)
        self.assertEqual(len(self.client.sent), 4)

    async def test_next_day_sends_the_next_countdown_step(self):
        await self.add(plus(3))
        await self.cog.run_reminders(at())
        await self.cog.run_reminders(at(day=TODAY + dt.timedelta(days=1)))
        self.assertEqual([("3 days left" in text_of(v), "2 days left" in text_of(v)) for v in self.client.sent],
                         [(True, False), (False, True)])

    async def test_nothing_before_the_reminder_hour_and_catches_up_afterwards(self):
        await self.add(plus(2))
        self.assertEqual(await self.cog.run_reminders(at(hour=8, minute=59)), 0)
        self.assertEqual(await self.cog.run_reminders(at(hour=svc_mod.REMINDER_HOUR, minute=0)), 1)

    async def test_cancelled_and_expired_services_get_nothing(self):
        await self.add(plus(2), server="a", status_cancel=True)
        await self.add(plus(-3), server="b")
        self.assertEqual(await self.cog.run_reminders(at()), 0)
        self.assertEqual(self.client.sent, [])

    async def test_closed_dms_or_missing_ticket_do_not_stop_the_other_channel(self):
        self.bot.users[CLIENT + 1] = FakeUser(CLIENT + 1, fail=http_error(discord.Forbidden, 403))   # DMs closed
        self.bot.users[CLIENT + 2] = FakeUser(CLIENT + 2)
        blocked = await self.add(plus(2), server="blocked", client=CLIENT + 1, ticket=CHAN)
        no_ticket = await self.add(plus(2), server="gone", client=CLIENT + 2, ticket=CHAN + 1)      # channel deleted
        await self.cog.run_reminders(at())
        self.assertEqual(len(self.channel.sent), 1)                  # ticket still got the reminder
        self.assertEqual(len(self.bot.users[CLIENT + 2].sent), 1)           # DM still went out
        rows = {r["service_id"]: r for r in await self.db.fetchall("SELECT * FROM service_reminders")}
        self.assertEqual((rows[blocked]["dm_status"], rows[blocked]["ticket_status"]), ("closed", "sent"))
        self.assertEqual((rows[no_ticket]["dm_status"], rows[no_ticket]["ticket_status"]), ("sent", "missing"))
        self.assertEqual(await self.cog.run_reminders(at(20)), 0)    # permanent failures are not retried

    async def test_ticket_without_permission_is_recorded(self):
        self.bot.channels[CHAN] = FakeChannel(CHAN, fail=http_error(discord.Forbidden, 403))
        sid = await self.add(plus(1))
        await self.cog.run_reminders(at())
        row = (await self.db.service_reminder_history(sid))[0]
        self.assertEqual((row["dm_status"], row["ticket_status"], row["state"]), ("sent", "denied", "done"))

    async def test_temporary_discord_errors_are_retried_then_given_up(self):
        self.client.fail = http_error(discord.HTTPException, 500)
        await self.add(plus(2), ticket=None)
        attempts = [await self.cog.run_reminders(at(9 + i)) for i in range(5)]
        self.assertEqual(attempts, [1, 1, 1, 0, 0])
        self.client.fail = None
        self.assertEqual(await self.cog.run_reminders(at(20)), 0)

    async def test_staff_are_told_when_nobody_could_be_reached(self):
        log_channel = self.bot.channels[LOGCHAN] = FakeChannel(LOGCHAN)
        self.client.fail = http_error(discord.Forbidden, 403)
        await self.add(plus(1), ticket=None)
        with mock.patch.object(config, "LOG_CHANNEL_ID", LOGCHAN):
            await self.cog.run_reminders(at())
        self.assertEqual(len(log_channel.sent), 1)
        self.assertIn("Reminder not delivered", text_of(log_channel.sent[0]))

    async def test_one_broken_service_does_not_stop_the_rest(self):
        first = await self.add(plus(1), server="first")
        await self.add(plus(1), server="second")
        real = svc_mod.notify_client

        async def flaky(bot, svc, make_view):
            if svc["id"] == first:
                raise RuntimeError("boom")
            return await real(bot, svc, make_view)

        with mock.patch.object(svc_mod, "notify_client", flaky):
            await self.cog.run_reminders(at())
        self.assertEqual(len(self.client.sent), 1)

    async def test_only_one_background_loop_even_if_loaded_twice(self):
        await self.cog.cog_load()
        task = self.cog.watcher._task
        await self.cog.cog_load()
        self.assertTrue(self.cog.watcher.is_running())
        self.assertIs(self.cog.watcher._task, task)
        await self.cog.cog_unload()
        await asyncio.sleep(0)
        self.assertFalse(self.cog.watcher.is_running())

    async def test_running_loop_ticks_repeatedly_without_duplicate_reminders(self):
        """The real tasks.loop (fast interval): many ticks, one reminder."""
        sid = await self.add((dt.datetime.now(_tz()).date() + dt.timedelta(days=2)).isoformat())
        self.bot.ready.set()
        with mock.patch.object(svc_mod, "REMINDER_HOUR", 0):
            self.cog.watcher.change_interval(seconds=0.05)
            await self.cog.cog_load()
            await asyncio.sleep(0.4)
            await self.cog.cog_unload()
        self.assertGreaterEqual(self.cog.watcher.current_loop, 3)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(len(await self.db.service_reminder_history(sid)), 1)

    def test_project_timezone_is_kolkata(self):
        self.assertEqual(config.TIMEZONE, "Asia/Kolkata")
        self.assertEqual(str(_tz()), "Asia/Kolkata")


# ---------------------------------------------------------------------------
class ServiceCommandTests(Base):
    def setUp(self):
        patcher = mock.patch.object(svc_mod, "today", lambda: TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_cmd(self, command, **kwargs):
        interaction = FakeInteraction()
        await command.callback(self.cog, interaction, **kwargs)
        return interaction

    async def ticket_row(self, channel_id=CHAN, seq=12):
        await self.db.conn.execute(
            "INSERT INTO tickets(guild_id, guild_seq, subject, client_name, channel_id, opened_by) "
            "VALUES (?,?,?,?,?,?)", (GUILD, seq, "Order", "Ann", channel_id, 1))
        await self.db.conn.commit()

    ADD = dict(client_name="Ann", client=f"<@{CLIENT}>", service="Minecraft 4GB", server="3f2a9c1e-0000",
               expiry_date=plus(20), start_date=plus(-10))

    async def test_service_add_saves_everything_and_accepts_ticket_by_channel_or_number(self):
        await self.ticket_row()
        done = await self.run_cmd(Services.service_add, ticket=f"<#{CHAN}>", **self.ADD)
        self.assertIn("SVC-0001", done.text)
        row = await self.db.get_service(1)
        self.assertEqual((row["client_discord_id"], row["client_name"], row["service_name"], row["server_ref"]),
                         (CLIENT, "Ann", "Minecraft 4GB", "3f2a9c1e-0000"))
        self.assertEqual((row["start_date"], row["expiry_date"], row["ticket_channel_id"], row["status"]),
                         (plus(-10), plus(20), CHAN, "active"))
        second = await self.run_cmd(Services.service_add, ticket="TICKET-0012",
                                    **{**self.ADD, "server": "second-server"})
        self.assertIn("SVC-0002", second.text)
        self.assertEqual((await self.db.get_service(2))["ticket_channel_id"], CHAN)

    async def test_service_add_rejects_bad_input_without_saving(self):
        cases = [
            ({"expiry_date": plus(-1)}, "past"),
            ({"expiry_date": plus(-20)}, "before the starting"),
            ({"expiry_date": "31/12/2026"}, "Invalid date"),
            ({"client": "not a user"}, "Client not found"),
            ({"client": "<@999999999999999999>"}, "Client not found"),
            ({"ticket": "<#12345678901234567>"}, "Ticket not found"),
            ({"ticket": "TICKET-0099"}, "Ticket not found"),
        ]
        for override, expected in cases:
            done = await self.run_cmd(Services.service_add, **{**self.ADD, **override})
            self.assertIn(expected, done.text, override)
        self.assertEqual(await self.db.fetchall("SELECT id FROM services"), [])

    async def test_service_add_blocks_a_second_active_service_for_the_same_server(self):
        await self.run_cmd(Services.service_add, **self.ADD)
        again = await self.run_cmd(Services.service_add, **self.ADD)
        self.assertIn("Already tracked", again.text)
        self.assertIn("SVC-0001", again.text)

    async def test_service_expiry_updates_db_notifies_client_and_ticket_and_shows_the_diff(self):
        sid = await self.add(plus(20), server="3f2a9c1e-0000")
        done = await self.run_cmd(Services.service_expiry, service="3f2a9c1e-0000", new_expiry=plus(27),
                                  reason="Renewal paid")
        for expected in ("Previous Expiry", "New Expiry", "+7 days added", "Renewal paid", "delivered", "posted in"):
            self.assertIn(expected, done.text)
        self.assertEqual((await self.db.get_service(sid))["expiry_date"], plus(27))
        for view in (self.client.sent[0], self.channel.sent[0]):
            body = text_of(view)
            self.assertIn("+7 days added", body)
            self.assertIn("Renewal paid", body)
            self.assertIn(dt.date.fromisoformat(plus(20)).strftime("%d %b %Y"), body)
            self.assertIn(dt.date.fromisoformat(plus(27)).strftime("%d %b %Y"), body)
        self.assertIn(f"<@{CLIENT}>", text_of(self.channel.sent[0]))        # the ticket pings the client
        removed = await self.run_cmd(Services.service_expiry, service=f"SVC-{sid:04d}", new_expiry=plus(24))
        self.assertIn("3 days removed", removed.text)
        self.assertEqual([h["days_changed"] for h in await self.db.service_expiry_history(sid)], [-3, 7])

    async def test_service_expiry_rejects_same_past_and_cancelled(self):
        sid = await self.add(plus(20))
        self.assertIn("No change", (await self.run_cmd(Services.service_expiry, service="1", new_expiry=plus(20))).text)
        self.assertIn("past", (await self.run_cmd(Services.service_expiry, service="1", new_expiry=plus(-1))).text)
        self.assertIn("not found", (await self.run_cmd(Services.service_expiry, service="SVC-0099",
                                                       new_expiry=plus(30))).text)
        await self.db.cancel_service(1, sid)
        self.assertIn("cancelled", (await self.run_cmd(Services.service_expiry, service="1",
                                                       new_expiry=plus(30))).text)
        self.assertEqual((await self.db.get_service(sid))["expiry_date"], plus(20))
        self.assertEqual((self.client.sent, self.channel.sent), ([], []))

    async def test_expiry_change_replaces_todays_reminder_instead_of_doubling_it(self):
        sid = await self.add(plus(20))
        await self.run_cmd(Services.service_expiry, service="1", new_expiry=plus(3))   # 3 days left today
        self.assertEqual((len(self.client.sent), len(self.channel.sent)), (1, 1))      # the change notice
        self.assertEqual(await self.cog.run_reminders(at()), 0)                        # no extra reminder
        self.assertEqual(await self.cog.run_reminders(at(day=TODAY + dt.timedelta(days=1))), 1)
        self.assertIn("2 days left", text_of(self.client.sent[-1]))
        self.assertEqual(sid, 1)

    async def test_if_the_change_notice_reaches_nobody_the_normal_reminder_still_goes_out(self):
        self.client.fail = http_error(discord.Forbidden, 403)
        await self.add(plus(20), ticket=None)
        done = await self.run_cmd(Services.service_expiry, service="1", new_expiry=plus(3))
        self.assertIn("not delivered", done.text)
        self.client.fail = None                                                         # DMs reopened
        self.assertEqual(await self.cog.run_reminders(at()), 1)
        self.assertIn("3 days left", text_of(self.client.sent[0]))

    async def test_new_expiry_far_away_waits_for_five_days_before(self):
        await self.add(plus(2))
        done = await self.run_cmd(Services.service_expiry, service="1", new_expiry=plus(30))
        self.assertIn("first reminder on", done.text)
        self.assertEqual(await self.cog.run_reminders(at()), 0)
        self.assertEqual(await self.cog.run_reminders(at(day=TODAY + dt.timedelta(days=25))), 1)

    async def test_service_info_shows_details_changes_and_reminders(self):
        sid = await self.add(plus(2))
        await self.cog.run_reminders(at())
        await self.db.change_service_expiry(1, sid, plus(40), "Renewed", TODAY.isoformat())
        text = (await self.run_cmd(Services.service_info, service="SVC-0001")).text
        for expected in ("SVC-0001", "Ann", "VPS", "srv-1", f"<#{CHAN}>", "Expiry changes", "Renewed",
                         "Reminder history", "2 days left", "✅ Active"):
            self.assertIn(expected, text)

    async def test_service_list_is_sorted_filtered_and_skips_cancelled(self):
        await self.add(plus(30), server="far")
        await self.add(plus(2), server="soon")
        await self.add(plus(-2), server="late")
        await self.add(plus(1), server="gone", status_cancel=True)
        text = (await self.run_cmd(Services.service_list)).text
        self.assertLess(text.index("late"), text.index("soon"))
        self.assertLess(text.index("soon"), text.index("far"))
        self.assertNotIn("`gone`", text)
        self.assertIn("past expiry", text)
        week = (await self.run_cmd(Services.service_list, days=7)).text
        self.assertNotIn("`far`", week)
        self.assertIn("`soon`", week)
        day = (await self.run_cmd(Services.service_list, days=1)).text
        self.assertIn("within 1 day", day)
        self.assertNotIn("1 days", day)
        self.assertNotIn("`soon`", day)
        self.assertIn("`late`", day)                       # overdue but not cancelled: still needs attention
        await self.db.cancel_service(1, 3)
        self.assertIn("No active service expires within 1 day.",
                      (await self.run_cmd(Services.service_list, days=1)).text)

    async def test_service_cancel_stops_reminders_but_keeps_the_record(self):
        sid = await self.add(plus(2))
        await self.cog.run_reminders(at())
        done = await self.run_cmd(Services.service_cancel, service="SVC-0001", reason="Client left")
        self.assertIn("Reminders are stopped", done.text)
        self.assertEqual(await self.cog.run_reminders(at(day=TODAY + dt.timedelta(days=1))), 0)
        self.assertEqual(len(self.client.sent), 1)                                      # only the earlier one
        self.assertEqual(len(await self.db.service_reminder_history(sid)), 1)           # history kept
        self.assertIn("Cancelled", (await self.run_cmd(Services.service_info, service="1")).text)
        self.assertIn("Already cancelled", (await self.run_cmd(Services.service_cancel, service="1")).text)

    async def test_ambiguous_prefix_asks_for_the_service_id(self):
        await self.add(plus(5), server="abcdef-one")
        await self.add(plus(5), server="abcdef-two")
        self.assertIn("More than one", (await self.run_cmd(Services.service_info, service="abcdef")).text)

    async def test_commands_are_staff_only(self):
        non_staff = FakeInteraction()
        non_staff.user = types.SimpleNamespace(id=2)           # not a guild Member: no staff role
        self.assertFalse(await self.cog.interaction_check(non_staff))
        self.assertIn("staff", non_staff.text)
        await self.add(plus(5))
        self.assertEqual(await self.cog._autocomplete(non_staff, ""), [])          # no client names leak
        with mock.patch.object(svc_mod, "is_staff", lambda user: True):
            choices = await self.cog._autocomplete(FakeInteraction(), "ann")
        self.assertEqual([c.value for c in choices], ["SVC-0001"])


if __name__ == "__main__":
    unittest.main()
