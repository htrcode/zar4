import asyncio
import logging
import io
from typing import Optional

import discord
import ui
from discord import app_commands
from discord.ext import commands

import config
import data as store
from desk.bridge import record_finance
from desk.pdf_gen import pnl_pdf
from desk.utils import (company_choices, fmt, parse_date, require_company, staff_interaction_check,
                        to_cents, valid_month)
from desk.views import DashboardView, simple_card

log = logging.getLogger("invoice-bot.finance")

KIND_CHOICES = [app_commands.Choice(name="Income", value="income"),
                app_commands.Choice(name="Expense", value="expense")]


@app_commands.guild_only()
class Finance(commands.GroupCog, group_name="finance", group_description="Income, expenses and reports"):
    def __init__(self, bot):
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    async def _record(self, interaction, kind, amount, category, description, date, company_key):
        try:
            company = require_company(company_key)
            cents = to_cents(amount)
            when = parse_date(date)
            tx_id = await self.bot.db.add_transaction(
                interaction.user.id, kind, category, cents, description, when, company_id=company.key)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        # Keep Sales / Expense Statements in step with the books — in this company's channel only.
        statement = "Sales Statement" if kind == "income" else "Expense Statement"
        try:
            posted, message = await record_finance(
                self.bot, kind, category, cents, description, when,
                interaction.user.display_name, company_id=company.key, txn_id=tx_id)
        except Exception:
            log.exception("Could not post the %s statement", kind)
            posted, message = False, "an unexpected error (it was logged)"
        icon = "➕" if kind == "income" else "➖"
        note = (f"-# {statement} posted to {message}." if posted
                else f"-# ⚠️ Saved, but the {statement} was not posted: {message}")
        await ui.say(interaction, f"{icon} {kind.title()} recorded\n"
                     f"{ui._field('ID', f'#{tx_id}')}\n{ui._field('Company', company.name)}\n"
                     f"{ui._field('Category', category)}\n"
                     f"{ui._field('Amount', fmt(cents))}\n{ui._field('Details', description or '—')}\n"
                     f"{note}")

    async def _ac(self, kind, current):
        cats = await self.bot.db.categories(kind)
        return [app_commands.Choice(name=c["name"], value=c["name"])
                for c in cats if current.lower() in c["name"].lower()][:25]

    # ---------- record ----------
    @app_commands.command(name="income", description="Record income (hosting revenue, other sales...)")
    @app_commands.describe(company="Company this income belongs to",
                           date="YYYY-MM-DD, defaults to today (UTC)")
    @app_commands.choices(company=company_choices())
    async def income(self, interaction: discord.Interaction,
                     company: app_commands.Choice[str],
                     amount: app_commands.Range[float, 0.01, 100_000_000.0],
                     category: str, description: str = "", date: Optional[str] = None):
        await self._record(interaction, "income", amount, category, description, date, company.value)

    @income.autocomplete("category")
    async def _income_ac(self, interaction, current: str):
        return await self._ac("income", current)

    @app_commands.command(name="expense", description="Record an expense (servers, software, marketing, commissions...)")
    @app_commands.describe(company="Company this expense belongs to",
                           date="YYYY-MM-DD, defaults to today (UTC)")
    @app_commands.choices(company=company_choices())
    async def expense(self, interaction: discord.Interaction,
                      company: app_commands.Choice[str],
                      amount: app_commands.Range[float, 0.01, 100_000_000.0],
                      category: str, description: str = "", date: Optional[str] = None):
        await self._record(interaction, "expense", amount, category, description, date, company.value)

    @expense.autocomplete("category")
    async def _expense_ac(self, interaction, current: str):
        return await self._ac("expense", current)

    @app_commands.command(name="category-add", description="Add a new income or expense category")
    @app_commands.choices(kind=KIND_CHOICES)
    async def category_add(self, interaction: discord.Interaction, kind: app_commands.Choice[str], name: str):
        try:
            await self.bot.db.add_category(interaction.user.id, name, kind.value)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"✅ Added {kind.value} category **{name}**.")

    # ---------- history ----------
    @app_commands.command(name="transactions", description="Transaction history for a month (with the ids void takes)")
    @app_commands.describe(company="Only show this company's transactions (default: all companies)")
    @app_commands.choices(kind=KIND_CHOICES, company=company_choices())
    async def transactions(self, interaction: discord.Interaction, month: Optional[str] = None,
                           kind: Optional[app_commands.Choice[str]] = None,
                           company: Optional[app_commands.Choice[str]] = None):
        try:
            month = valid_month(month)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        rows = await self.bot.db.transactions(month, kind.value if kind else None, limit=20,
                                              company_id=company.value if company else None)
        lines = [f"`#{t['id']}` {t['occurred_on']} · {'➕' if t['kind'] == 'income' else '➖'} "
                 f"{config.company_name(t.get('company_id'))} · {t['category']} · "
                 f"**{fmt(t['amount'])}** · {(t['description'] or '')[:40]}" for t in rows]
        title = f"Transactions · {month}" + (f" · {company.name}" if company else "")
        await interaction.response.send_message(
            view=simple_card(title, "\n".join(lines) or "No transactions."), ephemeral=True)

    @app_commands.command(name="void", description="Void a wrong income/expense entry so it leaves every total")
    @app_commands.describe(transaction_id="Transaction id (see /finance transactions)",
                           reason="Why it is being voided")
    async def void(self, interaction: discord.Interaction, transaction_id: int, reason: str):
        if not reason.strip():
            return await ui.say(interaction, "❌ Say why this entry is being voided.")
        try:
            row = await self.bot.db.void_transaction(interaction.user.id, transaction_id, reason)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        # The statement ledger holds the same entry; cancel it too so the Sales /
        # Expense statements and their running totals stop counting it.
        note = " It was not linked to a statement entry."
        try:
            kind, _entry = store.void_by_ref(f"tx:{transaction_id}", reason)
        except Exception:
            log.exception("Could not void the linked statement entry")
            kind = None
            note = " ⚠️ Its statement entry could not be voided (it was logged)."
        if kind:
            note = " Its statement entry was voided too."
        label = "Income" if (row or {}).get("kind") == "income" else "Expense"
        await ui.say(interaction, f"⚫ {label} #{transaction_id} voided\n"
                     f"{ui._field('Amount', fmt((row or {}).get('amount') or 0))}\n"
                     f"{ui._field('Company', config.company_name((row or {}).get('company_id')))}\n"
                     f"{ui._field('Reason', reason)}\n"
                     f"-# Removed from totals, reports and the P&L.{note}")

    @app_commands.command(name="audit", description="Recent audit log entries")
    async def audit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 15):
        rows = await self.bot.db.recent_audit(limit)
        lines = [f"`#{r['id']}` {r['ts'][:16]} · <@{r['actor_id']}> · **{r['action']}** "
                 f"{r['entity']} #{r['entity_id']} · `{(r['details'] or '')[:70]}`" for r in rows]
        await interaction.response.send_message(
            view=simple_card("Audit log", "\n".join(lines) or "Empty."), ephemeral=True)

    # ---------- reports ----------
    @app_commands.command(name="dashboard", description="Monthly revenue, expenses and net profit")
    @app_commands.describe(company="Only report this company (default: all companies)")
    @app_commands.choices(company=company_choices())
    async def dashboard(self, interaction: discord.Interaction, month: Optional[str] = None,
                        company: Optional[app_commands.Choice[str]] = None):
        try:
            month = valid_month(month)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        company_id = company.value if company else None
        report = await self.bot.db.month_report(month, company_id)
        await interaction.response.send_message(
            view=DashboardView(self.bot, report, await self.bot.db.outstanding(company_id)), ephemeral=True)

    @app_commands.command(name="pnl", description="Profit and Loss statement (PDF)")
    @app_commands.describe(company="Only report this company (default: all companies)")
    @app_commands.choices(company=company_choices())
    async def pnl(self, interaction: discord.Interaction, month: Optional[str] = None,
                  company: Optional[app_commands.Choice[str]] = None):
        try:
            month = valid_month(month)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.bot.db.month_report(month, company.value if company else None)
        data = await asyncio.to_thread(pnl_pdf, report)
        filename = f"PnL-{month}.pdf"
        card = ui.file_card(f"Profit & Loss · {month}", ui._field("Net", fmt(report["net"])), filename)
        await interaction.followup.send(view=card, file=discord.File(io.BytesIO(data), filename=filename),
                                        ephemeral=True)


async def setup(bot):
    await bot.add_cog(Finance(bot))
