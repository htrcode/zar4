"""Department-scoped ticket access, the ticket move flow and the /revenue fields.

Run with: python -m unittest discover -s tests -t .
"""
import json
import os
import shutil
import tempfile
import types
import unittest

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_TMP = tempfile.mkdtemp(prefix="zarbot-departments-")
os.environ.setdefault("INVOICE_BOT_DATA", os.path.join(_TMP, "data.json"))

import discord  # noqa: E402

import config  # noqa: E402
import ui  # noqa: E402
from desk import views  # noqa: E402
from desk.database import Database  # noqa: E402
from desk import pdf_gen  # noqa: E402
from desk.pdf_gen import pnl_pdf  # noqa: E402
from desk.tickets import Tickets  # noqa: E402


# --------------------------------------------------------------------------- fakes
class _Perms:
    def __init__(self, manage_channels=True, administrator=False):
        self.manage_channels = manage_channels
        self.administrator = administrator


class _Role:
    def __init__(self, role_id, name="Role"):
        self.id, self.name = role_id, name


class _Member:
    def __init__(self, member_id, roles=(), administrator=False, manage_channels=False):
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
        return _Perms(True)


class _Channel:
    def __init__(self, channel_id=500, category=None, name="ticket-0001"):
        self.id, self.name, self.category = channel_id, name, category
        self.category_id = getattr(category, "id", None)
        self.edits = []

    def permissions_for(self, member):
        return _Perms(True)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "category" in kwargs:
            self.category, self.category_id = kwargs["category"], kwargs["category"].id
        if "name" in kwargs:
            self.name = kwargs["name"]


class _Guild:
    def __init__(self, roles=(), channels=()):
        self.id = 1
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


class _Response:
    """Interaction response that records modals/messages and can refuse a modal."""

    def __init__(self, allow_modal=True):
        self.allow_modal, self.modals, self.messages = allow_modal, [], []

    def is_done(self):
        return bool(self.modals or self.messages)

    async def send_modal(self, modal):
        if not self.allow_modal:
            raise RuntimeError("Discord refused the modal")
        self.modals.append(modal)

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class _Interaction:
    def __init__(self, channel, user, guild=None, response=None, client=None):
        self.channel, self.user, self.guild = channel, user, guild
        self.channel_id = channel.id
        self.response = response or _Response()
        self.client = client or types.SimpleNamespace(tickets=None)


class _ExplodingDB:
    """Any database call fails the test: a dashboard button must answer first."""

    def __getattr__(self, name):
        raise AssertionError(f"the control must not touch the database (tried {name})")


def _custom_ids(components):
    for component in components or ():
        if isinstance(component, dict):
            if component.get("custom_id"):
                yield component["custom_id"]
            yield from _custom_ids(component.get("components"))


class _Departments:
    """Mixin: give two categories their own staff roles for one test, then restore them."""

    billing_id, support_id, manager_id = 11, 22, 33

    def setUp(self):
        super().setUp()
        self._saved = (dict(config.TICKET_DEPARTMENT_ROLE_IDS),
                       list(config.ALL_DEPARTMENT_ROLE_IDS),
                       list(config.TICKET_CROSS_DEPARTMENT_ROLE_IDS),
                       dict(config.TICKET_CATEGORIES))
        config.TICKET_DEPARTMENT_ROLE_IDS.clear()
        config.TICKET_DEPARTMENT_ROLE_IDS.update({"billing": [self.billing_id],
                                                 "support": [self.support_id]})
        config.ALL_DEPARTMENT_ROLE_IDS[:] = [self.billing_id, self.support_id]
        config.TICKET_CROSS_DEPARTMENT_ROLE_IDS[:] = [self.manager_id]
        config.TICKET_CATEGORIES.update({
            "billing": ("Billing", "TICKET_CATEGORY_ID", 111),
            "support": ("Technical Support", "TICKET_SUPPORT_CATEGORY_ID", 222),
        })
        self.billing_role = _Role(self.billing_id, "Billing")
        self.support_role = _Role(self.support_id, "Technical Support")
        self.manager_role = _Role(self.manager_id, "Manager")

    def tearDown(self):
        roles, all_roles, cross, categories = self._saved
        config.TICKET_DEPARTMENT_ROLE_IDS.clear()
        config.TICKET_DEPARTMENT_ROLE_IDS.update(roles)
        config.ALL_DEPARTMENT_ROLE_IDS[:] = all_roles
        config.TICKET_CROSS_DEPARTMENT_ROLE_IDS[:] = cross
        config.TICKET_CATEGORIES.clear()
        config.TICKET_CATEGORIES.update(categories)
        super().tearDown()


# --------------------------------------------------------------------------- access
class OverwriteTests(_Departments, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.guild = _Guild([self.billing_role, self.support_role, self.manager_role])
        self.owner = _Member(7)
        self.guild.members[7] = self.owner

    def test_move_grants_the_destination_and_locks_out_the_previous_department(self):
        overwrites = Tickets._overwrites(self.guild, 7, department="support",
                                        drop_roles=[self.billing_id])
        self.assertTrue(overwrites[self.support_role].view_channel)
        self.assertTrue(overwrites[self.support_role].send_messages)
        self.assertFalse(overwrites[self.billing_role].view_channel)          # previous department
        self.assertFalse(overwrites[self.billing_role].read_message_history)  # cannot read either
        self.assertTrue(overwrites[self.owner].view_channel)                  # ticket owner
        self.assertTrue(overwrites[self.guild.me].view_channel)               # the bot
        self.assertFalse(overwrites[self.guild.default_role].view_channel)

    def test_cross_department_staff_are_never_removed(self):
        overwrites = Tickets._overwrites(self.guild, 7, department="support",
                                        drop_roles=[self.billing_id, self.manager_id])
        self.assertTrue(overwrites[self.manager_role].view_channel)
        self.assertTrue(overwrites[self.manager_role].send_messages)
        self.assertFalse(overwrites[self.billing_role].view_channel)

    def test_a_closed_ticket_stays_readable_for_its_department(self):
        overwrites = Tickets._overwrites(self.guild, 7, closed=True, department="billing")
        self.assertFalse(overwrites[self.owner].send_messages)
        self.assertTrue(overwrites[self.billing_role].read_message_history)

    def test_an_unconfigured_category_grants_no_department(self):
        overwrites = Tickets._overwrites(self.guild, 7)  # no department known for the ticket
        self.assertTrue(overwrites[self.guild.me].view_channel)
        self.assertFalse(overwrites[self.guild.default_role].view_channel)
        self.assertNotIn(self.billing_role, overwrites)
        self.assertNotIn(self.support_role, overwrites)


class CanManageTests(_Departments, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service = Tickets.__new__(Tickets)

    def test_department_staff_are_scoped_to_their_own_category(self):
        billing = _Member(1, [self.billing_role])
        self.assertTrue(self.service.can_manage(billing, None, "billing"))
        self.assertFalse(self.service.can_manage(billing, None, "support"))
        self.assertTrue(self.service.can_manage(_Member(2, [self.support_role]), None, "support"))

    def test_admins_and_cross_department_roles_always_pass(self):
        self.assertTrue(self.service.can_manage(_Member(3, administrator=True), None, "billing"))
        self.assertTrue(self.service.can_manage(_Member(4, [self.manager_role]), None, "support"))

    def test_unrelated_member_is_refused(self):
        self.assertFalse(self.service.can_manage(_Member(5, [_Role(404)]), None, "support"))
        self.assertFalse(self.service.can_manage(object(), None, "support"))

    def test_ticket_outside_every_department_is_open_to_any_department_staff(self):
        self.assertTrue(self.service.can_manage(_Member(6, [self.billing_role]), None, None))


# --------------------------------------------------------------------------- moving
class MoveTests(_Departments, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-move-")
        self.db = Database(os.path.join(self.dir, "move.db"))
        await self.db.connect()
        self.billing_cat = _Category(111, "Billing")
        self.support_cat = _Category(222, "Technical Support")
        self.channel = _Channel(category=self.billing_cat)
        self.guild = _Guild([self.billing_role, self.support_role, self.manager_role],
                            [self.billing_cat, self.support_cat])
        self.service = Tickets.__new__(Tickets)
        self.service.bot = types.SimpleNamespace(db=self.db, get_channel=lambda cid: self.channel)
        self.service._moving, self.service._moved = set(), {}
        self.refreshes = []
        self.service.request_refresh = self.refreshes.append

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _ticket(self):
        return await self.db.create_ticket(
            1, subject="Need a VPS", kind="support", priority="normal",
            client_discord_id=7, client_name="Bibi", channel_id=self.channel.id)

    async def test_a_move_without_a_reason_is_refused(self):
        ticket_id = await self._ticket()
        with self.assertRaises(ValueError) as ctx:
            await self.service.move_ticket(self.guild, ticket_id, _Member(9), "support", "   ")
        self.assertIn("reason", str(ctx.exception).lower())
        self.assertEqual(self.channel.edits, [])   # nothing was changed
        self.assertEqual(self.refreshes, [])

    async def test_move_hands_the_ticket_to_the_destination_department(self):
        ticket_id = await self._ticket()
        actor = _Member(9, [self.support_role], manage_channels=True)
        result = await self.service.move_ticket(self.guild, ticket_id, actor, "support",
                                               "Client paid in full", "support-0001-bibi")

        edit = self.channel.edits[-1]
        self.assertEqual(edit["category"], self.support_cat)
        self.assertEqual(edit["name"], "support-0001-bibi")
        self.assertIn("Client paid in full", edit["reason"])
        self.assertTrue(edit["overwrites"][self.support_role].view_channel)
        self.assertFalse(edit["overwrites"][self.billing_role].view_channel)

        note = (await self.db.ticket_notes(ticket_id))[0]["note"]
        self.assertIn("reason: Client paid in full", note)   # timestamped dashboard note
        self.assertIn("user9", note)                         # ... naming the staff member
        self.assertIn("Billing", note)
        self.assertIn("Technical Support", note)

        moves = [row for row in await self.db.recent_audit(10) if row["action"] == "ticket.move"]
        details = json.loads(moves[0]["details"])
        self.assertEqual(details["reason"], "Client paid in full")
        self.assertEqual(details["by"], "user9")
        self.assertEqual(details["permissions"]["department"], "support")

        self.assertEqual(self.refreshes, [ticket_id])        # dashboard refreshed
        self.assertIn("Technical Support", result)           # and the staff member is told


# --------------------------------------------------------------------------- /revenue
class RevenueStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-revenue-")
        self.db = Database(os.path.join(self.dir, "revenue.db"))
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_revenue_records_every_field_the_command_collects(self):
        revenue_id = await self.db.add_revenue(
            1, "zarbunex", "Commission", 500000, "Hosting referral commission", "2026-10-05",
            received_from="Acme Ltd", method="upi", transaction_id="UTR-9911",
            remarks="part of the Q3 referral deal", recorded_by="Wixz")
        stored = await self.db.get_revenue(revenue_id)
        self.assertEqual(stored["company_id"], "zarbunex")
        self.assertEqual(stored["category"], "Commission")
        self.assertEqual(stored["amount"], 500000)
        self.assertEqual(stored["occurred_on"], "2026-10-05")
        self.assertEqual(stored["received_from"], "Acme Ltd")
        self.assertEqual(stored["method"], "upi")
        self.assertEqual(stored["transaction_id"], "UTR-9911")
        self.assertEqual(stored["remarks"], "part of the Q3 referral deal")
        self.assertEqual(stored["recorded_by"], "Wixz")

    async def test_revenue_reaches_its_company_pnl_for_its_own_month(self):
        await self.db.add_revenue(1, "zarbunex", "Commission", 500000, "Referral", "2026-10-05")
        await self.db.add_revenue(1, "zarbunex", "General", 100000, "Panel resale", "2026-10-06")
        await self.db.add_revenue(1, "hectorhost", "General", 20000, "Misc", "2026-10-07")

        report = await self.db.month_report("2026-10", "zarbunex")
        self.assertEqual(report["income_total"], 0)
        self.assertEqual(report["statement_total"], 600000)
        self.assertEqual(report["revenue"], 600000)          # in the P&L revenue total
        self.assertEqual(report["net"], 600000)
        self.assertEqual(dict(report["statement_rows"])["Commission"], 500000)
        self.assertEqual(dict(report["statement_rows"])["General"], 100000)
        self.assertTrue(pnl_pdf(report).startswith(b"%PDF"))

        # Another company, and another month, stay untouched.
        self.assertEqual((await self.db.month_report("2026-10", "matrixmc"))["revenue"], 0)
        self.assertEqual((await self.db.month_report("2026-09", "zarbunex"))["revenue"], 0)
        self.assertEqual((await self.db.month_report("2026-10", "hectorhost"))["revenue"], 20000)
        self.assertEqual((await self.db.month_report("2026-10"))["revenue"], 620000)

    async def test_investment_is_neither_revenue_nor_profit(self):
        await self.db.add_revenue(1, "hectorhost", "Investment", 1000000, "Seed capital", "2026-10-06")
        report = await self.db.month_report("2026-10", "hectorhost")
        self.assertEqual(report["investment"], 1000000)
        self.assertEqual(report["revenue"], 0)
        self.assertEqual(report["net"], 0)
        self.assertEqual(dict(report["statement_rows"])["Commission"], 0)
        summary = await self.db.revenue_summary("hectorhost")
        self.assertEqual((summary["operating"], summary["investment"]), (0, 1000000))


class RevenueCardTests(unittest.TestCase):
    def test_the_statement_card_shows_every_field(self):
        record = {"id": 3, "company_id": "zarbunex", "category": "General", "amount": 250000,
                  "description": "Panel resale", "received_from": "Acme Ltd", "method": "upi",
                  "transaction_id": "UTR-9", "remarks": "part payment", "occurred_on": "2026-10-05",
                  "recorded_by": "Wixz", "created_at": "2026-10-05T10:00:00"}
        view = ui.revenue_card(record, {"operating": 250000, "investment": 500000})
        text = "\n".join(c["content"] for c in view.to_components()[0]["components"] if c["type"] == 10)
        for label in ("Transaction ID", "Company", "Category", "Amount", "Description", "Date",
                      "Received From", "Payment Method", "Remarks", "Recorded By"):
            self.assertIn(f"**{label}:**", text)
        self.assertIn("REV-000003", text)
        self.assertIn("₹2,500.00", text)
        self.assertIn("**Total Revenue:**", text)
        self.assertIn("Investment (tracked separately)", text)
        self.assertIn("**Total received (revenue + investment):** ₹7,500.00", text)


class RevenueCommandTests(unittest.TestCase):
    def test_revenue_takes_the_same_fields_as_expense_add(self):
        from cogs.finance import Statements

        cog = Statements(object())
        command = next(c for c in cog.get_app_commands() if c.name == "revenue")
        names = {p.name for p in command.parameters}
        for name in ("company", "category", "amount", "description", "received_from", "method",
                     "transaction_id", "remarks", "date"):
            self.assertIn(name, names)
        required = {p.name for p in command.parameters if p.required}
        self.assertLessEqual({"company", "category", "amount", "description", "received_from"},
                             required)


# --------------------------------------------------------------------------- UI
class FinanceDashboardTests(unittest.TestCase):
    def test_revenue_statements_and_investment_are_their_own_lines(self):
        report = {"month": "2026-10", "company": "zarbunex",
                  "income": [("Hosting Revenue", 100000)], "income_total": 100000,
                  "expenses": [], "statement_rows": [("Commission", 500000), ("General", 0)],
                  "statement_total": 500000, "revenue": 600000, "expense_total": 0, "net": 600000,
                  "revenue_rows": [("Commission", 500000), ("Investment", 1000000), ("General", 0)],
                  "revenue_total": 1500000, "investment": 1000000}
        view = views.DashboardView(object(), report, 0)
        text = "\n".join(c["content"] for c in view.to_components()[0]["components"] if c["type"] == 10)
        self.assertIn("**Operating Revenue:** ₹6,000.00", text)
        self.assertIn("Revenue statements (Commission / General)", text)
        self.assertIn("**Investment (tracked separately):** ₹10,000.00", text)
        self.assertIn("**Total received (revenue + investment):** ₹16,000.00", text)


class TicketDashboardTests(_Departments, unittest.TestCase):
    ticket = {"id": 1, "subject": "Need a VPS", "kind": "purchase", "priority": "normal",
              "status": "open", "client_name": "Acme", "client_discord_id": 5,
              "company_id": "zarbunex", "claimed_by": None, "opened_by": 9, "deal_id": None,
              "created_at": "2026-10-27", "last_activity": "2026-10-28"}

    def test_the_dashboard_hides_the_ticket_type_and_company(self):
        fields, _more = views.ticket_blocks(dict(self.ticket), category="Billing")
        self.assertIn("**Category:**", fields)
        self.assertNotIn("**Type:**", fields)
        self.assertNotIn("**Company:**", fields)

    def test_the_panel_has_move_but_no_complete_purchase(self):
        view = views.TicketPanelView(object(), dict(self.ticket))
        ids = set(_custom_ids(view.to_components()))
        self.assertIn("ticket:move:1", ids)
        self.assertIn("ticket:close:1", ids)
        self.assertNotIn("ticket:complete:1", ids)

    def test_the_dashboard_has_no_sales_pipeline_line(self):
        deal = {"id": 3, "stage": "new", "budget": 0, "value": 0, "next_follow_up": "2026-10-29"}
        _fields, more = views.ticket_blocks(dict(self.ticket), deal)
        self.assertNotIn("**Sales:**", more)
        self.assertNotIn("New enquiry", more)
        self.assertNotIn("follow-up 2026-10-29", more)


class MoveModalTests(_Departments, unittest.TestCase):
    def test_the_form_requires_a_destination_and_a_reason(self):
        channel = _Channel(category=_Category(111, "Billing"))
        modal = views.MoveTicketModal({"id": 4, "channel_id": channel.id}, channel)
        labels = {c["label"]: c["component"] for c in modal.to_components() if c["type"] == 18}
        self.assertEqual(labels["Destination category"]["custom_id"], "move:destination")
        self.assertTrue(labels["Destination category"]["required"])
        self.assertEqual(labels["Reason for moving"]["custom_id"], "move:reason")
        self.assertTrue(labels["Reason for moving"]["required"])
        # Discord rejects a TextInput that also sets its own label inside a Label
        # (50035: "Cannot set label on a TextInput in a Label component").
        for caption in ("Reason for moving", "Channel name (optional)"):
            self.assertIn(caption, labels)
            self.assertNotIn("label", labels[caption])
        self.assertEqual(sorted(o["label"] for o in labels["Destination category"]["options"]),
                         ["Billing", "Technical Support"])
        self.assertEqual([o["label"] for o in labels["Destination category"]["options"]
                          if o.get("description") == "Current category"], ["Billing"])

    def test_submitted_values_reach_the_handler(self):
        channel = _Channel(category=_Category(111, "Billing"))
        modal = views.MoveTicketModal({"id": 4, "channel_id": channel.id}, channel)
        modal._refresh(None, [
            {"type": 18, "component": {"type": 3, "custom_id": "move:destination",
                                       "values": ["support"]}},
            {"type": 18, "component": {"type": 4, "custom_id": "move:reason", "value": "client paid"}},
        ], {})
        self.assertEqual(modal.destination.values, ["support"])
        self.assertEqual(modal.reason.value, "client paid")


# --------------------------------------------------------------------------- move button
class MoveButtonTests(_Departments, unittest.IsolatedAsyncioTestCase):
    """The dashboard's Move button must answer inside Discord's 3 second window."""

    ticket = {"id": 1, "subject": "Need a VPS", "kind": "purchase", "priority": "normal",
              "status": "open", "client_name": "Acme", "client_discord_id": 5,
              "company_id": "zarbunex", "claimed_by": None, "opened_by": 9, "deal_id": None,
              "created_at": "2026-10-27", "last_activity": "2026-10-28", "channel_id": 500}

    def _view(self):
        # No usable database: the button must not need one to open the form.
        bot = types.SimpleNamespace(tickets=types.SimpleNamespace(can_manage=lambda *a: True),
                                    db=_ExplodingDB())
        return views.TicketPanelView(bot, dict(self.ticket))

    async def test_the_form_opens_without_touching_the_database(self):
        interaction = _Interaction(_Channel(category=_Category(111, "Billing")), _Member(9))
        await self._view().on_move(interaction)
        self.assertEqual(len(interaction.response.modals), 1)   # the form is sent
        self.assertEqual(interaction.response.messages, [])      # and nothing failed

    async def test_a_form_that_cannot_open_still_answers(self):
        interaction = _Interaction(_Channel(category=_Category(111, "Billing")), _Member(9),
                                   response=_Response(allow_modal=False))
        with self.assertLogs(views.log, level="ERROR"):   # the failure is logged, not swallowed
            await self._view().on_move(interaction)
        self.assertEqual(interaction.response.modals, [])
        self.assertEqual(len(interaction.response.messages), 1)  # never a silent timeout


# --------------------------------------------------------------------------- P&L
class PnlStatementTests(unittest.TestCase):
    """Investment is visible on the statement and in its totals, but never revenue."""

    report = {"month": "2026-10", "company": "hectorhost",
              "income": [("Hosting Revenue", 100000)], "income_total": 100000,
              "expenses": [("Hosting & servers", 50000)], "expense_total": 50000,
              "statement_rows": [("Commission", 0), ("General", 0)], "statement_total": 0,
              "revenue": 100000, "net": 50000,
              "revenue_rows": [("Commission", 0), ("Investment", 1000000), ("General", 0)],
              "revenue_total": 1000000, "investment": 1000000}

    @staticmethod
    def _walk(item):
        """Text of a story item, including the cells of ReportLab tables."""
        if isinstance(item, str):
            yield item
        elif hasattr(item, "getPlainText"):
            yield item.getPlainText()
        for row in getattr(item, "_cellvalues", None) or ():
            for cell in row:
                yield from PnlStatementTests._walk(cell)

    def _text(self) -> str:
        captured = {}

        def capture(story, *args, **kwargs):
            captured["story"] = story
            return b"%PDF"

        original, pdf_gen._build = pdf_gen._build, capture
        try:
            self.assertTrue(pnl_pdf(dict(self.report)).startswith(b"%PDF"))
        finally:
            pdf_gen._build = original
        return "\n".join(text for item in captured["story"] for text in self._walk(item))

    def test_investment_gets_its_own_section_and_a_total_received_line(self):
        text = self._text()
        self.assertIn("INVESTMENT (TRACKED SEPARATELY)", text)
        self.assertIn("Total investment", text)
        self.assertIn("Total received (revenue + investment)", text)
        self.assertIn(pdf_gen.money(1000000, config.CURRENCY), text)
        self.assertIn(pdf_gen.money(1100000, config.CURRENCY), text)   # revenue + investment

    def test_investment_is_still_kept_out_of_revenue_and_profit(self):
        text = self._text()
        self.assertIn("never in revenue or net profit", text)
        self.assertIn(pdf_gen.money(100000, config.CURRENCY), text)    # Total revenue
        self.assertIn("NET PROFIT", text)


# --------------------------------------------------------------------------- voiding
class RevenueVoidTests(unittest.IsolatedAsyncioTestCase):
    """A wrong Revenue Statement is cancelled, not deleted, and leaves every total."""

    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-revenue-void-")
        self.db = Database(os.path.join(self.dir, "void.db"))
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_voiding_removes_it_from_totals_reports_and_the_pnl(self):
        commission = await self.db.add_revenue(1, "zarbunex", "Commission", 500000, "Referral",
                                               "2026-10-05")
        await self.db.add_revenue(1, "zarbunex", "Investment", 1000000, "Seed capital", "2026-10-06")
        await self.db.add_revenue(1, "hectorhost", "General", 20000, "Misc income", "2026-10-07")

        voided = await self.db.void_revenue(1, commission, "wrong amount, entered twice")
        self.assertEqual(voided["voided"], 1)
        self.assertEqual(voided["void_reason"], "wrong amount, entered twice")
        self.assertTrue(voided["voided_at"])

        report = await self.db.month_report("2026-10", "zarbunex")
        self.assertEqual(report["revenue"], 0)                      # no longer revenue
        self.assertEqual(report["net"], 0)                          # nor profit
        self.assertEqual(dict(report["statement_rows"])["Commission"], 0)
        self.assertEqual(report["investment"], 1000000)             # investment untouched
        self.assertEqual(report["revenue_total"], 1000000)
        self.assertTrue(pnl_pdf(dict(report)).startswith(b"%PDF"))

        summary = await self.db.revenue_summary("zarbunex")
        self.assertEqual((summary["operating"], summary["investment"]), (0, 1000000))
        listed = [r["id"] for r in await self.db.list_revenue("2026-10", "zarbunex")]
        self.assertNotIn(commission, listed)                         # gone from the list
        self.assertEqual(len(listed), 1)                             # the Investment entry stays
        # Another company keeps its own books.
        self.assertEqual((await self.db.month_report("2026-10", "hectorhost"))["revenue"], 20000)

    async def test_the_record_and_its_audit_line_are_kept(self):
        revenue_id = await self.db.add_revenue(1, "zarbunex", "General", 100000, "Panel", "2026-10-05")
        await self.db.void_revenue(1, revenue_id, "entered under the wrong company")
        stored = await self.db.get_revenue(revenue_id)                # never deleted
        self.assertEqual(stored["amount"], 100000)
        self.assertEqual(stored["void_reason"], "entered under the wrong company")
        audit = [r["action"] for r in await self.db.recent_audit(10)]
        self.assertIn("revenue.void", audit)
        listed = await self.db.recent_revenue("zarbunex", include_voided=True)
        self.assertEqual([(r["id"], r["voided"]) for r in listed], [(revenue_id, 1)])

    async def test_voiding_twice_or_without_a_reason_is_refused(self):
        revenue_id = await self.db.add_revenue(1, "zarbunex", "General", 100000, "Panel", "2026-10-05")
        with self.assertRaises(ValueError):
            await self.db.void_revenue(1, revenue_id, "   ")
        with self.assertRaises(ValueError):
            await self.db.void_revenue(1, 999, "typo")
        await self.db.void_revenue(1, revenue_id, "typo")
        with self.assertRaises(ValueError):
            await self.db.void_revenue(1, revenue_id, "typo")


class StatementEntryVoidTests(unittest.IsolatedAsyncioTestCase):
    """Sales/expense statement entries are voided by id too, and stay linked to the books."""

    async def asyncSetUp(self):
        import data as store

        self.store = store
        self.dir = tempfile.mkdtemp(prefix="zarbot-ledger-void-")
        self.path = os.path.join(self.dir, "data.json")
        self.original = store.DATA_FILE
        store.DATA_FILE = self.path
        self.db = Database(os.path.join(self.dir, "ledger.db"))
        await self.db.connect()
        self.bot = types.SimpleNamespace(db=self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self.store.DATA_FILE = self.original
        shutil.rmtree(self.dir, ignore_errors=True)

    def _expense(self, amount=500.0):
        return self.store.add_expense(
            description="Server", category="Hosting & servers", amount=amount, method="upi",
            paid_to="Host", transaction_id="UTR-1", remarks="", date="2026-10-05",
            recorded_by="Wixz", company="zarbunex")

    async def test_a_wrong_expense_leaves_the_statement_totals(self):
        from desk.bridge import mirror_expense

        record = self._expense()
        self.assertEqual(record["id"], 1)
        tx_id = await mirror_expense(self.bot, record, 1)
        self.assertTrue(tx_id)
        self.assertEqual(self.store.total_expenses(company="zarbunex"), 500.0)

        self.store.void_entry("expense", record["id"], "wrong amount")
        self.assertEqual(self.store.total_expenses(company="zarbunex"), 0.0)
        self.assertEqual(self.store.expenses(company="zarbunex"), [])
        with self.assertRaises(ValueError):
            self.store.void_entry("expense", record["id"], "again")
        with self.assertRaises(ValueError):
            self.store.void_entry("expense", 999, "no such entry")

    async def test_list_entries_gives_the_id_to_void_and_hides_voided_ones(self):
        first = self._expense(amount=100.0)
        second = self._expense(amount=200.0)

        rows = self.store.list_entries("expense", "zarbunex")
        self.assertEqual([r["id"] for r in rows], [second["id"], first["id"]])

        self.store.void_entry("expense", first["id"], "wrong amount")
        listed = self.store.list_entries("expense", "zarbunex")
        self.assertNotIn(first["id"], [r["id"] for r in listed])
        both = self.store.list_entries("expense", "zarbunex", include_voided=True)
        self.assertTrue(next(r for r in both if r["id"] == first["id"])["voided"])
        # Another company never sees it.
        self.assertEqual(self.store.list_entries("expense", "hectorhost"), [])

    async def test_voiding_the_books_entry_cancels_its_statement_entry(self):
        from desk.bridge import mirror_expense

        record = self._expense(amount=250.0)
        tx_id = await mirror_expense(self.bot, record, 1)
        stored = self.store._load()["expenses"][0]
        self.assertEqual(stored["ref"], f"tx:{tx_id}")              # the books are linked

        row = await self.db.void_transaction(1, tx_id, "duplicate entry")
        self.assertEqual(row["amount"], 25000)
        kind, entry = self.store.void_by_ref(f"tx:{tx_id}", "duplicate entry")
        self.assertEqual(kind, "expense")
        self.assertTrue(entry["voided"])
        self.assertEqual(self.store.total_expenses(company="zarbunex"), 0.0)

    def test_old_entries_without_ids_get_stable_ones(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"invoices": [], "next_invoice": 1001, "payments": [
                {"date": "2026-10-01", "amount": 100.0, "company": "zarbunex"},
                {"date": "2026-10-02", "amount": 200.0, "company": "zarbunex"},
            ]}, fh)
        first = sorted(p["id"] for p in self.store.payments(company="zarbunex"))
        second = sorted(p["id"] for p in self.store.payments(company="zarbunex"))
        self.assertEqual(first, [1, 2])
        self.assertEqual(second, [1, 2])                             # same ids every load


class VoidCommandTests(unittest.TestCase):
    def test_the_void_commands_take_an_id_and_a_reason(self):
        from cogs.finance import Statements

        cog = Statements(object())
        commands = {c.name: c for c in cog.get_app_commands()}
        for name in ("revenue-void", "revenue-list"):
            self.assertIn(name, commands)
        void = commands["revenue-void"]
        self.assertEqual([p.name for p in void.parameters], ["entry", "reason"])
        self.assertTrue(all(p.required for p in void.parameters))

    def test_the_expense_commands_take_an_id_and_a_reason(self):
        from cogs.finance import Statements

        cog = Statements(object())
        commands = {c.name: c for c in cog.get_app_commands()}
        for name in ("expense-void", "expense-list"):
            self.assertIn(name, commands)
        void = commands["expense-void"]
        self.assertEqual([p.name for p in void.parameters], ["entry", "reason"])
        self.assertTrue(all(p.required for p in void.parameters))

    def test_the_expense_id_accepts_every_form(self):
        from cogs.finance import _EXPENSE_ID, expense_number

        for text in ("EXP-0003", "exp-3", "EXP3", "3", "  #3 "):
            match = _EXPENSE_ID.match(text)
            self.assertIsNotNone(match, text)
            self.assertEqual(int(match.group(1)), 3)
        self.assertIsNone(_EXPENSE_ID.match("EXP-"))
        self.assertEqual(expense_number(7), "EXP-0007")

    def test_the_revenue_id_accepts_every_form(self):
        from cogs.finance import _REVENUE_ID

        for text in ("REV-0003", "rev-3", "3", "  REV3 "):
            match = _REVENUE_ID.match(text)
            self.assertIsNotNone(match, text)
            self.assertEqual(int(match.group(1)), 3)
        self.assertIsNone(_REVENUE_ID.match("REV-"))


if __name__ == "__main__":
    unittest.main()
