"""Deleting tickets: /ticket delete and the dashboard button."""
import os as _os
import tempfile as _tempfile

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_os.environ.setdefault("INVOICE_BOT_DATA", _os.path.join(_tempfile.mkdtemp(prefix="zarbot-ledger-"), "data.json"))

import asyncio  # noqa: E402
import datetime as dt  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
import warnings  # noqa: E402
from unittest import mock  # noqa: E402

import discord  # noqa: E402

import config  # noqa: E402
from desk.database import Database, ticket_number  # noqa: E402
from desk.tickets import Tickets  # noqa: E402
from desk.views import DeleteTicketModal, TicketPanelView  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)
GUILD = 10


def http_error(cls, status=500):
    return cls(types.SimpleNamespace(status=status, reason="x"), "boom")


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


class Staff:
    def __init__(self, uid=1, admin=True):
        self.id, self.mention, self.roles = uid, f"<@{uid}>", []
        self.guild_permissions = types.SimpleNamespace(administrator=admin, manage_channels=False)

    def __str__(self):
        return "Staff#0001"


class FakeMessage:
    def __init__(self, text):
        self.clean_content, self.attachments, self.author = text, [], Staff(5)
        self.created_at = dt.datetime(2026, 11, 5, 10, 0)


class FakeChannel:
    category_id = None

    def __init__(self, cid, messages=("hello", "my vps is ready"), history_error=None, delete_error=None):
        self.id, self.guild = cid, types.SimpleNamespace(id=GUILD)
        self.messages, self.history_error, self.delete_error = list(messages), history_error, delete_error
        self.deleted_reason, self.sent = None, []

    async def history(self, limit=100, oldest_first=False):
        for text in self.messages:
            yield FakeMessage(text)
        if self.history_error:
            raise self.history_error

    async def delete(self, reason=None):
        if self.delete_error:
            raise self.delete_error
        self.deleted_reason = reason

    async def send(self, **kw):
        self.sent.append(kw)


class FakeBot:
    def __init__(self, db):
        self.db, self.user, self.channels = db, None, {}
        self.gone = set()

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        raise http_error(discord.NotFound, 404)

    async def wait_until_ready(self):
        await asyncio.Event().wait()


class FakeInteraction:
    def __init__(self, bot, user=None, channel=None):
        self.client, self.user, self.channel = bot, user or Staff(), channel
        self.guild = types.SimpleNamespace(id=GUILD)
        self.sent, self.modals, self._done = [], [], False
        self.response = types.SimpleNamespace(defer=self._defer, is_done=lambda: self._done,
                                              send_message=self._send, send_modal=self._modal)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self, **kw):
        self._done = True

    async def _send(self, *a, **kw):
        self._done = True
        self.sent.append(kw["view"])

    async def _modal(self, modal):
        self._done = True
        self.modals.append(modal)

    @property
    def text(self):
        return "\n".join(text_of(v) for v in self.sent)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-tdel-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.object(config, "TICKET_TRANSCRIPT_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = Database(_os.path.join(self.dir, "t.db"))
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.bot = FakeBot(self.db)
        self.service = Tickets(self.bot)
        self.addCleanup(self.service.stale_digest.cancel)
        self.logged = mock.AsyncMock()
        self.service._log = self.logged
        self.guild = types.SimpleNamespace(id=GUILD)
        self.channel = self.bot.channels[7001] = FakeChannel(7001)
        self.ticket_id = await self.db.create_ticket(
            1, subject="VPS order", kind="purchase", priority="normal", client_discord_id=2, client_name="Cat",
            channel_id=7001, guild_id=GUILD)

    async def delete(self, reason="Duplicate ticket", user=None):
        return await self.service.delete_ticket(self.guild, self.ticket_id, user or Staff(), reason)

    async def row(self):
        return await self.db.get_ticket(self.ticket_id)


class DeleteTicketTests(Base):
    async def test_deleting_saves_the_transcript_deletes_the_channel_and_keeps_the_record(self):
        row = await self.delete()
        self.assertIn(f"{ticket_number(self.ticket_id)} deleted by Staff#0001", self.channel.deleted_reason)
        self.assertEqual(row["status"], "closed")
        self.assertIn("Deleted by Staff#0001: Duplicate ticket", row["close_reason"])
        with open(row["transcript_path"], encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("my vps is ready", body)                       # the conversation survives
        self.assertEqual((await self.db.ticket_by_channel(7001))["id"], self.ticket_id)  # record still there
        audit = [a for a in await self.db.recent_audit(20) if a["action"] == "ticket.delete"]
        self.assertEqual(len(audit), 1)
        self.assertIn("Duplicate ticket", audit[0]["details"])
        title, body_text, transcript = self.logged.await_args.args[:3]
        self.assertIn("Ticket deleted", title)
        self.assertEqual(transcript, row["transcript_path"])
        self.assertIn("Duplicate ticket", body_text)

    async def test_a_closed_ticket_can_be_deleted_too(self):
        await self.db.set_ticket_status(1, self.ticket_id, "closed", "done")
        row = await self.delete(reason="")
        self.assertIsNotNone(self.channel.deleted_reason)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["close_reason"], "Deleted by Staff#0001")

    async def test_nothing_is_deleted_when_the_transcript_is_incomplete(self):
        self.channel.history_error = http_error(discord.HTTPException)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            await self.delete()
        self.assertIsNone(self.channel.deleted_reason)
        self.assertEqual((await self.row())["status"], "open")
        self.assertEqual(self.logged.await_count, 0)

    async def test_missing_permission_leaves_everything_as_it_was(self):
        self.channel.delete_error = http_error(discord.Forbidden, 403)
        with self.assertRaisesRegex(ValueError, "Manage Channels"):
            await self.delete()
        self.assertEqual((await self.row())["status"], "open")
        self.assertEqual([a for a in await self.db.recent_audit(20) if a["action"] == "ticket.delete"], [])

    async def test_a_channel_that_is_already_gone_is_not_an_error(self):
        del self.bot.channels[7001]                                   # fetch_channel says NotFound
        row = await self.delete()
        self.assertEqual(row["status"], "closed")
        self.assertIn("Deleted by", row["close_reason"])

    async def test_other_departments_cannot_delete(self):
        with self.assertRaisesRegex(ValueError, "another department"):
            await self.delete(user=Staff(9, admin=False))
        self.assertIsNone(self.channel.deleted_reason)
        self.assertEqual((await self.row())["status"], "open")

    async def test_unknown_ticket_and_double_delete(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            await self.service.delete_ticket(self.guild, 999, Staff(), "")
        self.service._deleting.add(self.ticket_id)
        with self.assertRaisesRegex(ValueError, "already being deleted"):
            await self.delete()

    async def test_the_channel_deleted_listener_does_not_log_a_second_closure(self):
        await self.delete()
        self.logged.reset_mock()
        await self.service.on_guild_channel_delete(self.channel)      # Discord's own event arrives afterwards
        self.assertEqual(self.logged.await_count, 0)
        fresh = await self.db.create_ticket(1, subject="x", kind="support", priority="normal", client_discord_id=3,
                                            client_name="D", channel_id=7002, guild_id=GUILD)
        self.service._deleting.add(fresh)                             # mid-delete: stay out of the way
        await self.service.on_guild_channel_delete(FakeChannel(7002))
        self.assertEqual((await self.db.get_ticket(fresh))["status"], "open")
        self.service._deleting.discard(fresh)
        await self.service.on_guild_channel_delete(FakeChannel(7002))  # a manual delete is still recorded
        self.assertEqual((await self.db.get_ticket(fresh))["status"], "closed")


class ConfirmationTests(Base):
    async def submit(self, confirm, reason="dup"):
        modal = DeleteTicketModal(self.ticket_id)
        modal.confirm._value, modal.reason._value = confirm, reason
        interaction = FakeInteraction(self.bot)
        self.bot.tickets = self.service
        await modal.on_submit(interaction)
        return interaction

    async def test_wrong_confirmation_deletes_nothing(self):
        for typed in ("", "yes", "delet"):
            done = await self.submit(typed)
            self.assertIn("Not deleted", done.text)
        self.assertIsNone(self.channel.deleted_reason)
        self.assertEqual((await self.row())["status"], "open")

    async def test_typing_delete_in_any_case_deletes(self):
        done = await self.submit(" delete ")
        self.assertIn("deleted", done.text)
        self.assertIn("transcript", done.text)
        self.assertIsNotNone(self.channel.deleted_reason)
        self.assertEqual((await self.row())["status"], "closed")

    async def test_failures_are_reported_to_the_staff_member(self):
        self.channel.delete_error = http_error(discord.Forbidden, 403)
        done = await self.submit("DELETE")
        self.assertIn("❌", done.text)
        self.assertIn("Manage Channels", done.text)

    async def test_the_command_opens_the_confirmation_for_permitted_staff_only(self):
        interaction = FakeInteraction(self.bot, channel=self.channel)
        await Tickets.delete_cmd.callback(self.service, interaction, self.ticket_id)
        self.assertIsInstance(interaction.modals[0], DeleteTicketModal)
        self.assertEqual(interaction.modals[0].ticket_id, self.ticket_id)
        self.assertIsNone(self.channel.deleted_reason)                # asking is not deleting
        stranger = FakeInteraction(self.bot, user=Staff(9, admin=False), channel=self.channel)
        await Tickets.delete_cmd.callback(self.service, stranger, self.ticket_id)
        self.assertEqual(stranger.modals, [])
        self.assertIn("another department", stranger.text)
        missing = FakeInteraction(self.bot, channel=self.channel)
        await Tickets.delete_cmd.callback(self.service, missing, 999)
        self.assertEqual(missing.modals, [])


class DashboardButtonTests(Base):
    def components(self, ticket):
        view = TicketPanelView(self.bot, ticket)
        found = {}

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == 2:
                    found[node["custom_id"]] = node
                if node.get("type") == 1:
                    self.assertLessEqual(len(node["components"]), 5)   # Discord's limit per row
                for child in node.get("components", []) or []:
                    walk(child)
        for top in view.to_components():
            walk(top)
        return view, found

    async def test_dashboard_has_a_delete_button_for_open_and_closed_tickets(self):
        for status in ("open", "closed"):
            ticket = {**await self.row(), "status": status}
            _, buttons = self.components(ticket)
            button = buttons[f"ticket:delete:{self.ticket_id}"]
            self.assertEqual(button["style"], 4)                       # danger (red)
            self.assertFalse(button.get("disabled"))

    async def test_clicking_delete_opens_the_confirmation_for_staff_and_is_blocked_for_others(self):
        view, _ = self.components(await self.row())
        view._staff = mock.AsyncMock(return_value=True)
        interaction = FakeInteraction(self.bot)
        await view.on_delete(interaction)
        self.assertIsInstance(interaction.modals[0], DeleteTicketModal)
        view._staff = mock.AsyncMock(return_value=False)
        blocked = FakeInteraction(self.bot)
        await view.on_delete(blocked)
        self.assertEqual(blocked.modals, [])


class PanelUpgradeTests(Base):
    async def test_open_tickets_get_their_dashboard_refreshed_once_after_startup(self):
        closed = await self.db.create_ticket(1, subject="old", kind="support", priority="normal", client_discord_id=3,
                                             client_name="D", channel_id=7002, guild_id=GUILD)
        await self.db.set_ticket_status(1, closed, "closed", "done")
        self.service.refresh_dashboard = mock.AsyncMock(return_value=True)
        with mock.patch("desk.tickets.PANEL_UPGRADE_DELAY", 0):
            await self.service._upgrade_panels()
            await self.service._upgrade_panels()                      # reconnects must not repeat it
        self.assertEqual([c.args[0] for c in self.service.refresh_dashboard.await_args_list], [self.ticket_id])

    async def test_a_failing_refresh_is_logged_not_raised(self):
        self.service.refresh_dashboard = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with mock.patch("desk.tickets.PANEL_UPGRADE_DELAY", 0), self.assertLogs("invoice-bot.tickets", "ERROR"):
            await self.service._upgrade_panels()


if __name__ == "__main__":
    unittest.main()
