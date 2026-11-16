"""Invoice due days: /invoice create (was crashing), the ticket-dashboard form, and /invoice create-detailed."""
import os as _os
import tempfile as _tempfile

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_os.environ.setdefault("INVOICE_BOT_DATA", _os.path.join(_tempfile.mkdtemp(prefix="zarbot-ledger-"), "data.json"))

import datetime as dt
import itertools
import os
import shutil
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest import mock

import data as store
import desk.invoices as inv_mod
from cogs.invoices import QuickInvoiceModal
from desk.database import Database
from desk.invoices import Invoices, ItemsModal
from desk.utils import DEFAULT_DUE_DAYS, parse_due, today

UNIQUE = itertools.count(1)
warnings.filterwarnings("ignore", category=DeprecationWarning)


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


class FakeChannel:
    id = 4242

    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw["view"])


class FakeInteraction:
    def __init__(self, client=None):
        self.client = client
        self.user = types.SimpleNamespace(id=1, display_name="Staff")
        self.sent, self.modals, self._done = [], [], False
        self.response = types.SimpleNamespace(send_message=self._send, send_modal=self._modal,
                                              is_done=lambda: self._done)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self._done = True
        self.sent.append(kw["view"])

    async def _modal(self, modal):
        self._done = True
        self.modals.append(modal)

    @property
    def text(self):
        return "\n".join(text_of(v) for v in self.sent)


def fill(modal, **values):
    """Pretend the user typed these into the form."""
    for name, value in values.items():
        getattr(modal, name)._value = value


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.mkdtemp(prefix="zarbot-due-")
        original = store.DATA_FILE
        store.DATA_FILE = Path(folder) / "data.json"
        self.addCleanup(lambda: (setattr(store, "DATA_FILE", original), shutil.rmtree(folder, ignore_errors=True)))
        self.db = Database(os.path.join(folder, "t.db"))
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.bot = types.SimpleNamespace(db=self.db)
        self.channel = FakeChannel()
        patcher = mock.patch("ui.resolve_channel", mock.AsyncMock(return_value=self.channel))
        patcher.start()
        self.addCleanup(patcher.stop)
        staff = mock.patch.object(inv_mod, "is_ticket_staff", lambda user: True)
        staff.start()
        self.addCleanup(staff.stop)

    def in_days(self, n):
        return (today() + dt.timedelta(days=n)).isoformat()


class ParseDueTests(unittest.TestCase):
    def test_days_dates_and_blank(self):
        self.assertIsNone(parse_due(""))
        self.assertIsNone(parse_due("   "))
        self.assertEqual(parse_due("30"), 30)
        self.assertEqual(parse_due("0"), 0)
        self.assertEqual(parse_due((today() + dt.timedelta(days=12)).isoformat()), 12)

    def test_bad_values_are_explained(self):
        for bad in ("abc", "7 days", "31/12/2026", "-3", "366", "99999"):
            with self.assertRaises(ValueError, msg=bad):
                parse_due(bad)
        with self.assertRaises(ValueError):
            parse_due((today() - dt.timedelta(days=1)).isoformat())


class QuickInvoiceTests(Base):
    async def test_invoice_create_opens_its_form_again(self):
        """Regression: the modal had no title, so ValueError('Modal must have a title') killed the command."""
        cog = Invoices.__new__(Invoices)
        interaction = FakeInteraction()
        await Invoices.create.callback(cog, interaction, types.SimpleNamespace(value="zarbunex"), None)
        modal = interaction.modals[0]
        self.assertIsInstance(modal, QuickInvoiceModal)
        self.assertIn("zarbunex", modal.title.lower())
        self.assertLessEqual(len(modal.children), 5)

    async def test_due_days_option_prefills_the_form(self):
        cog = Invoices.__new__(Invoices)
        for option, expected in ((None, DEFAULT_DUE_DAYS), (30, 30), (0, 0)):
            interaction = FakeInteraction()
            await Invoices.create.callback(cog, interaction, types.SimpleNamespace(value="zarbunex"), option)
            self.assertEqual(interaction.modals[0].due.default, self.in_days(expected))

    async def test_the_command_has_a_due_days_option(self):
        names = [p.name for p in Invoices.create.parameters]
        self.assertEqual(names, ["company", "due_days"])
        self.assertFalse(Invoices.create.parameters[1].required)

    async def submit(self, modal, **values):
        interaction = FakeInteraction(client=object())
        fill(modal, **values)
        await modal.on_submit(interaction)
        return interaction

    async def test_submit_saves_the_due_date_whether_typed_as_days_or_a_date(self):
        cases = [("30", self.in_days(30)), (self.in_days(45), self.in_days(45)), ("", "")]
        for index, (typed, expected) in enumerate(cases):
            done = await self.submit(QuickInvoiceModal("zarbunex", 30), customer=f"Client {index}", contact="",
                                     amount="100", service="VPS", due=typed)
            self.assertIn("Invoice created", done.text, done.text)
            self.assertEqual(store.invoices()[-1]["due"], expected)
        self.assertEqual(len(self.channel.sent), 3)
        self.assertIn(self.in_days(30), text_of(self.channel.sent[0]))

    async def test_untouched_form_keeps_the_prefilled_date(self):
        modal = QuickInvoiceModal("hectorhost", 30)
        # Discord submits whatever the box holds, which is the pre-filled date when nobody touched it
        done = await self.submit(modal, customer="Prefilled", contact="", amount="250", service="VPS",
                                 due=modal.due.default)
        self.assertIn("Invoice created", done.text)
        self.assertEqual(store.invoices()[-1]["due"], self.in_days(30))
        self.assertIn("Due", done.text)

    async def test_invalid_due_saves_nothing_and_says_why(self):
        before = len(store.invoices())
        for bad in ("soon", "2020-01-01", "400"):
            done = await self.submit(QuickInvoiceModal("zarbunex"), customer=f"Bad {bad}", contact="",
                                     amount="100", service="VPS", due=bad)
            self.assertIn("Invalid due date", done.text, bad)
        self.assertEqual(len(store.invoices()), before)
        self.assertEqual(self.channel.sent, [])


class DashboardFormTests(Base):
    OPTS = dict(client_discord_id=2, client_name="Cat", client_contact="", due_days=7, tax_percent=0.0,
                discount_cents=0, category="Hosting Revenue", ticket_channel_id=None, company_id="zarbunex",
                product=None)

    def modal(self, company="ZarBunex PVT LTD", **opts):
        cog = types.SimpleNamespace(bot=self.bot)
        return ItemsModal(cog, {**self.OPTS, **opts}, company=company)

    async def test_ticket_form_has_a_due_days_field_defaulting_to_the_current_default(self):
        modal = self.modal()
        self.assertLessEqual(len(modal.children), 5)
        self.assertIn(modal.due, modal.children)
        self.assertEqual(modal.due.default, "7")
        self.assertNotIn(modal.company, modal.children)               # company comes from the ticket
        self.assertIn(modal.notes, modal.children)                    # nothing else was lost
        self.assertIn("zarbunex", modal.title.lower())

    async def test_unknown_company_still_asks_for_it(self):
        modal = self.modal(company="", company_id=None)
        self.assertLessEqual(len(modal.children), 5)
        self.assertIn(modal.company, modal.children)
        self.assertIn(modal.due, modal.children)

    async def test_forms_do_not_leak_layout_between_tickets(self):
        self.modal(), self.modal(company="", company_id=None)
        again = self.modal()
        self.assertEqual(len(again.children), 5)
        self.assertIn(again.notes, again.children)

    async def create(self, **typed):
        modal = self.modal()
        interaction = FakeInteraction()
        fill(modal, items=f"VPS {next(UNIQUE)}GB | 1 | 700 | type=VPS", **typed)  # the bot refuses exact repeats
        await modal.on_submit(interaction)
        return interaction

    async def test_30_day_due_date_is_saved_on_the_invoice(self):
        done = await self.create(due="30")
        self.assertEqual(len(done.sent), 1, done.text)
        inv = await self.db.get_invoice(1)
        self.assertEqual((dt.date.fromisoformat(inv["due_date"]) - dt.date.fromisoformat(inv["issue_date"])).days, 30)
        self.assertEqual(inv["company_id"], "zarbunex")

    async def test_blank_keeps_the_default_and_a_date_is_accepted(self):
        await self.create(due="")
        inv = await self.db.get_invoice(1)
        self.assertEqual((dt.date.fromisoformat(inv["due_date"]) - dt.date.fromisoformat(inv["issue_date"])).days, 7)
        await self.create(due=self.in_days(45))
        inv = await self.db.get_invoice(2)
        self.assertEqual((dt.date.fromisoformat(inv["due_date"]) - dt.date.fromisoformat(inv["issue_date"])).days, 45)

    async def test_bad_due_value_creates_nothing(self):
        for bad in ("soon", "999", "2020-01-01"):
            done = await self.create(due=bad)
            self.assertIn("❌", done.text, bad)
        self.assertEqual(await self.db.fetchall("SELECT id FROM invoices"), [])

    async def test_detailed_command_value_prefills_the_form(self):
        modal = self.modal(due_days=14)
        self.assertEqual(modal.due.default, "14")


if __name__ == "__main__":
    unittest.main()
