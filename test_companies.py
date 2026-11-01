"""Company separation tests for the multi-company finance changes.

Run with: python -m unittest discover -s tests -v
"""
import os
import shutil
import tempfile
import unittest

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_TMP = tempfile.mkdtemp(prefix="zarbot-ledger-")
os.environ.setdefault("INVOICE_BOT_DATA", os.path.join(_TMP, "data.json"))

import dataclasses  # noqa: E402

import discord  # noqa: E402

import config  # noqa: E402
import data as store  # noqa: E402
import ui  # noqa: E402
from desk.bridge import post_company, sale_from_payment  # noqa: E402
from desk.database import Database, revenue_number  # noqa: E402
from desk.utils import parse_items  # noqa: E402


class CompanyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-companies-")
        self.db = Database(os.path.join(self.dir, "companies.db"))
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_invoice_and_payment_keep_the_company(self):
        items = parse_items("VPS | 1 | 700 | type=VPS")
        invoice_id = await self.db.create_invoice(
            1, client_discord_id=2, client_name="Flame", items=items, due_days=7,
            tax_percent=0, discount_cents=0, category="Hosting Revenue", notes="",
            ticket_channel_id=None, company_id="hectorhost",
        )
        invoice = await self.db.get_invoice(invoice_id)
        self.assertEqual(invoice["company_id"], "hectorhost")

        await self.db.add_payment(1, invoice_id, invoice["total"], "UPI", "paid")
        invoice = await self.db.get_invoice(invoice_id)
        self.assertEqual(invoice["payments"][0]["company_id"], "hectorhost")

        month = invoice["issue_date"][:7]
        rows = await self.db.transactions(month, company_id="hectorhost")
        self.assertEqual([r["company_id"] for r in rows], ["hectorhost"])
        self.assertEqual(await self.db.transactions(month, company_id="matrixmc"), [])
        self.assertEqual(await self.db.gross_sales(month, "zarbunex"), 0)
        self.assertEqual(await self.db.gross_sales(month, "hectorhost"), invoice["total"])
        self.assertEqual(await self.db.outstanding("hectorhost"), 0)

        sale = sale_from_payment(invoice, invoice["payments"][0])
        self.assertEqual(sale["company"], "hectorhost")

    async def test_revenue_totals_are_split_and_investment_stays_out_of_profit(self):
        month = "2026-10"
        first = await self.db.add_revenue(1, "zarbunex", "Commission", 500000,
                                          "Hosting referral commission", f"{month}-05")
        self.assertEqual(revenue_number(first), "REV-000001")
        await self.db.add_revenue(1, "zarbunex", "Investment", 1000000, "Seed capital", f"{month}-06")
        await self.db.add_revenue(1, "hectorhost", "General", 20000, "Misc income", f"{month}-07")

        report = await self.db.month_report(month, "zarbunex")
        rows = dict(report["revenue_rows"])
        self.assertEqual(rows["Commission"], 500000)
        self.assertEqual(rows["Investment"], 1000000)
        self.assertEqual(rows["General"], 0)
        self.assertEqual(report["revenue"], 500000)        # investment excluded
        self.assertEqual(report["revenue_total"], 1500000)
        self.assertEqual(report["net"], 500000)

        other = await self.db.month_report(month, "hectorhost")
        self.assertEqual(other["revenue"], 20000)
        self.assertEqual(other["investment"], 0)
        self.assertEqual(dict(other["revenue_rows"])["Commission"], 0)

        combined = await self.db.month_report(month)
        self.assertEqual(combined["revenue"], 520000)

    async def test_revenue_rejects_bad_category_and_amount(self):
        with self.assertRaises(ValueError):
            await self.db.add_revenue(1, "matrixmc", "Bonus", 100, "x", "2026-10-01")
        with self.assertRaises(ValueError):
            await self.db.add_revenue(1, "matrixmc", "General", 0, "x", "2026-10-01")

    async def test_ticket_company_survives_a_category_move(self):
        ticket_id = await self.db.create_ticket(
            1, subject="Need a VPS", kind="support", priority="normal",
            client_discord_id=2, client_name="B", channel_id=42, company_id="matrixmc",
        )
        self.assertEqual((await self.db.get_ticket(ticket_id))["company_id"], "matrixmc")
        await self.db.set_ticket_company(1, ticket_id, "hectorhost")
        self.assertEqual((await self.db.get_ticket(ticket_id))["company_id"], "hectorhost")
        await self.db.record_ticket_move(1, ticket_id, {"from": {"category": "Billing"},
                                                        "to": {"category": "Support"}})
        self.assertEqual((await self.db.get_ticket(ticket_id))["company_id"], "hectorhost")
        with self.assertRaises(ValueError):
            await self.db.set_ticket_company(1, 99999, "zarbunex")

    async def test_configured_companies_match_the_request(self):
        self.assertEqual([c.name for c in config.COMPANIES.values()],
                         ["Zarbunex PVT LTD", "HectorHost", "MatrixMC"])
        self.assertIsNone(config.get_company(""))


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_database_is_upgraded_in_place(self):
        import sqlite3

        folder = tempfile.mkdtemp(prefix="zarbot-old-")
        path = os.path.join(folder, "old.db")
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE, kind TEXT NOT NULL, UNIQUE(name, kind));
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, client_discord_id INTEGER,
                client_name TEXT NOT NULL, client_contact TEXT, issue_date TEXT NOT NULL,
                due_date TEXT NOT NULL, currency TEXT NOT NULL, subtotal INTEGER NOT NULL,
                discount INTEGER NOT NULL DEFAULT 0, tax_percent REAL NOT NULL DEFAULT 0,
                tax INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL, notes TEXT,
                category_id INTEGER NOT NULL REFERENCES categories(id), ticket_channel_id INTEGER,
                voided INTEGER NOT NULL DEFAULT 0, void_reason TEXT,
                overdue_notified INTEGER NOT NULL DEFAULT 0, delivered_at TEXT, delivery_note TEXT,
                created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'support', priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'open', client_discord_id INTEGER,
                client_name TEXT NOT NULL, channel_id INTEGER, parent_ticket_id INTEGER,
                claimed_by INTEGER, close_reason TEXT, transcript_path TEXT, deal_id INTEGER,
                last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                opened_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO categories(name, kind) VALUES ('Hosting Revenue', 'income');
            INSERT INTO invoices(client_name, issue_date, due_date, currency, subtotal, total,
                                 category_id, created_by)
                VALUES ('Legacy client', '2026-01-01', '2026-01-08', 'INR', 10000, 10000, 1, 7);
            INSERT INTO tickets(subject, client_name, opened_by) VALUES ('Old ticket', 'Old', 7);
            """
        )
        con.commit()
        con.close()

        db = Database(path)
        await db.connect()
        try:
            invoice = await db.get_invoice(1)
            self.assertEqual(invoice["client_name"], "Legacy client")
            self.assertIsNone(invoice["company_id"])
            ticket = await db.get_ticket(1)
            self.assertIsNone(ticket["company_id"])
            await db.set_ticket_company(7, 1, "hectorhost")
            self.assertEqual((await db.get_ticket(1))["company_id"], "hectorhost")
            revenue_id = await db.add_revenue(7, "matrixmc", "General", 100000, "after upgrade",
                                              "2026-10-01")
            self.assertEqual(revenue_number(revenue_id), "REV-000001")
        finally:
            await db.close()
            shutil.rmtree(folder, ignore_errors=True)


class FakeBot:
    """Minimal channel lookup: enough to exercise post_company without a gateway."""

    def __init__(self, channel=None):
        self.channel = channel
        self.posted = []

    def get_channel(self, channel_id):
        return self.channel

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(_FakeResponse(), "Unknown Channel")


class _FakeResponse:
    status = 404
    reason = "Not Found"


class _FailingChannel:
    id = 123

    async def send(self, **kwargs):
        raise discord.HTTPException(_FakeResponse(), "Missing Permissions")


class StatementRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.company = config.get_company("zarbunex")

    def tearDown(self):
        config.COMPANIES["zarbunex"] = self.company

    async def test_unset_channel_names_the_exact_variable(self):
        config.COMPANIES["zarbunex"] = dataclasses.replace(self.company, statement_channel_id=None)
        ok, message = await post_company(FakeBot(), "zarbunex", ui.PersistentView(timeout=None))
        self.assertFalse(ok)
        self.assertIn("COMPANY_ZARBUNEX_STATEMENT_CHANNEL_ID", message)
        self.assertIn("Zarbunex PVT LTD", message)

    async def test_missing_channel_is_reported_not_posted_elsewhere(self):
        config.COMPANIES["zarbunex"] = dataclasses.replace(self.company, statement_channel_id=987654321)
        ok, message = await post_company(FakeBot(), "zarbunex", ui.PersistentView(timeout=None))
        self.assertFalse(ok)
        self.assertIn("987654321", message)

    async def test_discord_error_is_reported(self):
        config.COMPANIES["zarbunex"] = dataclasses.replace(self.company, statement_channel_id=123)
        ok, message = await post_company(FakeBot(_FailingChannel()), "zarbunex",
                                         ui.PersistentView(timeout=None))
        self.assertFalse(ok)
        self.assertIn("Send Messages", message)


class RevenueCardTests(unittest.TestCase):
    def test_revenue_statement_fields(self):
        record = {"id": 1, "number": "REV-000001", "company_id": "zarbunex", "category": "Commission",
                  "amount": 500000, "description": "Hosting referral commission",
                  "occurred_on": "2026-10-05", "recorded_by": "Fluffy",
                  "created_at": "2026-10-05T12:00:00"}
        text = "\n".join(c["content"] for c in ui.revenue_card(record).to_components()[0]["components"]
                         if c["type"] == 10)
        for label in ("Transaction ID", "Company", "Category", "Amount", "Description", "Date",
                      "Recorded By"):
            self.assertIn(f"**{label}:**", text)
        self.assertIn("REV-000001", text)
        self.assertIn("Zarbunex PVT LTD", text)
        self.assertIn("₹5,000.00", text)


class LedgerCompanyTests(unittest.TestCase):
    def test_sales_and_expenses_are_per_company(self):
        base = dict(client_contact="", server_name="", server_uuid="", service="VPS",
                    method="upi", transaction_id="", remarks="", date="2026-10-01", recorded_by="me")
        store.add_payment(client_name="A", amount=100, company="zarbunex", **base)
        store.add_payment(client_name="B", amount=250, company="hectorhost", **base)
        self.assertEqual(store.total_sales(company="zarbunex"), 100)
        self.assertEqual(store.total_sales(company="hectorhost"), 250)
        self.assertGreaterEqual(store.total_sales(), 350)

        store.add_expense(description="Node", category="Hosting & servers", amount=40, method="upi",
                          paid_to="", transaction_id="", remarks="", date="2026-10-02",
                          recorded_by="me", company="matrixmc")
        self.assertEqual(store.total_expenses(company="matrixmc"), 40)
        self.assertEqual(store.total_expenses(company="zarbunex"), 0)


if __name__ == "__main__":
    unittest.main()
