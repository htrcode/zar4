"""Sales, expenses and revenue must reach every report exactly once.

Covers the two books (statement ledger in data.json + SQLite finance books), legacy / half-mirrored
entries, voids, company separation, dates, /revenue vs investment, and the reconcile report.
Run with: python -m unittest discover -s tests -v
"""
import os as _os
import tempfile as _tempfile

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_os.environ.setdefault("INVOICE_BOT_DATA", _os.path.join(_tempfile.mkdtemp(prefix="zarbot-ledger-"), "data.json"))

import asyncio
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import data as store  # noqa: E402
from desk.bridge import (ISSUE_TITLES, ledger_date, mirror_expense, mirror_sale,  # noqa: E402
                         outstanding_cents, post_transaction_event, reconcile_books)
from desk.database import Database  # noqa: E402
from desk.finance import reconcile_card  # noqa: E402
from desk.utils import parse_items  # noqa: E402

MONTH = "2026-11"
DAY = "2026-11-05"
SALE = dict(client_contact="", server_name="", server_uuid="", method="upi", transaction_id="",
            remarks="", recorded_by="Staff", service="VPS")


class FakeBot:
    def __init__(self, db):
        self.db = db

    def get_channel(self, cid):
        return None

    async def fetch_channel(self, cid):
        import discord
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="x"), "x")


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.mkdtemp(prefix="zarbot-reconcile-")
        original = store.DATA_FILE
        store.DATA_FILE = Path(folder) / "data.json"
        self.addCleanup(lambda: (setattr(store, "DATA_FILE", original), shutil.rmtree(folder, ignore_errors=True)))
        self.db = Database(os.path.join(folder, "t.db"))
        await self.db.connect()
        self.bot = FakeBot(self.db)

    async def asyncTearDown(self):
        await self.db.close()

    # -- helpers ----------------------------------------------------------
    def sale(self, amount, company="zarbunex", date=DAY, client="Ann", **kw):
        return store.add_payment(client_name=client, amount=amount, company=company, date=date, **{**SALE, **kw})

    def expense(self, amount, company="zarbunex", date=DAY, **kw):
        return store.add_expense(description="Server", category="Hosting & servers", amount=amount, method="upi",
                                 paid_to="", transaction_id="", remarks="", date=date, recorded_by="Staff",
                                 company=company, **kw)

    async def report(self, company=None, month=MONTH):
        return await self.db.month_report(month, company)

    async def issues(self, **kw):
        return (await reconcile_books(self.bot, **kw))["issues"]

    async def paid_invoice(self, company="hectorhost", price=200000):
        iid = await self.db.create_invoice(
            1, client_discord_id=2, client_name="Cat", items=parse_items(f"VPS | 1 | {price // 100} | type=VPS"),
            due_days=7, tax_percent=0, discount_cents=0, category="Hosting Revenue", notes="",
            ticket_channel_id=None, company_id=company)
        inv = await self.db.get_invoice(iid)
        pid = await self.db.add_payment(1, iid, inv["total"], "UPI", "paid")
        return iid, pid


# ---------------------------------------------------------------------------
class MissingSalesTests(Base):
    async def test_legacy_sale_is_missing_from_the_pnl_until_reconciled_then_counted_once(self):
        self.sale(700, "zarbunex")                                   # recorded before mirroring existed
        self.assertEqual((await self.report())["revenue"], 0)       # the bug: sales summary has it, P&L not
        self.assertEqual(store.total_sales(), 700)

        found = await self.issues()                                  # dry run reports, changes nothing
        self.assertEqual([i["code"] for i in found], ["not_in_books"])
        self.assertEqual((await self.report())["revenue"], 0)
        self.assertEqual(await self.db.fetchall("SELECT id FROM transactions"), [])

        result = await reconcile_books(self.bot, apply=True, actor_id=7)
        self.assertEqual(result["fixed"], 1)
        self.assertEqual((await self.report("zarbunex"))["revenue"], 70000)
        self.assertEqual((await self.report("hectorhost"))["revenue"], 0)            # company separation
        self.assertEqual((await self.report(month="2026-10"))["revenue"], 0)         # date respected
        for _ in range(2):                                                          # idempotent: no double count
            self.assertEqual((await reconcile_books(self.bot, apply=True))["issues"], [])
        self.assertEqual((await self.report())["revenue"], 70000)
        audit = [a["action"] for a in await self.db.recent_audit(10)]
        self.assertIn("reconcile.fix", audit)
        self.assertIn("transaction.income", audit)

    async def test_half_mirrored_sale_is_linked_not_added_twice(self):
        sale = self.sale(300, "zarbunex")
        await self.db.add_transaction(1, "income", "Hosting Revenue", 30000, "Sale: Ann — VPS", DAY,
                                      company_id="zarbunex")         # mirrored, but the link was lost
        self.assertEqual((await self.report())["revenue"], 30000)
        found = await self.issues(apply=True)
        self.assertEqual([i["code"] for i in found], ["linked"])
        self.assertEqual((await self.report())["revenue"], 30000)    # still counted exactly once
        self.assertEqual(store.entry("payment", sale["id"])["ref"], "tx:1")

    async def test_manual_books_income_and_expense_reach_the_statement_totals(self):
        await self.db.add_transaction(1, "income", "Hosting Revenue", 12345, "manual", DAY, company_id="hectorhost")
        await self.db.add_transaction(1, "expense", "Other Expenses", 5000, "fee", DAY, company_id="hectorhost")
        self.assertEqual((store.total_sales(), store.total_expenses()), (0, 0))
        await reconcile_books(self.bot, apply=True)
        self.assertEqual(store.total_sales(company="hectorhost"), 123.45)
        self.assertEqual(store.total_expenses(company="hectorhost"), 50.0)
        self.assertEqual((await self.report("hectorhost"))["revenue"], 12345)        # books unchanged
        self.assertEqual(await self.issues(), [])

    async def test_invoice_payment_missing_from_the_ledger_is_added_without_double_counting(self):
        iid, pid = await self.paid_invoice()                         # the payment event never ran
        self.assertEqual(store.total_sales(), 0)
        found = await self.issues(apply=True)
        self.assertEqual([i["code"] for i in found], ["not_in_ledger"])
        self.assertEqual(store.total_sales(company="hectorhost"), 2000)
        self.assertEqual(store.all_payments()[0]["ref"], f"desk-payment:{pid}")
        self.assertEqual((await self.db.gross_sales()), 200000)                       # books not doubled
        self.assertEqual(await self.issues(), [])

    async def test_expense_recorded_only_in_the_ledger_reaches_the_pnl(self):
        self.expense(250, "hectorhost")
        self.assertEqual((await self.report())["expense_total"], 0)
        await reconcile_books(self.bot, apply=True)
        self.assertEqual((await self.report("hectorhost"))["expense_total"], 25000)
        self.assertEqual((await self.report("zarbunex"))["expense_total"], 0)
        self.assertEqual(await self.issues(), [])

    async def test_voided_ledger_entries_are_never_resurrected(self):
        sale = self.sale(500)
        store.void_entry("payment", sale["id"], "typo")
        self.assertEqual(await self.issues(apply=True), [])
        self.assertEqual((await self.report())["revenue"], 0)
        self.assertTrue(store.payment_by_sale_id(sale["sale_id"])["voided"])           # kept for audit

    async def test_current_flows_stay_in_step(self):
        sale = self.sale(500, "matrixmc")
        await mirror_sale(self.bot, sale, 1)
        exp = self.expense(80, "matrixmc")
        await mirror_expense(self.bot, exp, 1)
        await self.paid_invoice()
        await post_transaction_event(self.bot, "payment", {"invoice_id": 1, "payment_id": 1})
        result = await reconcile_books(self.bot)
        self.assertEqual(result["issues"], [])
        for key, t in result["totals"].items():
            self.assertEqual((t["ledger_sales"], t["ledger_expenses"]), (t["books_income"], t["books_expenses"]), key)
        self.assertEqual(((await self.report("matrixmc"))["revenue"], (await self.report("matrixmc"))["expense_total"]),
                         (50000, 8000))


# ---------------------------------------------------------------------------
class VoidTests(Base):
    async def test_voiding_an_invoice_removes_its_payment_from_sales_statements_too(self):
        iid, pid = await self.paid_invoice()
        await post_transaction_event(self.bot, "payment", {"invoice_id": iid, "payment_id": pid})
        self.assertEqual(store.total_sales(), 2000)
        await self.db.void_invoice(1, iid, "wrong client")
        await post_transaction_event(self.bot, "invoice.voided", {"invoice_id": iid, "reason": "wrong client"})
        self.assertEqual(store.total_sales(), 0)                      # Sales statement totals
        self.assertEqual((await self.report())["revenue"], 0)         # P&L
        row = store.all_payments()[0]
        self.assertTrue(row["voided"])                                # still there for audit
        self.assertEqual(row["void_reason"], "wrong client")
        self.assertEqual(await self.issues(), [])

    async def test_database_emits_the_void_event_for_the_listener(self):
        seen = []

        async def listener(event, payload):
            seen.append((event, payload["invoice_id"]))
        self.db.subscribe(listener)
        iid, _ = await self.paid_invoice()
        await self.db.void_invoice(1, iid, "x")
        await asyncio.sleep(0.05)
        self.assertIn(("invoice.voided", iid), seen)

    async def test_lost_void_event_is_found_and_repaired_by_reconcile(self):
        iid, pid = await self.paid_invoice()
        await post_transaction_event(self.bot, "payment", {"invoice_id": iid, "payment_id": pid})
        await self.db.void_invoice(1, iid, "x")                       # listener was not subscribed
        self.assertEqual(store.total_sales(), 2000)                   # ledger still counts it
        found = await self.issues(apply=True)
        self.assertEqual([i["code"] for i in found], ["void_mismatch"])
        self.assertEqual(store.total_sales(), 0)
        self.assertEqual(await self.issues(), [])

    async def test_ledger_void_is_followed_by_the_books(self):
        sale = self.sale(400)
        await mirror_sale(self.bot, sale, 1)
        store.void_entry("payment", sale["id"], "typo")               # interrupted delete: books side not voided
        self.assertEqual((await self.report())["revenue"], 40000)
        await reconcile_books(self.bot, apply=True, actor_id=3)
        self.assertEqual((await self.report())["revenue"], 0)
        voided = await self.db.fetchone("SELECT voided FROM transactions WHERE id=1")
        self.assertEqual(voided["voided"], 1)                         # kept, flagged void

    async def test_history_lists_voided_rows_only_on_request_and_totals_never_count_them(self):
        keep = await self.db.add_transaction(1, "income", "Hosting Revenue", 10000, "keep", DAY, company_id="zarbunex")
        gone = await self.db.add_transaction(1, "income", "Hosting Revenue", 20000, "gone", DAY, company_id="zarbunex")
        await self.db.void_transaction(1, gone, "typo")
        plain = await self.db.transactions(MONTH)
        full = await self.db.transactions(MONTH, include_voided=True)
        self.assertEqual([t["id"] for t in plain], [keep])
        self.assertEqual({t["id"]: t["voided"] for t in full}, {keep: 0, gone: 1})
        self.assertEqual((await self.report())["revenue"], 10000)
        self.assertIn("transaction.void", [a["action"] for a in await self.db.recent_audit(10)])


# ---------------------------------------------------------------------------
class RevenueAndInvestmentTests(Base):
    async def test_revenue_counts_but_investment_never_becomes_revenue_or_profit(self):
        await self.db.add_transaction(1, "income", "Hosting Revenue", 100000, "sale", DAY, company_id="zarbunex")
        await self.db.add_transaction(1, "expense", "Other Expenses", 30000, "cost", DAY, company_id="zarbunex")
        await self.db.add_revenue(1, "zarbunex", "Commission", 20000, "c", DAY, received_from="x")
        await self.db.add_revenue(1, "zarbunex", "General", 10000, "g", DAY, received_from="x")
        await self.db.add_revenue(1, "zarbunex", "Investment", 900000, "capital", DAY, received_from="y")
        rep = await self.report("zarbunex")
        self.assertEqual(rep["revenue"], 130000)                      # sale + Commission + General
        self.assertEqual(rep["net"], 100000)                          # profit ignores the investment
        self.assertEqual(rep["investment"], 900000)                   # tracked, separately
        self.assertEqual(rep["revenue"] + rep["investment"], 1030000)
        result = await reconcile_books(self.bot)
        self.assertEqual([i for i in result["issues"] if i["code"] == "report_mismatch"], [])
        t = result["totals"]["zarbunex"]
        self.assertEqual((t["operating_revenue"], t["investment"]), (30000, 900000))

    async def test_voided_revenue_leaves_the_totals(self):
        rid = await self.db.add_revenue(1, "zarbunex", "Commission", 20000, "c", DAY, received_from="x")
        await self.db.void_revenue(1, rid, "wrong")
        self.assertEqual((await self.report())["revenue"], 0)
        self.assertEqual((await reconcile_books(self.bot))["totals"].get("zarbunex", {}).get("operating_revenue", 0), 0)

    async def test_company_separation_is_preserved_everywhere(self):
        for company, amount in (("zarbunex", 100), ("hectorhost", 200), ("matrixmc", 400)):
            await mirror_sale(self.bot, self.sale(amount, company), 1)
        self.sale(800, "")                                           # old entry with no company
        await reconcile_books(self.bot, apply=True)
        self.assertEqual([(await self.report(c))["revenue"] for c in ("zarbunex", "hectorhost", "matrixmc")],
                         [10000, 20000, 40000])
        self.assertEqual((await self.report())["revenue"], 150000)    # all companies incl. the unassigned one
        self.assertEqual(store.total_sales(company="hectorhost"), 200)


# ---------------------------------------------------------------------------
class DateAndReportTests(Base):
    def test_ledger_dates_are_normalised_so_monthly_reports_cannot_skip_them(self):
        self.assertEqual(ledger_date({"date": "2026-11-05"}), "2026-11-05")
        self.assertEqual(ledger_date({"date": "27/10/2026"}), "2026-10-27")
        self.assertEqual(ledger_date({"date": "5 Nov 2026"}), "2026-11-05")
        self.assertEqual(ledger_date({"date": "yesterday", "created_at": "2026-11-04T20:00:00+00:00"}), "2026-11-04")
        self.assertEqual(ledger_date({"date": "2026-13-45", "created_at": "2026-11-04T20:00:00+00:00"}), "2026-11-04")

    async def test_odd_date_sale_lands_in_the_right_month(self):
        self.sale(50, "zarbunex", date="27/10/2026")
        await reconcile_books(self.bot, apply=True)
        self.assertEqual((await self.report(month="2026-10"))["revenue"], 5000)
        self.assertEqual((await self.report(month=MONTH))["revenue"], 0)

    async def test_rows_with_unreadable_dates_are_reported_because_no_report_sees_them(self):
        await self.db.conn.execute(
            "INSERT INTO transactions(kind, category_id, company_id, amount, occurred_on, created_by) "
            "VALUES ('income', 1, 'zarbunex', 9900, 'sometime', 1)")
        await self.db.conn.commit()
        found = [i for i in await self.issues() if i["code"] == "bad_date"]
        self.assertEqual([(i["ref"], i["cents"]) for i in found], [("tx #1", 9900)])

    async def test_amount_company_or_date_differences_are_reported_never_rewritten(self):
        sale = self.sale(100, "zarbunex")
        await mirror_sale(self.bot, sale, 1)
        await self.db.conn.execute("UPDATE transactions SET amount=15000 WHERE id=1")
        await self.db.conn.commit()
        found = await self.issues(apply=True)
        self.assertEqual([i["code"] for i in found], ["mismatch"])
        self.assertIn("amount", found[0]["detail"])
        self.assertEqual((await self.db.fetchone("SELECT amount FROM transactions WHERE id=1"))["amount"], 15000)

    async def test_a_report_that_disagrees_with_its_records_is_flagged(self):
        await self.db.add_transaction(1, "income", "Hosting Revenue", 10000, "s", DAY, company_id="zarbunex")
        real = self.db.month_report

        async def wrong(month, company=None):
            data = await real(month, company)
            return {**data, "revenue": data["revenue"] - 100}
        self.db.month_report = wrong
        found = [i for i in await self.issues() if i["code"] == "report_mismatch"]
        self.assertTrue(found)
        self.assertEqual(found[0]["cents"], -100)

    async def test_card_explains_a_dry_run_and_a_repair(self):
        self.sale(700)
        dry = reconcile_card(await reconcile_books(self.bot))
        text = _text(dry)
        self.assertIn("Nothing was changed", text)
        self.assertIn(ISSUE_TITLES["not_in_books"], text)
        self.assertIn("SALE-000001", text)
        done = _text(reconcile_card(await reconcile_books(self.bot, apply=True)))
        self.assertIn("repaired", done)
        clean = _text(reconcile_card(await reconcile_books(self.bot)))
        self.assertIn("all reconcile", clean)


# ---------------------------------------------------------------------------
class OutstandingTests(Base):
    async def test_unpaid_quick_invoices_count_as_receivables_per_company(self):
        await self.paid_invoice(company="hectorhost")                # fully paid: nothing owed
        store.create_invoice("Bob", "", 1000.0, "2026-12-01", "Plugin", "staff", company="zarbunex")
        paid = store.create_invoice("Dee", "", 400.0, "2026-12-01", "Plugin", "staff", company="zarbunex")
        store.mark_paid(paid["number"], "upi", "", "", "", "", "staff", company="zarbunex")
        self.assertEqual(await outstanding_cents(self.bot, "zarbunex"), 100000)
        self.assertEqual(await outstanding_cents(self.bot, "hectorhost"), 0)
        self.assertEqual(await outstanding_cents(self.bot), 100000)
        iid = await self.db.create_invoice(
            1, client_discord_id=2, client_name="Eve", items=parse_items("VPS | 1 | 50 | type=VPS"), due_days=7,
            tax_percent=0, discount_cents=0, category="Hosting Revenue", notes="", ticket_channel_id=None,
            company_id="zarbunex")
        self.assertEqual(await outstanding_cents(self.bot, "zarbunex"), 105000)
        await self.db.void_invoice(1, iid, "x")
        self.assertEqual(await outstanding_cents(self.bot, "zarbunex"), 100000)


def _text(view) -> str:
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


if __name__ == "__main__":
    unittest.main()
