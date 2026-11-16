"""Per-server ticket systems: configuration, numbering, migration and isolation.

Run with: python -m unittest discover -s tests -t .
"""
import os
import sqlite3
import tempfile
import types
import unittest

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_TMP = tempfile.mkdtemp(prefix="zarbot-guilds-")
os.environ.setdefault("INVOICE_BOT_DATA", os.path.join(_TMP, "data.json"))

import discord  # noqa: E402

import config  # noqa: E402
from desk import views  # noqa: E402
from desk.database import Database, ticket_number  # noqa: E402
from desk.tickets import Tickets  # noqa: E402


# --------------------------------------------------------------------------- fakes
class _Perms:
    def __init__(self, manage_channels=False, administrator=False):
        self.manage_channels = manage_channels
        self.administrator = administrator


class _Role:
    def __init__(self, role_id, name="Role"):
        self.id, self.name = role_id, name


class _Member:
    def __init__(self, member_id=7, roles=(), administrator=False, manage_channels=False):
        self.id = member_id
        self.roles = list(roles)
        self.guild_permissions = _Perms(manage_channels, administrator)
        self.display_name = f"user{member_id}"
        self.mention = f"<@{member_id}>"


class _Category(discord.CategoryChannel):
    """A CategoryChannel without a gateway, so isinstance() checks pass."""

    def __init__(self, category_id, name):
        self.id, self.name = category_id, name

    def permissions_for(self, member):
        return _Perms(manage_channels=True)


class _Guild:
    def __init__(self, guild_id, roles=(), channels=()):
        self.id = guild_id
        self.name = f"server-{guild_id}"
        self.default_role = _Role(0, "@everyone")
        self._roles = {role.id: role for role in roles}
        self._channels = {channel.id: channel for channel in channels}
        self.members = {}
        self.me = _Member(999)

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, member_id):
        return self.members.get(member_id)


def _server_config(guild_id, billing_category, support_category, billing_role, support_role=None):
    return config._parse_guild_tickets(guild_id, {
        "name": f"server-{guild_id}",
        "staff_role_ids": [],
        "categories": {"tickets": 0, "billing": billing_category, "support": support_category},
        "department_roles": {"billing": [billing_role],
                             **({"support": [support_role]} if support_role else {})},
    })


class _InjectedConfig(unittest.TestCase):
    """Give the module two configured servers for one test, then restore it."""

    guild_a, guild_b, guild_c = 11, 22, 33

    def setUp(self):
        super().setUp()
        self._saved = dict(config.TICKET_GUILDS)
        config.TICKET_GUILDS.clear()
        config.TICKET_GUILDS[self.guild_a] = _server_config(self.guild_a, 111, 112, 1101)
        config.TICKET_GUILDS[self.guild_b] = _server_config(self.guild_b, 221, 222, 2201)
        self.cfg_a = config.tickets_for(self.guild_a)
        self.cfg_b = config.tickets_for(self.guild_b)

    def tearDown(self):
        config.TICKET_GUILDS.clear()
        config.TICKET_GUILDS.update(self._saved)
        super().tearDown()


# --------------------------------------------------------------------------- config
class PerServerConfigTests(_InjectedConfig):
    def test_each_server_has_its_own_categories_and_roles(self):
        self.assertEqual(self.cfg_a.category_id("billing"), 111)
        self.assertEqual(self.cfg_b.category_id("billing"), 221)
        self.assertEqual(self.cfg_a.roles("billing"), [1101])
        self.assertEqual(self.cfg_b.roles("billing"), [2201])
        # Server B's category is not a category of server A.
        self.assertIsNone(self.cfg_a.category_key(221))
        self.assertIsNone(self.cfg_b.category_key(111))

    def test_an_unlisted_server_has_no_ticket_setup(self):
        cfg = config.tickets_for(99)
        self.assertFalse(cfg.configured)
        self.assertEqual(cfg.categories, {})
        self.assertFalse(cfg.is_ticket_staff(_Member(roles=[_Role(1101)])))

    def test_staff_of_one_server_are_not_staff_of_the_other(self):
        billing_a = _Member(7, roles=[_Role(1101)])
        self.assertTrue(config.tickets_for(self.guild_a).is_ticket_staff(billing_a))
        self.assertFalse(config.tickets_for(self.guild_b).is_ticket_staff(billing_a))

    def test_overwrites_only_grant_roles_configured_for_that_server(self):
        guild_a = _Guild(self.guild_a, roles=[_Role(1101, "A billing"), _Role(2201, "B billing")])
        overwrites = Tickets._overwrites(guild_a, 7, department="billing", cfg=self.cfg_a)
        granted = {role.id for role in overwrites if isinstance(role, _Role)}
        self.assertIn(1101, granted)
        self.assertNotIn(2201, granted)  # B's billing role never grants access in A

        guild_b = _Guild(self.guild_b, roles=[_Role(2201, "B billing")])
        overwrites = Tickets._overwrites(guild_b, 7, department="billing", cfg=self.cfg_b)
        granted = {role.id for role in overwrites if isinstance(role, _Role)}
        self.assertIn(2201, granted)  # B's own billing staff

    def test_setup_reports_ids_that_belong_to_another_server(self):
        guild_a = _Guild(self.guild_a, roles=[_Role(1101)],
                         channels=[_Category(111, "Billing"), _Category(112, "Support")])
        stub = types.SimpleNamespace(unconfigured_message=Tickets.unconfigured_message)
        issues = Tickets.config_issues(stub, guild_a, self.cfg_a)
        self.assertEqual(issues, [])  # everything resolves inside server A

        # The same config evaluated against server B: A's ids are not there.
        guild_b = _Guild(self.guild_b, roles=[_Role(2201)],
                         channels=[_Category(221, "Billing"), _Category(222, "Support")])
        issues = Tickets.config_issues(stub, guild_b, self.cfg_a)
        self.assertTrue(any("111" in issue and "not in this server" in issue for issue in issues))
        self.assertTrue(any("1101" in issue for issue in issues))

    def test_unconfigured_server_gets_a_setup_message(self):
        message = Tickets.unconfigured_message(types.SimpleNamespace(id=99), config.tickets_for(99))
        self.assertIn("99", message)
        self.assertIn("ticket_guilds.json", message)

    def test_the_setup_command_exists(self):
        self.assertEqual(Tickets.setup_cmd.name, "setup")


# --------------------------------------------------------------------------- database
class _DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = dict(config.TICKET_GUILDS)
        config.TICKET_GUILDS.clear()
        config.TICKET_GUILDS[11] = _server_config(11, 111, 112, 1101)
        config.TICKET_GUILDS[22] = _server_config(22, 221, 222, 2201)
        self.path = os.path.join(tempfile.mkdtemp(prefix="zarbot-db-"), "bot.db")
        self.db = Database(self.path)
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        config.TICKET_GUILDS.clear()
        config.TICKET_GUILDS.update(self._saved)


class PerServerNumberingTests(_DatabaseCase):
    async def _ticket(self, guild_id, channel_id, subject="ticket"):
        return await self.db.create_ticket(
            1, subject=subject, kind="support", priority="normal", client_discord_id=7,
            client_name="client", channel_id=channel_id, guild_id=guild_id)

    async def test_numbers_restart_in_every_server(self):
        a1 = await self._ticket(11, 1001, "a1")
        b1 = await self._ticket(22, 2001, "b1")
        a2 = await self._ticket(11, 1002, "a2")
        b2 = await self._ticket(22, 2002, "b2")

        self.assertEqual(ticket_number(a1), "ticket-0001")
        self.assertEqual(ticket_number(b1), "ticket-0001")
        self.assertEqual(ticket_number(a2), "ticket-0002")
        self.assertEqual(ticket_number(b2), "ticket-0002")

        row = await self.db.get_ticket(a2)
        self.assertEqual((row["guild_id"], row["guild_seq"]), (11, 2))

    async def test_rows_and_history_carry_the_guild(self):
        ticket_id = await self._ticket(11, 1001)
        note_id = await self.db.add_ticket_note(1, ticket_id, "internal note")
        note = await self.db.fetchone("SELECT guild_id FROM ticket_notes WHERE id=?", (note_id,))
        self.assertEqual(note["guild_id"], 11)
        audit = await self.db.fetchone(
            "SELECT guild_id, entity FROM audit_log WHERE action='ticket.note' ORDER BY id DESC LIMIT 1")
        self.assertEqual((audit["guild_id"], audit["entity"]), (11, "ticket"))

    async def test_lists_and_totals_are_scoped_to_one_server(self):
        await self._ticket(11, 1001, "a1")
        await self._ticket(22, 2001, "b1")
        await self._ticket(22, 2002, "b2")

        self.assertEqual(len(await self.db.list_tickets("all", guild_id=11)), 1)
        self.assertEqual(len(await self.db.list_tickets("all", guild_id=22)), 2)
        self.assertEqual((await self.db.ticket_stats(11))["open"], 1)
        self.assertEqual((await self.db.ticket_stats(22))["open"], 2)
        self.assertEqual(await self.db.count_open_tickets(7, 11), 1)

    async def test_old_ticket_rows_are_attached_to_their_server_once_migrated(self):
        await self.db.close()
        # Make the file look like it was written before multi-server support.
        legacy = sqlite3.connect(self.path)
        legacy.execute("DROP INDEX IF EXISTS idx_tickets_guild")
        legacy.execute("ALTER TABLE tickets DROP COLUMN guild_id")
        legacy.execute("ALTER TABLE tickets DROP COLUMN guild_seq")
        for subject, channel_id in (("old-a", 5001), ("old-b", 5002)):
            legacy.execute(
                "INSERT INTO tickets(subject, kind, client_name, channel_id, opened_by) "
                "VALUES (?, 'support', 'client', ?, 1)", (subject, channel_id))
        legacy.commit()
        legacy.close()

        self.db = Database(self.path)
        await self.db.connect()
        columns = {row["name"] for row in await self.db.fetchall("PRAGMA table_info(tickets)")}
        self.assertIn("guild_id", columns)
        self.assertIn("guild_seq", columns)

        moved = await self.db.assign_ticket_guilds(lambda cid: {5001: 11, 5002: 22}.get(cid), None)
        self.assertEqual(moved, 2)
        rows = {row["subject"]: dict(row) for row in await self.db.fetchall("SELECT * FROM tickets")}
        self.assertEqual(rows["old-a"]["guild_id"], 11)
        self.assertEqual(rows["old-b"]["guild_id"], 22)
        self.assertEqual(ticket_number(rows["old-a"]["id"]), "ticket-0001")
        self.assertEqual(ticket_number(rows["old-b"]["id"]), "ticket-0001")
        # The migrated records are still there, just filed under their server.
        self.assertEqual(len(await self.db.list_tickets("all", guild_id=11)), 1)


# --------------------------------------------------------------------------- panel
class PanelContentTests(_InjectedConfig):
    """Every server shows its own wording on the public ticket panel."""

    def _cfg(self, guild_id, **panel):
        return config._parse_guild_tickets(guild_id, {"categories": {"tickets": guild_id}, **panel})

    @staticmethod
    def _render(view):
        texts, labels = [], []

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == 10:
                    texts.append(node.get("content", ""))
                if node.get("type") == 2 and node.get("label"):
                    labels.append(node["label"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(view.to_components())
        return "\n".join(texts), labels

    def test_each_server_parses_its_own_panel_wording(self):
        a = self._cfg(11, panel_title="Server A help", panel_description="A only",
                      panel_button="Get help")
        b = self._cfg(22)
        self.assertEqual((a.panel_title, a.panel_description, a.panel_button),
                         ("Server A help", "A only", "Get help"))
        # A server without panel_* keys keeps the default wording.
        self.assertEqual((b.panel_title, b.panel_description, b.panel_button),
                         (config.TICKET_PANEL_TITLE, config.TICKET_PANEL_DESCRIPTION,
                          config.TICKET_PANEL_BUTTON))

    def test_the_panel_view_shows_the_wording_of_its_server(self):
        cfg = self._cfg(11, panel_title="Server A help", panel_description="A only",
                        panel_button="Get help")
        text, labels = self._render(views.TicketOpenPanelView(object(), cfg))
        self.assertIn("Server A help", text)
        self.assertIn("A only", text)
        self.assertIn("Get help", labels)

        other = self._cfg(22, panel_title="Server B help", panel_description="B only")
        text, labels = self._render(views.TicketOpenPanelView(object(), other))
        self.assertIn("Server B help", text)
        self.assertNotIn("Server A help", text)
        self.assertIn(config.TICKET_PANEL_BUTTON, labels)

    def test_the_panel_view_without_config_keeps_working(self):
        view = views.TicketOpenPanelView(object())
        text, _labels = self._render(view)
        self.assertIn(config.TICKET_PANEL_TITLE, text)
        self.assertIn("ticketopen:create", {node.get("custom_id") for node in _walk(view)})

    def test_the_panel_command_takes_per_server_wording(self):
        self.assertEqual(Tickets.panel.name, "panel")
        params = {p.name for p in Tickets.panel.parameters}
        self.assertLessEqual({"channel", "title", "description", "button"}, params)


def _walk(view):
    out = []

    def walk(node):
        if isinstance(node, dict):
            out.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(view.to_components())
    return out


if __name__ == "__main__":
    unittest.main()
