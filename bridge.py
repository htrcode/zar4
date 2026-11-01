"""Glue between the SQLite desk (itemised invoices, tickets, finance) and the statement ledger.

* Every payment recorded on an itemised invoice becomes a **Sales Statement**
  card, and every new itemised invoice is announced — both only in the
  destination channel of the invoice's **company**.
* Sales / expenses recorded with ``/sale add``, ``/expense add``, ``/revenue``
  or the quick invoice "Mark as paid" flow are mirrored into the SQLite books
  (under the same company) so the finance dashboard and P&L include them.
* ``/finance income`` and ``/finance expense`` entries are mirrored back into
  the ledger and posted as statements, again per company.

Company routing lives in :func:`post_company`: it resolves the configured
channel id, reports missing channels / permissions / Discord API errors to the
caller and never falls back to another company's channel.
"""
import logging
import re

import discord

import config
import data as store
import ui
from desk.database import invoice_number, primary_service, revenue_number, ticket_number
from desk.utils import dmy, fmt, to_cents

log = logging.getLogger("invoice-bot.bridge")


# ---------------------------------------------------------------------------
# posting
# ---------------------------------------------------------------------------
async def _channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except discord.HTTPException:
        log.warning("Channel %s is missing or not visible; skipping post.", channel_id)
        return None


async def post_to(bot, primary_id, view_factory) -> int:
    """Send a freshly built card to ``primary_id`` plus any TRANSACTION_CHANNEL_IDS."""
    targets = [c for c in dict.fromkeys([primary_id, *config.TRANSACTION_CHANNEL_IDS]) if c]
    if not targets:
        log.warning("No channel configured for this card (set STATEMENTS_CHANNEL_ID / INVOICE_CHANNEL_ID).")
        return 0
    sent = 0
    for channel_id in targets:
        channel = await _channel(bot, channel_id)
        if channel is None:
            continue
        try:
            await channel.send(view=view_factory())
            sent += 1
        except discord.HTTPException:
            log.exception("Could not post to channel %s", channel_id)
    return sent


async def post_company(bot, company_key, view, *, kind: str = "statement",
                       track=None) -> tuple[bool, str]:
    """Post ``view`` to a company's own channel only.

    Returns ``(ok, message)``: on success ``message`` is the channel mention, on
    failure it is a short, user-safe explanation (unset channel, missing channel
    or insufficient permissions, Discord API error).

    ``track`` (see :func:`statement_tracker`) is awaited once with the posted Discord
    message, or with ``None`` if nothing was posted, so the statement can be found later.
    """
    sent: list = []
    result = await _post_company(bot, company_key, view, kind, sent)
    if track is not None:
        await track(sent[0] if sent else None)
    return result


async def _post_company(bot, company_key, view, kind, sent) -> tuple[bool, str]:
    company = config.get_company(company_key)
    if company is None:
        return False, "That record has no company yet, so there is no channel to post to."
    channel_id = company.channel_id(kind)
    env_name = company.statement_env if kind == "statement" else company.invoice_env
    if not channel_id:
        return False, (f"No channel is configured for **{company.name}**. "
                       f"Set `{env_name}` to that channel's id, then try again.")
    channel = await ui.resolve_channel(bot, channel_id)
    if channel is None:
        return False, (f"I could not open channel `{channel_id}` for **{company.name}**. "
                       f"Check `{env_name}` and give me **View Channel** there.")
    try:
        sent.append(await channel.send(view=view))
    except discord.HTTPException as exc:
        log.warning("Could not post to %s (%s): %s", channel_id, company.name, exc)
        return False, (f"I could not send to <#{channel_id}> for **{company.name}** — "
                       "check my **Send Messages** permission there.")
    return True, f"<#{channel_id}>"


# ---------------------------------------------------------------------------
# invoice events -> cards
# ---------------------------------------------------------------------------
def _contact(inv: dict) -> str:
    if inv.get("client_contact"):
        return inv["client_contact"]
    return f"<@{inv['client_discord_id']}>" if inv.get("client_discord_id") else ""


def _service_label(item: dict) -> str:
    label = " · ".join(x for x in (item.get("product_type"), item.get("plan")) if x)
    return label or item.get("description") or ""


def invoice_card(inv: dict, ticket_label):
    item = primary_service(inv)
    f = ui._field
    lines = [
        f("Company", config.company_name(inv.get("company_id"))),
        f("Client Name", ui._val(inv["client_name"])),
        f("Client Contact", ui._val(_contact(inv))),
        f("Product", ui._val(item.get("product_type"))),
        f("Server Name", ui._val(item.get("service_name") or item.get("description"))),
        f("Node", ui._val(item.get("node"))),
        f("Server ID", ui._val(item.get("server_id"), code=True)),
        f("Plan", ui._val(item.get("plan"))),
        f("Amount", fmt(inv["total"], inv["currency"])),
        f("Due Date", dmy(inv["due_date"])),
        f("Invoice", inv["number"]),
        f("Ticket", ticket_label or "—"),
    ]
    items = ui.title_and_body(f"New Invoice · {inv['number']}", "\n".join(lines))
    ui.footer_items(items, f"Raised by <@{inv['created_by']}> · {dmy(inv['issue_date'])}")
    view = ui.PersistentView(timeout=None)
    view.add_item(discord.ui.Container(*items))
    return view


def sale_from_payment(inv: dict, payment: dict) -> dict:
    """Ledger fields for an invoice payment (the invoice's company is carried over)."""
    item = primary_service(inv)
    remarks = [inv["number"]]
    if payment["paid_at"] > inv["due_date"]:
        remarks.append("Late renewal")
    if payment.get("note"):
        remarks.append(payment["note"])
    return dict(
        client_name=inv["client_name"],
        client_contact=_contact(inv),
        server_name=item.get("service_name") or item.get("description") or "",
        server_uuid=item.get("server_id") or "",
        service=_service_label(item),
        amount=payment["amount"] / 100,
        method=payment.get("method") or "manual",
        transaction_id=payment.get("transaction_id") or "",
        remarks=" · ".join(remarks),
        date=payment["paid_at"],
        recorded_by=f"<@{payment['created_by']}>",
        company=inv.get("company_id") or "",
        extra={"source": "desk", "ref": f"desk-payment:{payment['id']}", "invoice": inv["number"]},
    )


async def post_transaction_event(bot, event: str, payload: dict):
    """Database event listener: invoice created / paid / voided."""
    if event not in ("invoice.created", "payment", "invoice.void"):
        return
    inv = await bot.db.get_invoice(payload["invoice_id"])
    if not inv:
        return
    if event == "invoice.void":
        await void_invoice_sales(bot, inv, payload.get("reason") or "")
        return
    if event == "invoice.created":
        ticket = None
        if inv.get("ticket_channel_id"):
            ticket = await bot.db.ticket_by_channel(inv["ticket_channel_id"])
        label = ticket_number(ticket["id"]) if ticket else None
        if config.get_company(inv.get("company_id")) is None:
            # Invoices created before multi-company support keep the old routing.
            await post_to(bot, config.INVOICE_CHANNEL_ID, lambda: invoice_card(inv, label))
            return
        ok, message = await post_company(bot, inv["company_id"], invoice_card(inv, label), kind="invoice")
        if not ok:
            log.warning("Invoice %s was not posted: %s", inv["number"], message)
        return

    payment = next((p for p in inv["payments"] if p["id"] == payload.get("payment_id")), None)
    if not payment:
        return
    ref = f"desk-payment:{payment['id']}"
    if store.has_ref(ref):
        return
    sale = store.add_payment(**sale_from_payment(inv, payment))
    company = config.get_company(sale.get("company"))
    total = store.total_sales(company=sale.get("company") or None)
    if company is None:
        await post_to(bot, config.STATEMENTS_CHANNEL_ID, lambda: ui.sale_card(sale, total))
        return
    ok, message = await post_company(
        bot, company.key, ui.sale_card(sale, total),
        track=statement_tracker(bot, sale.get("sale_id"), "sale", company.key, sale.get("id")))
    if not ok:
        log.warning("Sales statement %s was not posted: %s", inv["number"], message)


# ---------------------------------------------------------------------------
# ledger -> SQLite books
# ---------------------------------------------------------------------------
async def mirror_sale(bot, sale: dict, actor_id: int):
    """Make a ledger sale count as income in the SQLite books (dashboard / P&L).

    Idempotent: a sale that already carries a ``ref`` is linked to an existing finance
    entry (``tx:`` / ``desk-payment:``) and is never counted a second time. Voided sales
    are mirrored as voided entries, so they stay in audit history but out of every total.
    """
    db = getattr(bot, "db", None)
    if db is None or sale.get("source") in ("desk", "finance") or sale.get("ref"):
        return None
    try:
        await db.ensure_category("Hosting Revenue", "income")
        text = f"Sale: {sale.get('client_name') or '—'} — {sale.get('service') or 'service'}"
        tx_id = await db.add_transaction(actor_id, "income", "Hosting Revenue", to_cents(sale["amount"]),
                                         text, store._entry_date(sale),
                                         company_id=sale.get("company") or None,
                                         voided=bool(sale.get("voided")))
        # Link the two books so voiding either side cancels the statement entry too.
        if tx_id and sale.get("id"):
            store.set_ref("payment", sale["id"], f"tx:{tx_id}")
        return tx_id
    except Exception:
        log.exception("Could not mirror the sale into the finance books")


async def void_invoice_sales(bot, inv: dict, reason: str = "") -> int:
    """Void the Sales Statements posted for ``inv``'s payments (records kept for audit)."""
    reason = (reason or "").strip() or f"Invoice {inv.get('number') or inv.get('id')} was voided"
    voided = 0
    refs = {f"desk-payment:{p['id']}" for p in inv.get("payments") or []}
    if not refs:
        return 0
    for sale in store.all_payments():
        if sale.get("ref") in refs and not sale.get("voided"):
            try:
                store.void_entry("payment", sale["id"], reason)
                voided += 1
            except ValueError:
                continue
    return voided


async def reconcile_sales(bot, actor_id: int = 0) -> dict:
    """Align the statement ledger and the SQLite books on startup.

    Finances live in two places: the JSON statements ledger (what ``/sale`` posts) and
    the SQLite ``transactions`` books (what the dashboard, P&L, gross sales and the
    transaction history read). A sale that never reached the books was silently missing
    from every report, and an invoice payment that never reached the ledger was missing
    from the Sales Statements. This links both directions once, and is idempotent: the
    ``ref`` / ``payment_id`` links make a second run add nothing.

    Returns ``{"book_entries", "expense_entries", "ledger_sales", "voided_entries"}``.
    """
    db = getattr(bot, "db", None)
    stats = {"book_entries": 0, "expense_entries": 0, "ledger_sales": 0, "voided_entries": 0}
    if db is None:
        return stats

    # 1) itemised-invoice payments: make sure each has its income entry and Sales Statement.
    refs = {str(s.get("ref")) for s in store.all_payments() if s.get("ref")}
    invoice_ids = [row["invoice_id"] for row in await db.fetchall(
        "SELECT DISTINCT invoice_id FROM payments ORDER BY invoice_id")]
    for invoice_id in invoice_ids:
        inv = await db.get_invoice(invoice_id)
        if not inv:
            continue
        if inv.get("voided"):
            # A voided invoice must not leave an active Sales Statement in the totals.
            await void_invoice_sales(bot, inv, f"Invoice {inv['number']} is void")
            continue
        adopted: set = set()
        for payment in inv.get("payments") or []:
            row = await db.fetchone(
                "SELECT id FROM transactions WHERE payment_id=?", (payment["id"],))
            if row is None:
                await db.add_transaction(
                    actor_id, "income", inv["category"], payment["amount"],
                    f"Payment for {inv['number']} ({inv['client_name']})", payment["paid_at"],
                    company_id=inv.get("company_id"))
                stats["book_entries"] += 1
            ref = f"desk-payment:{payment['id']}"
            if ref in refs:
                continue
            # Never duplicate a Sales Statement already recorded without its link. A sale
            # linked to a *different* payment is not adopted, so equal part-payments do not
            # collapse into one.
            existing = next(
                (s for s in store.all_payments()
                 if s.get("id") not in adopted
                 and str(s.get("invoice") or "") == inv["number"]
                 and abs(float(s.get("amount") or 0) - payment["amount"] / 100) < 0.005
                 and (not s.get("ref") or s.get("ref") == ref)), None)
            if existing is not None:
                adopted.add(existing.get("id"))
                if not existing.get("ref"):
                    store.set_ref("payment", existing["id"], ref)
                    refs.add(ref)
                continue
            store.add_payment(**sale_from_payment(inv, payment))
            refs.add(ref)
            stats["ledger_sales"] += 1

    # 2) statement-ledger sales: make sure each non-desk sale is in the books. Voided
    #    ones are kept as voided entries, so they stay in audit history only.
    for sale in store.all_payments():
        added = await mirror_sale(bot, sale, actor_id)
        if added:
            if sale.get("voided"):
                stats["voided_entries"] += 1
            else:
                stats["book_entries"] += 1

    # 3) the same alignment for expense statements.
    for expense in store.all_expenses():
        added = await mirror_expense(bot, expense, actor_id)
        if added:
            if expense.get("voided"):
                stats["voided_entries"] += 1
            else:
                stats["expense_entries"] += 1

    if any(stats.values()):
        log.info("Books reconciliation: %s income, %s expense, %s voided entries, %s ledger sales",
                 stats["book_entries"], stats["expense_entries"], stats["voided_entries"],
                 stats["ledger_sales"])
    return stats


async def mirror_expense(bot, expense: dict, actor_id: int):
    """Make a ledger expense count in the SQLite books; idempotent through ``ref``."""
    db = getattr(bot, "db", None)
    if db is None or expense.get("source") == "finance" or expense.get("ref"):
        return None
    try:
        category = expense.get("category") or "Other Expenses"
        await db.ensure_category(category, "expense")
        text = f"{expense.get('description') or category} — {expense.get('paid_to') or ''}".strip(" —")
        tx_id = await db.add_transaction(actor_id, "expense", category, to_cents(expense["amount"]),
                                         text, str(expense.get("date", ""))[:10],
                                         company_id=expense.get("company") or None,
                                         voided=bool(expense.get("voided")))
        if tx_id and expense.get("id"):
            store.set_ref("expense", expense["id"], f"tx:{tx_id}")
        return tx_id
    except Exception:
        log.exception("Could not mirror the expense into the finance books")


# ---------------------------------------------------------------------------
# SQLite finance commands -> ledger + statements
# ---------------------------------------------------------------------------
async def record_finance(bot, kind: str, category: str, amount_cents: int, description: str,
                         date: str, actor_name: str, company_id: str | None = None,
                         txn_id: int | None = None):
    """Mirror ``/finance income|expense`` into the ledger and post its statement.

    Returns ``(ok, message)`` from :func:`post_company`.
    """
    amount = amount_cents / 100
    if kind == "income":
        sale = store.add_payment(
            client_name="", client_contact="", server_name="", server_uuid="", service=category,
            amount=amount, method="other", transaction_id="", remarks=description, date=date,
            recorded_by=actor_name, company=company_id or "", extra={"source": "finance"})
        if txn_id and sale.get("id"):
            store.set_ref("payment", sale["id"], f"tx:{txn_id}")
        if config.get_company(sale.get("company")) is None:
            if not config.STATEMENTS_CHANNEL_ID:
                return False, "no company was given and `STATEMENTS_CHANNEL_ID` is not set."
            await post_to(bot, config.STATEMENTS_CHANNEL_ID,
                          lambda: ui.sale_card(sale, store.total_sales()))
            return True, f"<#{config.STATEMENTS_CHANNEL_ID}>"
        return await post_company(
            bot, sale["company"], ui.sale_card(sale, store.total_sales(company=sale["company"])),
            track=statement_tracker(bot, sale.get("sale_id"), "sale", sale["company"], sale.get("id")))
    expense = store.add_expense(
        description=description or category, category=category, amount=amount, method="other",
        paid_to="", transaction_id="", remarks="", date=date, recorded_by=actor_name,
        company=company_id or "", extra={"source": "finance"})
    if txn_id and expense.get("id"):
        store.set_ref("expense", expense["id"], f"tx:{txn_id}")
    if config.get_company(expense.get("company")) is None:
        if not config.STATEMENTS_CHANNEL_ID:
            return False, "no company was given and `STATEMENTS_CHANNEL_ID` is not set."
        await post_to(bot, config.STATEMENTS_CHANNEL_ID,
                      lambda: ui.expense_card(expense, store.total_expenses()))
        return True, f"<#{config.STATEMENTS_CHANNEL_ID}>"
    return await post_company(
        bot, expense["company"], ui.expense_card(expense, store.total_expenses(company=expense["company"])))


# ---------------------------------------------------------------------------
# permanent transaction ids: tracking the posted statement, lookup and deletion
# ---------------------------------------------------------------------------
_TXN_ID = re.compile(r"^\s*(SALE|REV)[-\s]?(\d{1,9})\s*$", re.IGNORECASE)


def parse_txn_id(text) -> str | None:
    """``sale-12`` / ``SALE-000012`` -> ``SALE-000012``; ``REV-0003`` -> ``REV-000003``.

    The prefix is mandatory (a bare number could mean either book); anything else is None.
    """
    found = _TXN_ID.match(text or "")
    if found is None or int(found.group(2)) <= 0:
        return None
    number = int(found.group(2))
    return store.sale_number(number) if found.group(1).upper() == "SALE" else revenue_number(number)


async def register_statement(bot, txn_id, kind, company_id, record_id, message=None) -> None:
    """Best effort: store the id, its record and the Discord message that announced it."""
    db = getattr(bot, "db", None)
    if db is None or not txn_id or not record_id:
        return
    try:
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        await db.register_statement(
            txn_id, kind, company_id, record_id,
            guild_id=getattr(guild, "id", None) or getattr(message, "guild_id", None),
            channel_id=getattr(channel, "id", None) or getattr(message, "channel_id", None),
            message_id=getattr(message, "id", None))
    except Exception:
        log.exception("Could not store the Discord message of %s", txn_id)


def statement_tracker(bot, txn_id, kind, company_id, record_id):
    """Callback for ``post_company(..., track=...)`` / ``_send_statement``."""
    async def track(message) -> None:
        await register_statement(bot, txn_id, kind, company_id, record_id, message)
    return track


def _blocked_by_invoice(sale: dict, desk_invoice) -> str | None:
    """Why a sale must not be deleted on its own (it is the payment of a live invoice)."""
    if desk_invoice is not None:
        if desk_invoice["voided"]:
            return None
        return (f"This sale is the payment on invoice **{invoice_number(desk_invoice['invoice_id'])}**. "
                "Void that invoice first (`/invoice void`), then delete this statement.")
    number = str(sale.get("invoice") or "")
    quick = store.invoice(number) if number else None
    if quick is not None and quick.get("status") == "paid" and not sale.get("voided"):
        return (f"This sale marks quick invoice **{number}** as paid. Deleting it would leave that "
                "invoice marked paid with no payment, so it is blocked.")
    return None


async def inspect_transaction(bot, txn_id: str) -> dict | None:
    """Look a transaction up by its exact id and say whether (and how) it can be deleted.

    Returns None when no such transaction exists. Keys: txn_id, kind, record_id, company,
    amount (rupees), date, party, detail, recorded_by, record_voided, tx_id, tx_voided,
    registry, blocked (reason or None) and already_deleted.
    """
    db = bot.db
    registry = await db.statement_txn(txn_id)
    if txn_id.startswith("SALE-"):
        sale = store.payment_by_sale_id(txn_id)
        if sale is None:
            return None
        ref = str(sale.get("ref") or "")
        desk_invoice = None
        tx_id = tx_voided = None
        if ref.startswith("desk-payment:"):
            desk_invoice = await db.fetchone(
                "SELECT p.invoice_id, i.voided FROM payments p JOIN invoices i ON i.id=p.invoice_id "
                "WHERE p.id=?", (int(ref.split(":", 1)[1]),))
        elif ref.startswith("tx:"):
            tx_id = int(ref[3:])
            row = await db.fetchone("SELECT voided FROM transactions WHERE id=?", (tx_id,))
            tx_voided = bool(row["voided"]) if row else None
            if row is None:
                tx_id = None
        info = {
            "txn_id": txn_id, "kind": "sale", "record_id": sale["id"],
            "company": sale.get("company") or None, "amount": float(sale["amount"]),
            "date": store._entry_date(sale),
            "party": sale.get("client_name") or sale.get("customer") or "—",
            "detail": sale.get("service") or "—", "recorded_by": sale.get("recorded_by") or "—",
            "record_voided": bool(sale.get("voided")), "tx_id": tx_id, "tx_voided": tx_voided,
            "registry": registry, "blocked": _blocked_by_invoice(sale, desk_invoice),
            "linked": bool(ref),
        }
        info["already_deleted"] = info["record_voided"] and tx_voided is not False
        return info
    revenue = await db.get_revenue(int(txn_id.split("-", 1)[1]))
    if revenue is None:
        return None
    return {
        "txn_id": txn_id, "kind": "revenue", "record_id": revenue["id"],
        "company": revenue.get("company_id") or None, "amount": (revenue["amount"] or 0) / 100,
        "date": revenue.get("occurred_on") or "", "party": revenue.get("received_from") or "—",
        "detail": revenue.get("category") or "—", "recorded_by": revenue.get("recorded_by") or "—",
        "record_voided": bool(revenue["voided"]), "tx_id": None, "tx_voided": None,
        "registry": registry, "blocked": None, "linked": True,
        "already_deleted": bool(revenue["voided"]),
    }


async def remove_statement_message(bot, channel_id, message_id) -> tuple[str, str]:
    """Delete the posted statement using its stored ids (works in any server the bot is in).

    Returns ``(state, text)`` where state is removed / missing (already gone) / untracked
    (ids were never stored) / failed (Discord refused or errored; safe to retry).
    """
    if not channel_id or not message_id:
        return "untracked", "No Discord message was stored for it (posted before ids were tracked)."
    channel = bot.get_channel(channel_id)
    try:
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        await channel.get_partial_message(message_id).delete()
    except discord.NotFound:
        return "missing", "The Discord message was already gone."
    except discord.Forbidden:
        return "failed", (f"I am not allowed to delete the message in <#{channel_id}> "
                          "(needs View Channel + Read Message History).")
    except (discord.HTTPException, AttributeError) as exc:
        log.warning("Could not delete message %s in %s: %s", message_id, channel_id, exc)
        return "failed", f"Discord would not delete the message in <#{channel_id}> right now."
    return "removed", "The statement message was removed from Discord."


async def delete_transaction_record(bot, txn_id: str, actor_id: int, reason: str) -> dict:
    """Void one transaction everywhere it counts, audit it, then remove its Discord statement.

    Records are never erased: the sale/revenue row, and the finance entry mirrored from a sale,
    are flagged void so every total, statement, dashboard and P&L stops counting them. Every
    step checks the current state first, so a run that failed half-way is finished by running
    it again, and running it twice never double-counts. The database is corrected before
    Discord is touched, so a Discord failure can never leave the books wrong.

    Returns ``{"ok", "error", "info", "message_state", "message_text", "noop"}``.
    """
    out = {"ok": False, "error": None, "info": None, "message_state": None,
           "message_text": "", "noop": False}
    reason = (reason or "").strip()
    if not reason:
        out["error"] = "A reason is required."
        return out
    db = bot.db
    try:
        info = await inspect_transaction(bot, txn_id)
    except Exception:
        log.exception("Could not look up %s", txn_id)
        out["error"] = "The database could not be read. Nothing was changed; try again."
        return out
    out["info"] = info
    if info is None:
        out["error"] = f"There is no transaction `{txn_id}`."
        return out
    if info["blocked"]:
        out["error"] = info["blocked"]
        return out

    registry = info["registry"] or {}
    first_delete = not registry.get("deleted_at")
    try:
        if info["kind"] == "sale":
            if info["tx_id"] and info["tx_voided"] is False:
                try:
                    await db.void_transaction(actor_id, info["tx_id"], reason)
                except ValueError as exc:
                    if "already void" not in str(exc).lower():
                        raise
            if not info["record_voided"]:
                try:
                    store.void_entry("payment", info["record_id"], reason)
                except ValueError as exc:
                    if "already void" not in str(exc).lower():
                        raise
        elif not info["record_voided"]:
            try:
                await db.void_revenue(actor_id, info["record_id"], reason)
            except ValueError as exc:
                if "already void" not in str(exc).lower():
                    raise
        if first_delete:
            await db.record_statement_delete(
                actor_id, txn_id, info["kind"], info["company"], info["record_id"],
                round(info["amount"] * 100), reason,
                {"channel_id": registry.get("channel_id"), "message_id": registry.get("message_id"),
                 "guild_id": registry.get("guild_id"), "finance_entry": info["tx_id"]})
    except Exception as exc:
        log.exception("Could not delete %s", txn_id)
        out["error"] = (f"The books could not be fully corrected ({type(exc).__name__}). Nothing was "
                        "removed from Discord. Run the command again to finish the job.")
        return out

    state = registry.get("message_state")
    if not first_delete and state in ("removed", "missing", "untracked"):
        out.update(ok=True, noop=True, message_state=state)
        return out
    state, text = await remove_statement_message(bot, registry.get("channel_id"), registry.get("message_id"))
    try:
        await db.set_statement_message_state(actor_id, txn_id, state)
    except Exception:
        log.exception("Could not store the message state of %s", txn_id)
    out.update(ok=True, message_state=state, message_text=text)
    return out
