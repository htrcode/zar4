from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from config import LOG_CHANNEL_ID
import data as store
import ui
from cogs.invoices import QuickInvoiceModal
from desk.utils import (PRODUCT_TYPES, company_choices, duplicate_submission, dmy, fmt, is_ticket_staff,
                   normalize_product, parse_items, require_company, staff_interaction_check, to_cents,
                   valid_month)
from desk.views import (InvoiceView, STATUS_STYLE, _pdf_message, deliver_invoice,
                        send_invoice_to_channel, simple_card)

TicketChannel = Union[discord.TextChannel, discord.Thread]

BOTH_STATUS = [app_commands.Choice(name=n, value=n) for n in ("unpaid", "partial", "overdue", "paid", "void")]

# Shown in the item form; staff can type any configured company name.
COMPANY_HINT = " / ".join(company.name for company in config.COMPANIES.values())


class ItemsModal(discord.ui.Modal, title="Invoice items"):
    company = discord.ui.TextInput(
        label="Company",
        placeholder=COMPANY_HINT,
        max_length=60,
    )
    items = discord.ui.TextInput(
        label="Items (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="VPS 4GB | 1 | 700 | type=VPS; node=Ryzen; plan=4GB; link=https://panel...",
        max_length=1800,
    )
    discount = discord.ui.TextInput(label="Discount amount (0 for none)", required=False, max_length=15)
    tax = discord.ui.TextInput(label="Tax % (leave empty for no tax)", required=False, max_length=6)
    notes = discord.ui.TextInput(label="Notes (optional)", style=discord.TextStyle.paragraph,
                                 required=False, max_length=500)

    def __init__(self, cog, opts: dict, company: str = ""):
        super().__init__()
        self.cog, self.opts = cog, opts
        if company:
            self.company.default = company
        self.discount.default = f"{opts['discount_cents'] / 100:g}"
        self.tax.default = f"{opts['tax_percent']:g}" if opts["tax_percent"] else ""

    async def on_submit(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Ticket staff only.")
        bot = self.cog.bot
        try:
            company = require_company(self.company.value)
            items = parse_items(self.items.value)
            opts = dict(self.opts)
            opts["company_id"] = company.key
            opts["discount_cents"] = to_cents(self.discount.value or "0")
            try:
                opts["tax_percent"] = float((self.tax.value or "0").replace("%", "").strip())
            except ValueError:
                raise ValueError("Tax % must be a number, like `18`.")
            if opts["discount_cents"] < 0:
                raise ValueError("Discount cannot be negative.")
            if not 0 <= opts["tax_percent"] <= 100:
                raise ValueError("Tax % must be between 0 and 100.")
            if duplicate_submission((interaction.user.id, "itemised-invoice", company.key,
                                     self.items.value.strip().lower()), window=15):
                raise ValueError("That invoice form was just submitted. Check the invoice channel before "
                                 "creating it again.")
            default_product = opts.pop("product", None)
            if default_product:
                for item in items:
                    if not item["product_type"]:
                        item["product_type"] = normalize_product(default_product)
            invoice_id = await bot.db.create_invoice(
                interaction.user.id, items=items, notes=self.notes.value, **opts)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        inv = await bot.db.get_invoice(invoice_id)
        await interaction.response.send_message(view=InvoiceView(bot, invoice_id, inv), ephemeral=True)
        ticket_id = self.opts["ticket_channel_id"]
        if ticket_id:
            try:
                channel = bot.get_channel(ticket_id) or await bot.fetch_channel(ticket_id)
                await send_invoice_to_channel(bot, channel, inv)
                await ui.say(interaction, f"📨 Also posted in {channel.mention}.")
            except discord.HTTPException:
                await ui.say(interaction, "⚠️ Created, but I couldn't post in that ticket channel.")


@app_commands.guild_only()
class Invoices(commands.GroupCog, group_name="invoice", group_description="Create and manage invoices"):
    def __init__(self, bot):
        self.bot = bot
        self.bot.invoices = self  # ticket/deal panels call back into this cog
        super().__init__()
        self.overdue_check.start()

    async def cog_unload(self):
        self.overdue_check.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    # ---------- reusable invoice drafts (called by ticket and sales panels) ----------
    def _draft_opts(self, *, client_id, client_name, ticket_channel_id, product=None, company=None) -> dict:
        return dict(client_discord_id=client_id, client_name=(client_name or "Client")[:100],
                    client_contact="", due_days=7, tax_percent=0.0, discount_cents=0,
                    category="Hosting Revenue", ticket_channel_id=ticket_channel_id,
                    company_id=company or None, product=product)

    async def open_draft_for_ticket(self, interaction: discord.Interaction, ticket: dict):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Ticket staff only.")
        opts = self._draft_opts(client_id=ticket.get("client_discord_id"),
                                client_name=ticket.get("client_name") or "Client",
                                ticket_channel_id=ticket.get("channel_id"),
                                company=ticket.get("company_id"))
        # A ticket's company is preselected; staff can change it in the form before saving.
        await interaction.response.send_modal(
            ItemsModal(self, opts, company=config.company_name(ticket.get("company_id"))
                       if config.get_company(ticket.get("company_id")) else ""))

    async def open_draft_for_deal(self, interaction: discord.Interaction, deal: dict):
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 Sales staff only.")
        company = deal.get("company_id")
        if config.get_company(company) is None and deal.get("ticket_id"):
            ticket = await self.bot.db.get_ticket(deal["ticket_id"])
            company = (ticket or {}).get("company_id")
        opts = self._draft_opts(client_id=deal.get("client_discord_id"),
                                client_name=deal.get("client_name") or "Client",
                                ticket_channel_id=deal.get("ticket_channel_id"),
                                company=company,
                                product=deal.get("service"))
        await interaction.response.send_modal(
            ItemsModal(self, opts, company=config.company_name(company)
                       if config.get_company(company) else ""))

    # ---------- create ----------
    @app_commands.command(name="create",
                          description="Create an invoice quickly (client, amount, service, due date)")
    @app_commands.describe(company="Company this invoice belongs to")
    @app_commands.choices(company=company_choices())
    async def create(self, interaction: discord.Interaction, company: app_commands.Choice[str]):
        await interaction.response.send_modal(QuickInvoiceModal(company.value))

    @app_commands.command(name="create-detailed",
                          description="Create an itemised PDF invoice (items are entered in the next form)")
    @app_commands.describe(client_name="Name printed on the invoice", client="Discord user being billed",
                           due_days="Days until due", tax_percent="Tax rate in %", discount="Flat discount amount",
                           contact="Client phone/email shown on the invoice", category="Income category",
                           company="Company this invoice belongs to",
                           ticket="Ticket channel/thread to send it to",
                           product="Default digital product type for items without type=...")
    @app_commands.choices(product=[app_commands.Choice(name=p, value=p) for p in PRODUCT_TYPES],
                          company=company_choices())
    async def create_detailed(self, interaction: discord.Interaction, client_name: str,
                     company: app_commands.Choice[str],
                     client: Optional[discord.Member] = None,
                     due_days: app_commands.Range[int, 0, 365] = 14,
                     tax_percent: app_commands.Range[float, 0.0, 100.0] = 0.0,
                     discount: app_commands.Range[float, 0.0, 1_000_000.0] = 0.0,
                     category: str = "Hosting Revenue",
                     contact: Optional[str] = None,
                     ticket: Optional[TicketChannel] = None,
                     product: Optional[app_commands.Choice[str]] = None):
        try:
            await self.bot.db.category_id(category, "income")
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        opts = dict(client_discord_id=client.id if client else None, client_name=client_name[:100],
                    client_contact=(contact or "")[:80],
                    due_days=due_days, tax_percent=tax_percent, discount_cents=to_cents(discount),
                    category=category, ticket_channel_id=ticket.id if ticket else None,
                    company_id=company.value,
                    product=product.value if product else None)
        await interaction.response.send_modal(
            ItemsModal(self, opts, company=config.company_name(company.value)))

    @create_detailed.autocomplete("category")
    async def _cat_ac(self, interaction: discord.Interaction, current: str):
        cats = await self.bot.db.categories("income")
        return [app_commands.Choice(name=c["name"], value=c["name"])
                for c in cats if current.lower() in c["name"].lower()][:25]

    # ---------- read ----------
    @app_commands.command(name="view", description="Show an invoice")
    async def view(self, interaction: discord.Interaction, invoice_id: int):
        inv = await self.bot.db.get_invoice(invoice_id)
        if not inv:
            return await ui.say(interaction, "❌ Invoice not found.")
        await interaction.response.send_message(view=InvoiceView(self.bot, invoice_id, inv), ephemeral=True)

    @app_commands.command(name="pdf", description="Download an invoice as PDF")
    async def pdf(self, interaction: discord.Interaction, invoice_id: int):
        inv = await self.bot.db.get_invoice(invoice_id)
        if not inv:
            return await ui.say(interaction, "❌ Invoice not found.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _pdf_message(interaction.followup, inv, ephemeral=True)

    @app_commands.command(name="list", description="List invoices (quick and itemised; filter by status or month)")
    @app_commands.choices(status=BOTH_STATUS, company=company_choices())
    async def list_cmd(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None,
                       month: Optional[str] = None, company: Optional[app_commands.Choice[str]] = None):
        try:
            month = valid_month(month) if month else None
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        wanted = status.value if status else None
        company_id = company.value if company else None
        rows = await self.bot.db.list_invoices(wanted, month, limit=20, company_id=company_id)
        lines = []
        for i in rows:
            label = STATUS_STYLE[i["status"]][0].split(" ", 1)[0]
            extra = " · partial" if i["is_partial"] and i["status"] == "overdue" else ""
            delivered = " · 🔗 delivered" if i.get("delivered_at") else ""
            lines.append(f"{label} **{i['number']}** · {config.company_name(i.get('company_id'))} · "
                         f"{i['client_name'][:25]} · {fmt(i['total'], i['currency'])} "
                         f"(due {fmt(i['balance'], i['currency'])}) · due {dmy(i['due_date'])}{extra}{delivered}")
        quick = [q for q in store.invoices()
                 if (not wanted or wanted == q.get("status"))
                 and (not company_id or q.get("company") == company_id)
                 and (not month or str(q.get("created_at", ""))[:7] == month)]
        quick_lines = [ui.quick_invoice_line(q) for q in quick[-20:][::-1]]
        if not lines and not quick_lines:
            return await interaction.response.send_message(
                view=simple_card("Invoices", "No invoices found."), ephemeral=True)
        title = f"Invoices{' · ' + wanted if wanted else ''}{' · ' + month if month else ''}"
        blocks = []
        if quick_lines:
            blocks.append("**Quick invoices**\n" + "\n".join(quick_lines))
        if lines:
            blocks.append("**Itemised invoices**\n" + "\n".join(lines))
        await interaction.response.send_message(
            view=ui.build_card(title, *[b[:1900] for b in blocks], timeout=180), ephemeral=True)

    # ---------- payments / admin ----------
    @app_commands.command(name="pay", description="Record a (partial) payment on an invoice")
    async def pay(self, interaction: discord.Interaction, invoice_id: int, amount: float,
                  method: str = "manual", note: str = "", transaction_id: str = ""):
        try:
            await self.bot.db.add_payment(interaction.user.id, invoice_id, to_cents(amount), method, note,
                                          transaction_id)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        inv = await self.bot.db.get_invoice(invoice_id)
        await interaction.response.send_message(view=InvoiceView(self.bot, invoice_id, inv), ephemeral=True)

    @app_commands.command(name="void", description="Void an invoice (keeps history, removes it from reports)")
    async def void(self, interaction: discord.Interaction, invoice_id: int, reason: str):
        try:
            await self.bot.db.void_invoice(interaction.user.id, invoice_id, reason)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"⚫ Invoice #{invoice_id} voided.")

    @app_commands.command(name="send-ticket", description="Send an invoice (card + PDF) into a ticket channel")
    async def send_ticket(self, interaction: discord.Interaction, invoice_id: int, channel: TicketChannel):
        inv = await self.bot.db.get_invoice(invoice_id)
        if not inv:
            return await ui.say(interaction, "❌ Invoice not found.")
        await interaction.response.defer(ephemeral=True)
        try:
            await send_invoice_to_channel(self.bot, channel, inv)
        except discord.HTTPException:
            return await ui.say(interaction, "❌ I can't post there. Check my permissions.")
        await self.bot.db.set_ticket(interaction.user.id, invoice_id, channel.id)
        await ui.say(interaction, f"📨 Sent {inv['number']} to {channel.mention}.")

    @app_commands.command(name="deliver", description="Post digital delivery links (panel, bot file, VPS access)")
    async def deliver(self, interaction: discord.Interaction, invoice_id: int,
                      channel: Optional[TicketChannel] = None):
        inv = await self.bot.db.get_invoice(invoice_id)
        if not inv:
            return await ui.say(interaction, "❌ Invoice not found.")
        target = channel
        if target is None and inv.get("ticket_channel_id"):
            target = self.bot.get_channel(inv["ticket_channel_id"])
        if target is None:
            return await ui.say(interaction, "❌ No ticket linked. Pass a channel: `/invoice deliver invoice_id:... channel:#ticket`.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await deliver_invoice(self.bot, inv, target, interaction.user.id)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        except discord.HTTPException:
            return await ui.say(interaction, "❌ I can't post there. Check my permissions.")
        if channel is None and inv.get("ticket_channel_id"):
            await self.bot.db.set_ticket(interaction.user.id, invoice_id, target.id)
        await ui.say(interaction, f"🔗 Delivered {count} item(s) in {target.mention}.")

    # ---------- overdue watcher ----------
    @tasks.loop(hours=1)
    async def overdue_check(self):
        for inv in await self.bot.db.overdue_to_notify():
            body = "\n".join([ui._field("Client Name", inv["client_name"]),
                              ui._field("Balance", fmt(inv["balance"], inv["currency"])),
                              ui._field("Due Date", dmy(inv["due_date"]))])
            targets = [inv["ticket_channel_id"], LOG_CHANNEL_ID]
            for cid in dict.fromkeys(filter(None, targets)):
                try:
                    ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                    await ch.send(view=ui.build_card(f"🔴 Overdue · {inv['number']}", body, timeout=180))
                except discord.HTTPException:
                    pass
            await self.bot.db.mark_notified(inv["id"])

    @overdue_check.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Invoices(bot))
