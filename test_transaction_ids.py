"""Permanent SALE-/REV- transaction ids and the staff-only delete flow.

Run with: python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import discord  # noqa: E402

import config  # noqa: E402
import data as store  # noqa: E402
import ui  # noqa: E402
from desk.bridge import (delete_transaction_record, inspect_transaction, mirror_sale,  # noqa: E402
                         parse_txn_id, post_company, record_finance, statement_tracker)
from desk.database import Database, revenue_number  # noqa: E402
from desk.utils import parse_items  # noqa: E402

BASE = dict(client_contact="", server_name="", server_uuid="", method="upi", transaction_id="UTR1",
            remarks="", date="2026-10-05", recorded_by="Staff")


class _Resp:
    status = 404
    reason = "x"


class FakeMessage:
    def __init__(self, channel, mid):
        self.id = mid
        self.channel = channel
        self.guild = types.SimpleNamespace(id=555)
        self.deleted = False

    async def delete(self):
        mode = self.channel.mode
        if mode == "gone":
            raise discord.NotFound(_Resp(), "Unknown Message")
        if mode == "forbidden":
            raise discord.Forbidden(_Resp(), "Missing Access")
        if mode == "error":
            raise discord.HTTPException(_Resp(), "boom")
        self.deleted = True


class FakeChannel:
    def __init__(self, cid=123, mode="ok"):
        self.id, self.mode, self.sent = cid, mode, []

    async def send(self, **kwargs):
        msg = FakeMessage(self, 9000 + len(self.sent))
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        return next((m for m in self.sent if m.id == mid), FakeMessage(self, mid))


class FakeBot:
    def __init__(self, db, channel):
        self.db, self.channel = db, channel

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    async def fetch_channel(self, cid):
        raise discord.NotFound(_Resp(), "Unknown Channel")


def _private_ledger(case):
    """Point the JSON ledger at a throw-away file (other test modules share the real path)."""
    folder = tempfile.mkdtemp(prefix="zarbot-txn-")
    original = store.DATA_FILE
    store.DATA_FILE = Path(folder) / "data.json"

    def restore():
        store.DATA_FILE = original
        shutil.rmtree(folder, ignore_errors=True)
    case.addCleanup(restore)
    return store.DATA_FILE


class LedgerIdTests(unittest.TestCase):
    def setUp(self):
        self.ledger = _private_ledger(self)

    def test_ids_are_sequential_unique_and_shown_on_the_card(self):
        a = store.add_payment(client_name="A", amount=100, company="zarbunex", service="x", **BASE)
        b = store.add_payment(client_name="B", amount=50, company="hectorhost", service="x", **BASE)
        self.assertEqual((a["sale_id"], b["sale_id"]), ("SALE-000001", "SALE-000002"))
        text = "\n".join(c["content"] for c in ui.sale_card(a, 100).to_components()[0]["components"]
                         if c["type"] == 10)
        self.assertIn("**Transaction ID:** `SALE-000001`", text)

    def test_voiding_never_frees_an_id(self):
        a = store.add_payment(client_name="A", amount=100, company="zarbunex", service="x", **BASE)
        store.void_entry("payment", a["id"], "typo")
        b = store.add_payment(client_name="B", amount=50, company="zarbunex", service="x", **BASE)
        self.assertEqual(b["sale_id"], "SALE-000002")
        self.assertEqual(store.payment_by_sale_id("SALE-000001")["voided"], True)

    def test_historical_sales_are_numbered_in_file_order(self):
        old = {"invoices": [], "expenses": [], "next_invoice": 1001, "next_entry": 3,
               "payments": [{"id": 1, "amount": 10, "company": "zarbunex", "date": "2026-01-01"},
                            {"id": 2, "amount": 20, "company": "zarbunex", "date": "2026-01-02"}]}
        with open(self.ledger, "w") as fh:
            json.dump(old, fh)
        self.assertEqual(store.ensure_sale_ids(), 2)
        self.assertEqual(store.ensure_sale_ids(), 0)  # already saved
        self.assertEqual([p["sale_id"] for p in store.all_payments()], ["SALE-000001", "SALE-000002"])
        new = store.add_payment(client_name="N", amount=5, company="zarbunex", service="x", **BASE)
        self.assertEqual(new["sale_id"], "SALE-000003")

    def test_parse_txn_id_needs_the_prefix(self):
        self.assertEqual(parse_txn_id(" sale-12 "), "SALE-000012")
        self.assertEqual(parse_txn_id("REV-0003"), "REV-000003")
        for bad in ("12", "", "INV-1", "SALE-", "SALE-0", "SALE-1; DROP"):
            self.assertIsNone(parse_txn_id(bad))


class DeleteFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _private_ledger(self)
        self.dir = tempfile.mkdtemp(prefix="zarbot-txn-db-")
        self.db = Database(os.path.join(self.dir, "t.db"))
        await self.db.connect()
        self.channel = FakeChannel()
        self.bot = FakeBot(self.db, self.channel)
        self.company = config.get_company("zarbunex")
        config.COMPANIES["zarbunex"] = type(self.company)(
            key="zarbunex", name=self.company.name, statement_channel_id=123)

    async def asyncTearDown(self):
        config.COMPANIES["zarbunex"] = self.company
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _post_sale(self, client="A", amount=100, company="zarbunex"):
        sale = store.add_payment(client_name=client, amount=amount, company=company, service="VPS", **BASE)
        await mirror_sale(self.bot, sale, 1)
        sale = store.payment_by_sale_id(sale["sale_id"])
        await post_company(self.bot, company, ui.sale_card(sale, 0),
                           track=statement_tracker(self.bot, sale["sale_id"], "sale", company, sale["id"]))
        return sale

    async def _post_revenue(self, amount=250000):
        rid = await self.db.add_revenue(1, "zarbunex", "Commission", amount, "d", "2026-10-05",
                                        received_from="Acme")
        number = revenue_number(rid)
        await post_company(self.bot, "zarbunex", ui.PersistentView(timeout=None),
                           track=statement_tracker(self.bot, number, "revenue", "zarbunex", rid))
        return number

    async def test_statement_message_ids_are_stored(self):
        sale = await self._post_sale()
        row = await self.db.statement_txn(sale["sale_id"])
        self.assertEqual((row["company_id"], row["record_id"], row["guild_id"], row["channel_id"]),
                         ("zarbunex", sale["id"], 555, 123))
        self.assertEqual(row["message_id"], self.channel.sent[0].id)
        number = await self._post_revenue()
        self.assertEqual((await self.db.statement_txn(number))["kind"], "revenue")

    async def test_delete_sale_updates_every_total_and_message(self):
        keep = await self._post_sale("Keep", 40)
        sale = await self._post_sale("Wrong", 100)
        self.assertEqual(store.total_sales(company="zarbunex"), 140)
        report = await self.db.month_report("2026-10", "zarbunex")
        self.assertEqual(report["income_total"], 14000)

        result = await delete_transaction_record(self.bot, sale["sale_id"], 42, "entered twice")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["message_state"], "removed")
        self.assertTrue(self.channel.sent[1].deleted)
        self.assertFalse(self.channel.sent[0].deleted)
        self.assertEqual(store.total_sales(company="zarbunex"), 40)
        self.assertEqual([p["sale_id"] for p in store.payments()], [keep["sale_id"]])
        report = await self.db.month_report("2026-10", "zarbunex")
        self.assertEqual((report["income_total"], report["net"]), (4000, 4000))
        # record kept for audit, with who / when / why
        row = await self.db.statement_txn(sale["sale_id"])
        self.assertEqual((row["deleted_by"], row["delete_reason"]), (42, "entered twice"))
        self.assertTrue(row["deleted_at"])
        audit = [a for a in await self.db.recent_audit(20) if a["action"] == "statement.delete"]
        self.assertEqual(audit[0]["actor_id"], 42)
        self.assertIn("entered twice", audit[0]["details"])

    async def test_delete_revenue_leaves_revenue_totals_and_pnl(self):
        number = await self._post_revenue(250000)
        other = await self._post_revenue(100000)
        self.assertEqual((await self.db.revenue_summary("zarbunex"))["operating"], 350000)
        result = await delete_transaction_record(self.bot, number, 7, "wrong amount")
        self.assertTrue(result["ok"], result)
        self.assertEqual((await self.db.revenue_summary("zarbunex"))["operating"], 100000)
        report = await self.db.month_report("2026-10", "zarbunex")
        self.assertEqual(report["revenue"], 100000)
        self.assertEqual((await inspect_transaction(self.bot, other))["record_voided"], False)

    async def test_company_separation_and_exact_id(self):
        a = await self._post_sale("A", 100, "zarbunex")
        b = await self._post_sale("B", 100, "hectorhost")
        await delete_transaction_record(self.bot, a["sale_id"], 1, "dup")
        self.assertEqual(store.total_sales(company="zarbunex"), 0)
        self.assertEqual(store.total_sales(company="hectorhost"), 100)
        self.assertFalse(store.payment_by_sale_id(b["sale_id"]).get("voided"))

    async def test_unknown_id_changes_nothing(self):
        sale = await self._post_sale()
        result = await delete_transaction_record(self.bot, "SALE-999999", 1, "x")
        self.assertFalse(result["ok"])
        self.assertEqual(store.total_sales(), sale["amount"])

    async def test_already_gone_message_still_corrects_books(self):
        sale = await self._post_sale()
        self.channel.mode = "gone"
        result = await delete_transaction_record(self.bot, sale["sale_id"], 1, "x")
        self.assertTrue(result["ok"])
        self.assertEqual(result["message_state"], "missing")
        self.assertEqual(store.total_sales(), 0)

    async def test_discord_failure_keeps_books_correct_and_is_retryable(self):
        sale = await self._post_sale()
        self.channel.mode = "forbidden"
        first = await delete_transaction_record(self.bot, sale["sale_id"], 1, "x")
        self.assertTrue(first["ok"])
        self.assertEqual(first["message_state"], "failed")
        self.assertEqual(store.total_sales(), 0)  # books already right
        self.channel.mode = "ok"
        again = await delete_transaction_record(self.bot, sale["sale_id"], 1, "x")
        self.assertEqual(again["message_state"], "removed")
        self.assertTrue(self.channel.sent[0].deleted)
        third = await delete_transaction_record(self.bot, sale["sale_id"], 1, "x")
        self.assertTrue(third["noop"])  # never double-processed
        audit = [a for a in await self.db.recent_audit(50) if a["action"] == "statement.delete"]
        self.assertEqual(len(audit), 1)

    async def test_finishes_a_half_done_delete(self):
        sale = await self._post_sale()
        info = await inspect_transaction(self.bot, sale["sale_id"])
        await self.db.void_transaction(1, info["tx_id"], "half")  # only the finance side was voided
        self.assertFalse(info["already_deleted"])
        result = await delete_transaction_record(self.bot, sale["sale_id"], 1, "finish")
        self.assertTrue(result["ok"])
        self.assertEqual(store.total_sales(), 0)

    async def test_untracked_historical_sale_is_database_only(self):
        sale = store.add_payment(client_name="Old", amount=30, company="zarbunex", service="x", **BASE)
        await self.db.backfill_statement_txns(store.all_payments())
        result = await delete_transaction_record(self.bot, sale["sale_id"], 1, "old")
        self.assertTrue(result["ok"])
        self.assertEqual(result["message_state"], "untracked")
        self.assertEqual(store.total_sales(), 0)

    async def test_invoice_payment_is_protected_until_the_invoice_is_void(self):
        items = parse_items("VPS | 1 | 700 | type=VPS")
        iid = await self.db.create_invoice(
            1, client_discord_id=2, client_name="Flame", items=items, due_days=7, tax_percent=0,
            discount_cents=0, category="Hosting Revenue", notes="", ticket_channel_id=None,
            company_id="zarbunex")
        inv = await self.db.get_invoice(iid)
        await self.db.add_payment(1, iid, inv["total"], "UPI", "paid")
        inv = await self.db.get_invoice(iid)
        from desk.bridge import sale_from_payment
        sale = store.add_payment(**sale_from_payment(inv, inv["payments"][0]))
        blocked = await delete_transaction_record(self.bot, sale["sale_id"], 1, "x")
        self.assertFalse(blocked["ok"])
        self.assertIn("Void that invoice first", blocked["error"])
        self.assertEqual(store.total_sales(), sale["amount"])
        await self.db.void_invoice(1, iid, "cancelled")
        done = await delete_transaction_record(self.bot, sale["sale_id"], 1, "cancelled")
        self.assertTrue(done["ok"], done)
        self.assertEqual(store.total_sales(), 0)

    async def test_quick_invoice_payment_is_protected(self):
        inv = store.create_invoice("C", "", 500, "2026-12-01", "VPS", "Staff", company="zarbunex")
        _invoice, sale = store.mark_paid(inv["number"], "upi", "", "", "", "", "Staff", company="zarbunex")
        result = await delete_transaction_record(self.bot, sale["sale_id"], 1, "x")
        self.assertFalse(result["ok"])
        self.assertIn(inv["number"], result["error"])
        self.assertEqual(store.total_sales(), 500)

    async def test_finance_income_statement_is_tracked_and_deletable(self):
        txn = await self.db.add_transaction(1, "income", "Hosting Revenue", 20000, "d", "2026-10-05",
                                            company_id="zarbunex")
        ok, _ = await record_finance(self.bot, "income", "Hosting Revenue", 20000, "d", "2026-10-05",
                                     "Staff", company_id="zarbunex", txn_id=txn)
        self.assertTrue(ok)
        sale = store.all_payments()[-1]
        self.assertTrue((await self.db.statement_txn(sale["sale_id"]))["message_id"])
        await delete_transaction_record(self.bot, sale["sale_id"], 1, "oops")
        self.assertEqual((await self.db.month_report("2026-10", "zarbunex"))["income_total"], 0)
        self.assertEqual(store.total_sales(), 0)


if __name__ == "__main__":
    unittest.main()
