"""Quick invoices (client, contact, amount, service/plan, due date).

``/invoice create`` opens :class:`QuickInvoiceModal` and requires a company, so
the invoice is stored against that company and posted only to its channel. The
command itself lives in ``desk/invoices.py`` next to the itemised-invoice
commands because Discord allows only one ``/invoice`` group.
"""

from __future__ import annotations

import discord
from discord.ext import commands

import config
import data as store
import ui
from desk.utils import duplicate_submission


class QuickInvoiceModal(discord.ui.Modal):
    customer = discord.ui.TextInput(label="Client name", placeholder="Acme Ltd.", max_length=100)
    contact = discord.ui.TextInput(
        label="Client contact", placeholder="Discord / phone / email", required=False, max_length=100
    )
    amount = discord.ui.TextInput(label="Amount (₹)", placeholder="1250.00", max_length=16)
    service = discord.ui.TextInput(
        label="Service/Plan", placeholder="Minecraft Hosting - Plan 4GB", max_length=200
    )
    due = discord.ui.TextInput(
        label="Due date (YYYY-MM-DD)", placeholder="2026-11-30", required=False, max_length=32
    )

    def __init__(self, company: str = "") -> None:
        super().__init__()
        self.company = company
        if config.get_company(company):
            self.title = f"New invoice · {config.company_name(company)}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        company = config.get_company(self.company)
        if company is None:
            await interaction.response.send_message(
                view=ui.notice(
                    "No company selected",
                    "Use `/invoice create company:...` so the invoice is stored under the right company. "
                    "Nothing is saved until a company is chosen.",
                    error=True,
                ),
                ephemeral=True,
            )
            return
        raw_amount = str(self.amount.value).replace(",", "").replace("₹", "").strip()
        try:
            amount = round(float(raw_amount), 2)
        except ValueError:
            await interaction.response.send_message(
                view=ui.notice("Invalid amount", f"`{self.amount.value}` is not a number.", error=True),
                ephemeral=True,
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                view=ui.notice("Invalid amount", "The amount must be greater than zero.", error=True),
                ephemeral=True,
            )
            return
        if duplicate_submission((interaction.user.id, "quick-invoice", company.key, amount,
                                 str(self.customer.value).strip().lower())):
            await interaction.response.send_message(
                view=ui.notice(
                    "Already submitted",
                    "That invoice was just created. Check the company's invoice channel before adding it again.",
                    error=True,
                ),
                ephemeral=True,
            )
            return

        # THE COMPANY'S INVOICE CHANNEL ONLY. Check it first so no invoice is
        # saved without being posted.
        channel = await ui.resolve_channel(interaction.client, company.channel_id("invoice"))
        if channel is None:
            await interaction.response.send_message(
                view=ui.missing_company_channel_notice(company, "invoice"), ephemeral=True
            )
            return

        inv = store.create_invoice(
            customer=str(self.customer.value),
            contact=str(self.contact.value or ""),
            amount=amount,
            due=str(self.due.value or ""),
            service=str(self.service.value or ""),
            created_by=interaction.user.display_name,
            company=company.key,
        )

        try:
            await channel.send(view=ui.invoice_card(inv))
        except discord.HTTPException:
            await interaction.response.send_message(
                view=ui.notice(
                    "Could not post invoice",
                    f"`{inv['number']}` was saved for **{company.name}**, but I could not send it to "
                    f"<#{channel.id}> — check my **Send Messages** permission there.",
                    error=True,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            view=ui.notice(
                "Invoice created",
                f"`{inv['number']}` for **{store.money(inv['amount'])}** ({inv['customer']}) "
                f"was posted to <#{channel.id}> as **{company.name}**.",
            ),
            ephemeral=True,
        )


# Backwards-compatible name.
InvoiceModal = QuickInvoiceModal


async def setup(bot: commands.Bot) -> None:  # nothing to register: the commands live in desk/invoices.py
    return None
