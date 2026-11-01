"""Finance commands: sales (payments received), expenses and revenue statements.

Everything produced here — Sales, Expense and Revenue Statements, whether a
single entry or a period report — is posted to the **selected company's** own
statement channel and never to another company's channel. A company must be
picked explicitly: nothing defaults to the first company in the list.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import data as store
import ui
from desk.bridge import (delete_transaction_record, inspect_transaction, mirror_expense,
                         mirror_sale, parse_txn_id, post_company, statement_tracker)
from desk.database import revenue_number
from desk.utils import duplicate_submission, is_staff, staff_interaction_check, to_cents

log = logging.getLogger("invoice-bot.statements")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# REV-0003, REV3 or 3 — the id printed on every Revenue Statement.
_REVENUE_ID = re.compile(r"^\s*(?:REV[-\s]?)?(\d{1,9})\s*$", re.IGNORECASE)
# EXP-0003, EXP3 or 3 — the id /expense-list prints for an Expense Statement.
_EXPENSE_ID = re.compile(r"^\s*#?(?:EXP[-\s]?)?(\d{1,9})\s*$", re.IGNORECASE)


def expense_number(entry_id: int) -> str:
    return f"EXP-{entry_id:04d}"

_PERIOD_CHOICES = [
    app_commands.Choice(name=label, value=key) for key, label in store.PERIODS.items()
]

_METHOD_CHOICES = [
    app_commands.Choice(name="UPI", value="upi"),
    app_commands.Choice(name="Bank transfer", value="bank transfer"),
    app_commands.Choice(name="Card", value="card"),
    app_commands.Choice(name="Cash", value="cash"),
    app_commands.Choice(name="Other", value="other"),
]

_CATEGORY_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in (
        "Software & subscriptions",
        "Hosting & servers",
        "Domains",
        "Travel",
        "Office",
        "Equipment",
        "Marketing",
        "Professional services",
        "Utilities",
        "General",
    )
]

_COMPANY_CHOICES = [
    app_commands.Choice(name=company.name, value=company.key) for company in config.COMPANIES.values()
]
_REVENUE_CHOICES = [
    app_commands.Choice(name=name, value=name) for name in ("Commission", "Investment", "General")
]


async def _bad(interaction: discord.Interaction, title: str, body: str) -> None:
    await interaction.response.send_message(view=ui.notice(title, body, error=True), ephemeral=True)


async def _statement_channel(interaction: discord.Interaction, company: config.Company):
    """Resolve the company's statement channel, replying with setup help when it is unavailable."""
    channel = await ui.resolve_channel(interaction.client, company.channel_id("statement"))
    if channel is None:
        await interaction.response.send_message(
            view=ui.missing_company_channel_notice(company), ephemeral=True
        )
    return channel


async def _send_statement(
    interaction: discord.Interaction,
    channel,
    company: config.Company,
    card: discord.ui.LayoutView,
    *,
    saved: bool,
    done_title: str,
    track=None,
) -> None:
    """Send ``card`` to the company channel and confirm to the user.

    ``track`` (see ``statement_tracker``) receives the posted message, or None on failure.
    """
    try:
        posted = await channel.send(view=card)
    except discord.HTTPException:
        if track is not None:
            await track(None)
        await interaction.response.send_message(
            view=ui.notice(
                "Could not post statement",
                ("It was saved, but I" if saved else "I")
                + f" could not send it to <#{channel.id}> for **{company.name}** — "
                "check my **Send Messages** permission there.",
                error=True,
            ),
            ephemeral=True,
        )
        return
    if track is not None:
        await track(posted)
    await interaction.response.send_message(
        view=ui.notice(done_title, f"Posted to <#{channel.id}> ({company.name})."),
        ephemeral=True,
    )


class _DeleteConfirm(ui.PersistentView):
    """Ephemeral preview + confirmation; only the staff member who asked can confirm."""

    def __init__(self, bot, info: dict, actor_id: int, reason: str, interaction: discord.Interaction):
        super().__init__(timeout=120)
        self.bot, self.info, self.actor_id, self.reason = bot, info, actor_id, reason
        self._opened = interaction
        registry = info["registry"] or {}
        if registry.get("channel_id") and registry.get("message_id"):
            where = f"will be removed from <#{registry['channel_id']}>"
        else:
            where = "none stored — only the database is corrected"
        kind = "Sales Statement" if info["kind"] == "sale" else "Revenue Statement"
        lines = [
            ui._field("Transaction ID", f"`{info['txn_id']}`"),
            ui._field("Type", kind),
            ui._field("Company", config.company_name(info["company"])),
            ui._field("Date", store.pretty_date(info["date"])),
            ui._field("Amount", store.money(info["amount"])),
            ui._field("From", ui._val(info["party"])),
            ui._field("Service / Category", ui._val(info["detail"])),
            ui._field("Recorded By", ui._val(info["recorded_by"])),
            ui._field("Discord Message", where),
            ui._field("Reason", reason),
        ]
        if info["already_deleted"]:
            lines.append("-# Already voided in the books — confirming only retries the Discord message.")
        else:
            lines.append("-# The record is kept (voided) for audit; it leaves every total, report and the P&L.")
        self.confirm = discord.ui.Button(label="Delete transaction", style=discord.ButtonStyle.danger, emoji="🗑️")
        self.cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        self.confirm.callback = self._confirm
        self.cancel.callback = self._cancel
        self.add_item(discord.ui.Container(*ui.card_items(
            "Delete this transaction?", "\n".join(lines),
            rows=[discord.ui.ActionRow(self.confirm, self.cancel)], level="###")))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.actor_id and is_staff(interaction.user):
            return True
        await interaction.response.send_message(
            view=ui.notice("Not allowed", "Only the staff member who ran the command can confirm it.",
                           error=True), ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        self.confirm.disabled = self.cancel.disabled = True
        try:
            await self._opened.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(view=ui.notice("Cancelled", "Nothing was changed."))

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.stop()  # one click only: a double click cannot start a second delete
        txn_id = self.info["txn_id"]
        await interaction.response.edit_message(view=ui.notice("Deleting…", f"Working on `{txn_id}`."))
        result = await delete_transaction_record(self.bot, txn_id, self.actor_id, self.reason)
        if not result["ok"]:
            view = ui.notice("Transaction not deleted", result["error"], error=True)
        elif result["noop"]:
            view = ui.notice("Already deleted", f"`{txn_id}` was already deleted; nothing to do.")
        else:
            info = self.info
            if info["kind"] == "sale":
                totals = ui._field("Total Sales now", store.money(store.total_sales(company=info["company"])))
            else:
                summary = await self.bot.db.revenue_summary(info["company"])
                totals = ui._field("Operating revenue now", store.money((summary.get("operating") or 0) / 100))
            warn = "⚠️ " if result["message_state"] == "failed" else ""
            retry = ("\n-# The books are correct. Run `/delete_transaction` with the same ID to retry the message."
                     if result["message_state"] == "failed" else "")
            body = "\n".join([
                ui._field("Transaction ID", f"`{txn_id}`"),
                ui._field("Company", config.company_name(info["company"])),
                ui._field("Amount", store.money(info["amount"])),
                ui._field("Reason", self.reason),
                ui._field("Deleted By", interaction.user.mention),
                totals,
                f"{warn}{result['message_text']}",
            ]) + retry
            view = ui.notice(f"{txn_id} deleted", body, error=bool(warn))
        await interaction.edit_original_response(view=view)


class Statements(commands.Cog):
    """Sales, Expense and Revenue Statements (one company at a time)."""

    sale = app_commands.Group(name="sale", description="Sales (payments received)", guild_only=True)
    expense = app_commands.Group(name="expense", description="Professional expenses", guild_only=True)
    statement = app_commands.Group(
        name="statement",
        description="Period statements posted to the company's statements channel",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Sales/revenue recorded before transaction ids existed get their permanent id + registry row.
        try:
            assigned = store.ensure_sale_ids()
            added = await self.bot.db.backfill_statement_txns(store.all_payments())
            if assigned or added:
                log.info("Transaction ids: %s sale(s) numbered, %s registry row(s) added", assigned, added)
        except Exception:
            log.exception("Could not back-fill transaction ids")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Finance data is staff-only (administrators + STAFF_ROLE_IDS).
        return await staff_interaction_check(interaction)

    # -- Sales Statement (one sale) ----------------------------------------
    @sale.command(name="add", description="Record a sale and post its Sales Statement to the company channel")
    @app_commands.describe(
        company="Company this sale belongs to",
        client_name="Client name",
        amount="Amount received (₹)",
        service="Service / plan sold",
        method="How it was paid",
        client_contact="Client contact (Discord, phone, email)",
        server_name="Server name",
        server_uuid="Server UUID",
        transaction_id="Transaction ID / UTR",
        remarks="Anything worth noting",
        date="Sale date (YYYY-MM-DD), defaults to today",
    )
    @app_commands.choices(company=_COMPANY_CHOICES, method=_METHOD_CHOICES)
    async def add_sale(
        self,
        interaction: discord.Interaction,
        company: app_commands.Choice[str],
        client_name: str,
        amount: float,
        service: str,
        method: str = "upi",
        client_contact: str = "",
        server_name: str = "",
        server_uuid: str = "",
        transaction_id: str = "",
        remarks: str = "",
        date: str = "",
    ) -> None:
        if amount <= 0:
            await _bad(interaction, "Invalid amount", "The amount must be greater than zero.")
            return
        if date and not _DATE.match(date.strip()):
            await _bad(interaction, "Invalid date", f"`{date}` should look like `2026-10-26`.")
            return
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        if duplicate_submission((interaction.user.id, "sale", selected.key, round(amount, 2),
                                 client_name.strip().lower(), date.strip())):
            await _bad(interaction, "Already recorded",
                       "That sale was just submitted. Check the statement channel before adding it again.")
            return

        record = store.add_payment(
            client_name=client_name,
            client_contact=client_contact,
            server_name=server_name,
            server_uuid=server_uuid,
            service=service,
            amount=amount,
            method=method,
            transaction_id=transaction_id,
            remarks=remarks,
            date=date,
            recorded_by=interaction.user.display_name,
            company=selected.key,
        )
        await mirror_sale(interaction.client, record, interaction.user.id)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.sale_card(record, store.total_sales(company=selected.key)),
            saved=True,
            done_title="Sale recorded",
            track=statement_tracker(interaction.client, record.get("sale_id"), "sale",
                                    selected.key, record.get("id")),
        )

    # -- Expense Statement (one expense) -----------------------------------
    @expense.command(name="add", description="Record an expense and post its Expense Statement to the company channel")
    @app_commands.describe(
        company="Company this expense belongs to",
        description="What the expense was for",
        amount="Amount spent (₹)",
        category="Expense category",
        method="How it was paid",
        paid_to="Who was paid",
        transaction_id="Transaction ID / UTR",
        remarks="Anything worth noting",
        date="Expense date (YYYY-MM-DD), defaults to today",
    )
    @app_commands.choices(company=_COMPANY_CHOICES, category=_CATEGORY_CHOICES, method=_METHOD_CHOICES)
    async def add_expense(
        self,
        interaction: discord.Interaction,
        company: app_commands.Choice[str],
        description: str,
        amount: float,
        category: str = "General",
        method: str = "upi",
        paid_to: str = "",
        transaction_id: str = "",
        remarks: str = "",
        date: str = "",
    ) -> None:
        if amount <= 0:
            await _bad(interaction, "Invalid amount", "The amount must be greater than zero.")
            return
        if date and not _DATE.match(date.strip()):
            await _bad(interaction, "Invalid date", f"`{date}` should look like `2026-10-26`.")
            return
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        if duplicate_submission((interaction.user.id, "expense", selected.key, round(amount, 2),
                                 description.strip().lower(), date.strip())):
            await _bad(interaction, "Already recorded",
                       "That expense was just submitted. Check the statement channel before adding it again.")
            return

        record = store.add_expense(
            description=description,
            category=category,
            amount=amount,
            method=method,
            paid_to=paid_to,
            transaction_id=transaction_id,
            remarks=remarks,
            date=date,
            recorded_by=interaction.user.display_name,
            company=selected.key,
        )
        await mirror_expense(interaction.client, record, interaction.user.id)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.expense_card(record, store.total_expenses(company=selected.key)),
            saved=True,
            done_title="Expense recorded",
        )

    # -- Revenue Statement (one revenue entry) -----------------------------
    @app_commands.command(
        name="revenue",
        description="Record revenue (Commission / Investment / General) and post its Revenue Statement",
    )
    @app_commands.describe(
        company="Company this revenue belongs to",
        category="Revenue category",
        amount="Amount received (₹)",
        description="What the revenue was for",
        received_from="Payment sender / received from (client, partner, investor)",
        method="How it was received",
        transaction_id="Payment ID / transaction reference",
        remarks="Anything worth noting",
        date="Revenue date (YYYY-MM-DD), defaults to today",
    )
    @app_commands.choices(company=_COMPANY_CHOICES, category=_REVENUE_CHOICES, method=_METHOD_CHOICES)
    async def add_revenue(
        self,
        interaction: discord.Interaction,
        company: app_commands.Choice[str],
        category: app_commands.Choice[str],
        amount: app_commands.Range[float, 0.01, 100_000_000.0],
        description: str,
        received_from: str,
        method: str = "upi",
        transaction_id: str = "",
        remarks: str = "",
        date: str = "",
    ) -> None:
        if not received_from.strip():
            await _bad(interaction, "Missing sender", "Say who the money was received from.")
            return
        if date and not _DATE.match(date.strip()):
            await _bad(interaction, "Invalid date", f"`{date}` should look like `2026-10-26`.")
            return
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        when = date.strip() or store._today()
        if duplicate_submission((interaction.user.id, "revenue", selected.key, category.value,
                                 round(amount, 2), description.strip().lower(),
                                 received_from.strip().lower(), when)):
            await _bad(interaction, "Already recorded",
                       "That revenue entry was just submitted. Check the statement channel before "
                       "adding it again.")
            return

        try:
            revenue_id = await self.bot.db.add_revenue(
                interaction.user.id, selected.key, category.value, to_cents(amount), description, when,
                received_from=received_from, method=method, transaction_id=transaction_id,
                remarks=remarks, recorded_by=interaction.user.display_name,
            )
        except ValueError as exc:
            await _bad(interaction, "Revenue not recorded", str(exc))
            return

        record = await self.bot.db.get_revenue(revenue_id) or {}
        record["number"] = revenue_number(revenue_id)
        record["recorded_by"] = record.get("recorded_by") or interaction.user.display_name
        totals = await self.bot.db.revenue_summary(selected.key)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.revenue_card(record, totals),
            saved=True,
            done_title="Revenue recorded",
            track=statement_tracker(self.bot, record["number"], "revenue", selected.key, revenue_id),
        )

    # -- voiding a wrong statement ------------------------------------------
    @app_commands.command(
        name="revenue-void",
        description="Void a wrong Revenue Statement so it stops counting in every total",
    )
    @app_commands.describe(
        entry="Revenue Statement id (REV-0003 or 3)",
        reason="Why it is being voided",
    )
    async def revenue_void(self, interaction: discord.Interaction, entry: str, reason: str) -> None:
        match = _REVENUE_ID.match(entry or "")
        if match is None:
            await _bad(interaction, "Invalid id",
                       "Use the id on the statement, e.g. `REV-0003` or `3`.")
            return
        if not reason.strip():
            await _bad(interaction, "Missing reason", "Say why this entry is being voided.")
            return
        revenue_id = int(match.group(1))
        record = await self.bot.db.get_revenue(revenue_id)
        if record is None:
            await _bad(interaction, "Not found", f"There is no Revenue Statement `{revenue_number(revenue_id)}`.")
            return
        if record.get("voided"):
            await _bad(interaction, "Already void", f"`{revenue_number(revenue_id)}` is already void.")
            return
        try:
            voided = await self.bot.db.void_revenue(interaction.user.id, revenue_id, reason)
        except ValueError as exc:
            await _bad(interaction, "Revenue not voided", str(exc))
            return

        company = config.get_company(voided.get("company_id"))
        note = "-# No company was attached, so nothing was posted."
        if company is not None:
            body = "\n".join([
                ui._field("Transaction ID", revenue_number(revenue_id)),
                ui._field("Company", company.name),
                ui._field("Category", voided.get("category") or "—"),
                ui._field("Amount", store.money((voided.get("amount") or 0) / 100)),
                ui._field("Reason", reason),
                ui._field("Voided By", interaction.user.mention),
            ])
            try:
                ok, message = await post_company(
                    interaction.client, company.key,
                    ui.notice("Revenue Statement voided", body))
            except Exception:
                log.exception("Could not post the revenue void notice")
                ok, message = False, "an unexpected error (it was logged)"
            note = (f"-# Void notice posted to {message}." if ok
                    else f"-# ⚠️ Void notice not posted: {message}")

        totals = await self.bot.db.revenue_summary(company.key if company else None)
        await ui.say(
            interaction,
            f"⚫ {revenue_number(revenue_id)} voided\n"
            f"{ui._field('Category', voided.get('category') or '—')}\n"
            f"{ui._field('Amount', store.money((voided.get('amount') or 0) / 100))}\n"
            f"{ui._field('Reason', reason)}\n"
            f"{ui._field('Operating revenue now', store.money((totals.get('operating') or 0) / 100))}\n"
            f"-# Removed from totals, reports and the P&L (the record is kept for audit).\n{note}",
        )

    @app_commands.command(name="revenue-list", description="Recent Revenue Statements with the id to void")
    @app_commands.describe(company="Only this company", include_voided="Also list voided entries")
    @app_commands.choices(company=_COMPANY_CHOICES)
    async def revenue_list(
        self,
        interaction: discord.Interaction,
        company: Optional[app_commands.Choice[str]] = None,
        include_voided: bool = False,
    ) -> None:
        rows = await self.bot.db.recent_revenue(
            company.value if company else None, limit=20, include_voided=include_voided)
        lines = [
            f"{'⚫' if row.get('voided') else '🟢'} `{revenue_number(row['id'])}` · "
            f"{row['occurred_on']} · {config.company_name(row.get('company_id'))} · "
            f"{row.get('category') or '—'} · **{store.money((row.get('amount') or 0) / 100)}** · "
            f"{row.get('received_from') or '—'}"
            for row in rows
        ]
        title = "Revenue Statements" + (f" · {company.name}" if company else "")
        await interaction.response.send_message(
            view=ui.build_card(title, "\n".join(lines) or "No Revenue Statements yet.", timeout=180),
            ephemeral=True,
        )

    # -- voiding a wrong expense statement ----------------------------------
    @app_commands.command(
        name="expense-void",
        description="Void a wrong Expense Statement so it stops counting in every total",
    )
    @app_commands.describe(
        entry="Expense Statement id (EXP-0003 or 3)",
        reason="Why it is being voided",
    )
    async def expense_void(self, interaction: discord.Interaction, entry: str, reason: str) -> None:
        match = _EXPENSE_ID.match(entry or "")
        if match is None:
            await _bad(interaction, "Invalid id",
                       "Use the id from `/expense-list`, e.g. `EXP-0003` or `3`.")
            return
        if not reason.strip():
            await _bad(interaction, "Missing reason", "Say why this entry is being voided.")
            return
        entry_id = int(match.group(1))
        record = store.entry("expense", entry_id)
        if record is None:
            await _bad(interaction, "Not found",
                       f"There is no Expense Statement `{expense_number(entry_id)}`.")
            return
        if record.get("voided"):
            await _bad(interaction, "Already void",
                       f"`{expense_number(entry_id)}` is already void.")
            return
        try:
            voided = store.void_entry("expense", entry_id, reason)
        except ValueError as exc:
            await _bad(interaction, "Expense not voided", str(exc))
            return

        # The same expense is mirrored into the finance books; void that side too so the
        # dashboard, the reports and the P&L stop counting it.
        ref = str(voided.get("ref") or "")
        note = "-# It was not linked to a finance entry."
        if ref.startswith("tx:"):
            try:
                await self.bot.db.void_transaction(interaction.user.id, int(ref[3:]), reason)
                note = f"-# Finance entry #{ref[3:]} was voided too."
            except ValueError as exc:
                note = (f"-# Finance entry #{ref[3:]} was already void."
                        if "already void" in str(exc).lower()
                        else f"-# ⚠️ Finance entry #{ref[3:]} was not voided: {exc}")
            except Exception:
                log.exception("Could not void the finance entry linked to expense %s", entry_id)
                note = f"-# ⚠️ Finance entry #{ref[3:]} could not be voided (it was logged)."

        company = config.get_company(voided.get("company"))
        posted = "-# No company was attached, so nothing was posted."
        if company is not None:
            body = "\n".join([
                ui._field("Statement ID", expense_number(entry_id)),
                ui._field("Company", company.name),
                ui._field("Expense", voided.get("description") or voided.get("category") or "—"),
                ui._field("Amount", store.money(voided.get("amount") or 0)),
                ui._field("Reason", reason),
                ui._field("Voided By", interaction.user.mention),
            ])
            try:
                ok, message = await post_company(
                    interaction.client, company.key,
                    ui.notice("Expense Statement voided", body))
            except Exception:
                log.exception("Could not post the expense void notice")
                ok, message = False, "an unexpected error (it was logged)"
            posted = (f"-# Void notice posted to {message}." if ok
                      else f"-# ⚠️ Void notice not posted: {message}")

        total = store.total_expenses(company=voided.get("company") or None)
        await ui.say(
            interaction,
            f"⚫ {expense_number(entry_id)} voided\n"
            f"{ui._field('Expense', voided.get('description') or voided.get('category') or '—')}\n"
            f"{ui._field('Amount', store.money(voided.get('amount') or 0))}\n"
            f"{ui._field('Reason', reason)}\n"
            f"{ui._field('Expenses now', store.money(total))}\n"
            f"-# Removed from totals, reports and the P&L (the record is kept for audit).\n"
            f"{note}\n{posted}",
        )

    @app_commands.command(name="expense-list", description="Recent Expense Statements with the id to void")
    @app_commands.describe(company="Only this company", include_voided="Also list voided entries")
    @app_commands.choices(company=_COMPANY_CHOICES)
    async def expense_list(
        self,
        interaction: discord.Interaction,
        company: Optional[app_commands.Choice[str]] = None,
        include_voided: bool = False,
    ) -> None:
        rows = store.list_entries(
            "expense", company.value if company else None,
            include_voided=include_voided, limit=20)
        lines = [
            f"{'⚫' if row.get('voided') else '🟢'} `{expense_number(row['id'])}` · "
            f"{store._entry_date(row)} · {config.company_name(row.get('company'))} · "
            f"{row.get('category') or '—'} · **{store.money(row.get('amount') or 0)}** · "
            f"{row.get('description') or '—'}"
            for row in rows
        ]
        title = "Expense Statements" + (f" · {company.name}" if company else "")
        await interaction.response.send_message(
            view=ui.build_card(title, "\n".join(lines) or "No Expense Statements yet.", timeout=180),
            ephemeral=True,
        )

    # -- staff: delete a Sales / Revenue statement by its transaction id -----
    @app_commands.command(
        name="delete_transaction",
        description="Staff: delete a Sales (SALE-…) or Revenue (REV-…) statement by its transaction ID",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        transaction_id="Transaction ID on the statement, e.g. SALE-000012 or REV-000003",
        reason="Why it is being deleted (kept in the audit log)",
    )
    async def delete_transaction(
        self, interaction: discord.Interaction, transaction_id: str,
        reason: app_commands.Range[str, 3, 200],
    ) -> None:
        txn_id = parse_txn_id(transaction_id)
        if txn_id is None:
            await _bad(interaction, "Invalid transaction ID",
                       "Use the full ID from the statement, e.g. `SALE-000012` or `REV-000003`.")
            return
        try:
            info = await inspect_transaction(self.bot, txn_id)
        except Exception:
            log.exception("delete_transaction lookup failed for %s", txn_id)
            await _bad(interaction, "Database error", "I could not read the books. Nothing was changed.")
            return
        if info is None:
            await _bad(interaction, "Not found", f"There is no transaction `{txn_id}`.")
            return
        if info["blocked"]:
            await _bad(interaction, "Cannot delete this transaction", info["blocked"])
            return
        registry = info["registry"] or {}
        if (info["already_deleted"] and registry.get("deleted_at")
                and registry.get("message_state") in ("removed", "missing", "untracked")):
            await interaction.response.send_message(
                view=ui.notice("Already deleted", f"`{txn_id}` was deleted earlier "
                               f"by <@{registry.get('deleted_by')}>: {registry.get('delete_reason') or '—'}"),
                ephemeral=True)
            return
        view = _DeleteConfirm(self.bot, info, interaction.user.id, reason.strip(), interaction)
        await interaction.response.send_message(view=view, ephemeral=True)


    # -- period statements --------------------------------------------------
    @statement.command(name="sales", description="Post a Sales Statement for a period")
    @app_commands.describe(company="Company to report on", period="Period to cover")
    @app_commands.choices(company=_COMPANY_CHOICES, period=_PERIOD_CHOICES)
    async def sales_statement(self, interaction: discord.Interaction,
                              company: app_commands.Choice[str], period: str = "month") -> None:
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        start = store.period_start(period)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.sales_statement_card(store.payments(start, company=selected.key), period,
                                    store.total_sales(start, company=selected.key), company=selected.name),
            saved=False,
            done_title="Sales Statement",
        )

    @statement.command(name="expenses", description="Post an Expense Statement for a period")
    @app_commands.describe(company="Company to report on", period="Period to cover")
    @app_commands.choices(company=_COMPANY_CHOICES, period=_PERIOD_CHOICES)
    async def expenses_statement(self, interaction: discord.Interaction,
                                 company: app_commands.Choice[str], period: str = "month") -> None:
        selected = config.get_company(company.value)
        if selected is None:
            await _bad(interaction, "Unknown company", "Pick one of the configured companies.")
            return
        channel = await _statement_channel(interaction, selected)
        if channel is None:
            return
        start = store.period_start(period)
        await _send_statement(
            interaction,
            channel,
            selected,
            ui.expenses_statement_card(store.expenses(start, company=selected.key), period,
                                       store.total_expenses(start, company=selected.key),
                                       company=selected.name),
            saved=False,
            done_title="Expense Statement",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Statements(bot))
