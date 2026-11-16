"""P&L is reported per company: Sales — <Company>, expenses by company, company-wise summary."""
import os as _os
import tempfile as _tempfile

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_os.environ.setdefault("INVOICE_BOT_DATA", _os.path.join(_tempfile.mkdtemp(prefix="zarbot-ledger-"), "data.json"))

import asyncio  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

from desk.database import Database  # noqa: E402
from desk.pdf_gen import pnl_pdf  # noqa: E402
from desk.views import DashboardView  # noqa: E402

M, D = "2026-11", "2026-11-05"


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


def pdf_text(data: bytes) -> str:
    return subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, check=True).stdout.decode()


class PnlBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-pnl-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = Database(os.path.join(self.dir, "t.db"))
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        db = self.db
        # ZarBunex: sales under two categories, two expense categories, a Commission statement
        await db.add_transaction(1, "income", "Hosting Revenue", 700000, "s", D, company_id="zarbunex")
        await db.add_transaction(1, "income", "Other Sales", 150000, "s", D, company_id="zarbunex")
        await db.add_transaction(1, "expense", "Server Rental", 120000, "e", D, company_id="zarbunex")
        await db.add_transaction(1, "expense", "Marketing", 30000, "e", D, company_id="zarbunex")
        await db.add_revenue(1, "zarbunex", "Commission", 60000, "c", D, received_from="x")
        # HectorHost: sales, one expense, and an investment that is NOT revenue
        await db.add_transaction(1, "income", "Hosting Revenue", 450000, "s", D, company_id="hectorhost")
        await db.add_transaction(1, "expense", "Server Rental", 90000, "e", D, company_id="hectorhost")
        await db.add_revenue(1, "hectorhost", "Investment", 500000, "cap", D, received_from="y")
        # MatrixMC
        await db.add_transaction(1, "income", "Hosting Revenue", 250000, "s", D, company_id="matrixmc")
        await db.add_transaction(1, "expense", "Server Rental", 40000, "e", D, company_id="matrixmc")

    async def report(self, company=None, month=M):
        return await self.db.month_report(month, company)


class ReportShapeTests(PnlBase):
    async def test_sales_are_reported_per_company_whatever_category_they_were_filed_under(self):
        rep = await self.report()
        self.assertEqual(dict(rep["income"]), {"Sales — Zarbunex PVT LTD": 850000, "Sales — HectorHost": 450000,
                                               "Sales — MatrixMC": 250000})
        self.assertNotIn("Hosting Revenue", " ".join(name for name, _ in rep["income"]))
        self.assertEqual(dict(rep["income_categories"]), {"Hosting Revenue": 1400000, "Other Sales": 150000})

    async def test_overall_expenses_say_whose_they_are(self):
        rep = await self.report()
        self.assertEqual(dict(rep["expenses"]), {
            "Zarbunex PVT LTD — Server Rental": 120000, "Zarbunex PVT LTD — Marketing": 30000,
            "HectorHost — Server Rental": 90000, "MatrixMC — Server Rental": 40000})

    async def test_a_company_report_keeps_plain_category_expenses_and_only_its_own_figures(self):
        rep = await self.report("zarbunex")
        self.assertEqual(rep["income"], [("Sales — Zarbunex PVT LTD", 850000)])
        self.assertEqual(dict(rep["expenses"]), {"Server Rental": 120000, "Marketing": 30000})
        self.assertEqual((rep["revenue"], rep["expense_total"], rep["net"]), (910000, 150000, 760000))
        self.assertEqual([c["key"] for c in rep["by_company"]], ["zarbunex"])

    async def test_company_table_adds_up_to_the_overall_totals_and_to_each_company_report(self):
        rep = await self.report()
        rows = rep["by_company"]
        self.assertEqual([c["key"] for c in rows], ["zarbunex", "hectorhost", "matrixmc"])     # config order
        self.assertEqual(sum(c["sales"] for c in rows), rep["income_total"])
        self.assertEqual(sum(c["revenue"] for c in rows), rep["revenue"])
        self.assertEqual(sum(c["expense_total"] for c in rows), rep["expense_total"])
        self.assertEqual(sum(c["net"] for c in rows), rep["net"])
        for row in rows:
            own = await self.report(row["key"])
            self.assertEqual((row["sales"], row["revenue"], row["expense_total"], row["net"]),
                             (own["income_total"], own["revenue"], own["expense_total"], own["net"]), row["key"])

    async def test_totals_match_what_the_old_report_gave(self):
        rep = await self.report()
        self.assertEqual((rep["revenue"], rep["expense_total"], rep["net"]), (1610000, 280000, 1330000))

    async def test_investment_is_never_revenue_or_profit_for_any_company(self):
        rep = await self.report()
        self.assertEqual(rep["investment"], 500000)
        hector = next(c for c in rep["by_company"] if c["key"] == "hectorhost")
        self.assertEqual((hector["sales"], hector["other_revenue"], hector["revenue"], hector["net"]),
                         (450000, 0, 450000, 360000))
        commission = next(c for c in rep["by_company"] if c["key"] == "zarbunex")["statement_rows"]
        self.assertEqual(dict(commission), {"Commission": 60000, "General": 0})

    async def test_voided_entries_leave_every_figure_and_unassigned_money_has_its_own_row(self):
        gone = await self.db.add_transaction(1, "income", "Hosting Revenue", 99900, "x", D, company_id="matrixmc")
        await self.db.void_transaction(1, gone, "typo")
        await self.db.add_transaction(1, "income", "Hosting Revenue", 10000, "old", D)          # no company
        rep = await self.report()
        self.assertEqual(dict(rep["income"])["Sales — MatrixMC"], 250000)
        self.assertEqual(dict(rep["income"])["Sales — No company"], 10000)
        self.assertEqual(rep["by_company"][-1]["name"], "No company")
        self.assertEqual(rep["revenue"], 1620000)

    async def test_other_months_are_not_mixed_in(self):
        rep = await self.report(month="2026-10")
        self.assertEqual((rep["income"], rep["expenses"], rep["by_company"]), ([], [], []))


@unittest.skipUnless(shutil.which("pdftotext"), "pdftotext not installed")
class PdfAndDashboardTests(PnlBase):
    async def test_overall_pdf_lists_sales_and_expenses_by_company_with_a_summary_table(self):
        text = pdf_text(await asyncio.to_thread(pnl_pdf, await self.report()))
        for expected in ("Company: All companies", "Sales — Zarbunex PVT LTD", "Sales — HectorHost",
                         "Sales — MatrixMC", "HectorHost — Server Rental", "COMPANY-WISE SUMMARY",
                         "Commission — Zarbunex PVT LTD (revenue statement)"):
            self.assertIn(expected, text)
        self.assertNotIn("Hosting Revenue", text)

    async def test_company_pdf_names_the_company_and_shows_only_its_own_money(self):
        text = pdf_text(await asyncio.to_thread(pnl_pdf, await self.report("hectorhost")))
        self.assertIn("Company: HectorHost", text)
        self.assertIn("Sales — HectorHost", text)
        self.assertNotIn("Zarbunex", text)
        self.assertNotIn("MatrixMC", text)
        self.assertNotIn("COMPANY-WISE SUMMARY", text)

    async def test_a_report_without_company_data_still_renders(self):
        old = {"month": M, "company": None, "net": 0, "revenue": 0, "income": [("Hosting Revenue", 100)],
               "income_total": 100, "expenses": [], "expense_total": 0, "investment": 0,
               "statement_rows": [], "revenue_rows": [], "statement_total": 0}
        self.assertTrue((await asyncio.to_thread(pnl_pdf, old)).startswith(b"%PDF"))

    async def test_dashboard_shows_the_company_breakdown_only_for_all_companies(self):
        overall = text_of(DashboardView(None, await self.report(), 0))
        self.assertIn("By company", overall)
        self.assertIn("Sales — Zarbunex PVT LTD", overall)
        self.assertIn("HectorHost: sales", overall)
        single = text_of(DashboardView(None, await self.report("matrixmc"), 0, "matrixmc"))
        self.assertNotIn("By company", single)
        self.assertIn("Sales — MatrixMC", single)


if __name__ == "__main__":
    unittest.main()
