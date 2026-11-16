"""Shared Components V2 building blocks and card builders.

Every message the bot sends is a ``discord.ui.LayoutView`` (Components V2):
one flat container (no accent strip) holding a title, a divider line, the
body text, an optional footer and an optional button row.

Card shape (same everywhere)::

    ## Title
    -# optional brand tagline
    ──────────────
    body text
    ──────────────
    -# footer
    [ buttons ]
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord

import config
import data as store

log = logging.getLogger("bot.ui")

# Discord allows ~4000 characters of text across one V2 message.
TEXT_BUDGET = 3200

# A short line shown directly under the title of every card. Loaded from
# config.BRAND_TAGLINE at startup (see bot.py). None/"" = don't show one.
BRAND_TAGLINE: Optional[str] = None


def set_tagline(text: Optional[str]) -> None:
    global BRAND_TAGLINE
    BRAND_TAGLINE = text or None


# --------------------------------------------------------------------------
# generic building blocks
# --------------------------------------------------------------------------
def heading(title: str, *, level: str = "##") -> str:
    """A heading line, with the brand tagline directly under it if set."""
    text = f"{level} {title}"
    if BRAND_TAGLINE:
        text += f"\n-# {BRAND_TAGLINE}"
    return text


def title_and_body(
    title: str, body: str = "", *, level: str = "##", divider: bool = True
) -> List[discord.ui.Item]:
    """Title block, thin divider line, then the body as its own block."""
    items: List[discord.ui.Item] = [discord.ui.TextDisplay(heading(title, level=level))]
    if divider:
        items.append(discord.ui.Separator())
    if body:
        items.append(discord.ui.TextDisplay(body))
    return items


def ensure_separator(items: List[discord.ui.Item]) -> None:
    """Append a Separator unless the list already ends with one."""
    if not items or not isinstance(items[-1], discord.ui.Separator):
        items.append(discord.ui.Separator())


def footer_items(items: List[discord.ui.Item], text: str) -> None:
    ensure_separator(items)
    items.append(discord.ui.TextDisplay(f"-# {text}"))


class PersistentView(discord.ui.LayoutView):
    """Base for views whose buttons live on a message for a long time.

    Expired interactions (Discord error 10062) are logged as one line instead
    of a full traceback; anything else is logged normally.
    """

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        if isinstance(error, discord.NotFound) and error.code == 10062:
            log.warning(
                "Interaction expired before %s could respond (custom_id=%s)",
                type(item).__name__,
                getattr(item, "custom_id", None),
            )
            return
        log.exception("Unhandled error in a view item", exc_info=error)


class StatusView(PersistentView):
    """Single-container status card for confirmations and errors."""

    def __init__(self, title: str, description: str = "", *, footer: Optional[str] = None) -> None:
        super().__init__(timeout=None)
        items = title_and_body(title, description, level="###", divider=bool(description))
        if footer:
            footer_items(items, footer)
        self.add_item(discord.ui.Container(*items))


def notice(title: str, body: str = "", *, error: bool = False) -> StatusView:
    return StatusView(f"{'❌' if error else '✅'} {title}", body)


def missing_channel_notice(which: str) -> StatusView:
    return notice(
        "Channel not configured",
        f"Set `{which}` in `config.py` to the id of the channel messages should go to, "
        "then restart the bot.",
        error=True,
    )


def missing_company_channel_notice(company: config.Company, kind: str = "statement") -> StatusView:
    """Per-company channel that is either unset or not reachable by the bot."""
    which = company.statement_env if kind == "statement" else company.invoice_env
    return notice(
        f"{company.name} channel not available",
        f"Set `{which}` to a channel id in a server I am in, and give me "
        "**View Channel** + **Send Messages** there, then try again.",
        error=True,
    )


_ERROR_ICONS = ("❌", "🚫", "⚠️", "⚠")


def split_text(text: str) -> Tuple[str, str, str]:
    """Split a plain chat line like ``"❌ Invoice not found."`` into (icon, title, body)."""
    text = (text or "").strip() or "Done."
    icon = ""
    match = re.match(r"^(\S+)\s+(.*)$", text, re.S)
    if match and not re.search(r"[A-Za-z0-9]", match.group(1)) and len(match.group(1)) <= 8:
        icon, text = match.group(1), match.group(2)
    first, _, rest = text.partition("\n")
    return icon, first.strip() or "Done.", rest.strip()


def text_notice(text: str, *, error: Optional[bool] = None) -> StatusView:
    """Turn a plain message into a Components V2 status card."""
    icon, title, body = split_text(text)
    is_error = icon in _ERROR_ICONS if error is None else error
    shown = "❌" if is_error else (icon or "✅")
    return StatusView(f"{shown} {title}", body)


async def say(
    interaction: discord.Interaction,
    text: str,
    *,
    ephemeral: bool = True,
    error: Optional[bool] = None,
    file: Optional[discord.File] = None,
) -> None:
    """Reply to an interaction with a V2 status card (works before or after a defer)."""
    view = text_notice(text, error=error)
    kwargs: Dict[str, Any] = {"view": view, "ephemeral": ephemeral}
    if file is not None:
        kwargs["file"] = file
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


def card_items(
    title: str,
    *blocks: str,
    footer: Optional[str] = None,
    rows: Sequence[discord.ui.Item] = (),
    level: str = "##",
    extra: Sequence[discord.ui.Item] = (),
) -> List[discord.ui.Item]:
    """Items for a flat card: title, each text block behind a divider, footer, button rows."""
    items: List[discord.ui.Item] = [discord.ui.TextDisplay(heading(title, level=level))]
    for block in blocks:
        if block:
            items.append(discord.ui.Separator())
            items.append(discord.ui.TextDisplay(block))
    items.extend(extra)
    if footer:
        footer_items(items, footer)
    elif rows:
        ensure_separator(items)
    items.extend(rows)
    return items


def build_card(
    title: str,
    *blocks: str,
    footer: Optional[str] = None,
    rows: Sequence[discord.ui.Item] = (),
    level: str = "##",
    timeout: Optional[float] = None,
    extra: Sequence[discord.ui.Item] = (),
) -> PersistentView:
    """A ready-to-send flat card (see :func:`card_items`)."""
    view = PersistentView(timeout=timeout)
    view.add_item(
        discord.ui.Container(
            *card_items(title, *blocks, footer=footer, rows=rows, level=level, extra=extra)
        )
    )
    return view


def file_card(title: str, body: str, filename: str, *, footer: Optional[str] = None) -> PersistentView:
    """Card that embeds an attached file (send together with ``file=discord.File(..., filename=filename)``)."""
    items: List[discord.ui.Item] = title_and_body(title, body)
    items.append(discord.ui.File(f"attachment://{filename}"))
    if footer:
        footer_items(items, footer)
    view = PersistentView(timeout=None)
    view.add_item(discord.ui.Container(*items))
    return view


async def resolve_channel(
    bot: discord.Client, channel_id: Optional[int]
) -> Optional[discord.abc.Messageable]:
    """Look up a configured channel by id, fetching it if it is not cached."""
    if channel_id is None:
        return None
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def _epoch(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _when(iso: Optional[str]) -> str:
    epoch = _epoch(iso)
    return f"<t:{epoch}:f>" if epoch else "—"


def _val(value: Any, code: bool = False) -> str:
    text = str(value).strip() if value not in (None, "") else ""
    if not text:
        return "—"
    return f"`{text}`" if code else text


def _method(value: Any) -> str:
    text = str(value or "").strip()
    return _val("UPI" if text.lower() == "upi" else text.title())


def _field(label: str, value: str) -> str:
    return f"**{label}:** {value}"


def _fit(lines: Sequence[str], budget: int = TEXT_BUDGET) -> List[str]:
    """Keep as many lines as fit the text budget, noting any overflow."""
    out: List[str] = []
    used = 0
    for index, line in enumerate(lines):
        if used + len(line) + 1 > budget:
            out.append(f"_…and {len(lines) - index} more_")
            break
        out.append(line)
        used += len(line) + 1
    return out


def _single(view: PersistentView, items: List[discord.ui.Item]) -> PersistentView:
    view.add_item(discord.ui.Container(*items))
    return view


# --------------------------------------------------------------------------
# Sales Statement (one sale)
# --------------------------------------------------------------------------
def sale_card(sale: Dict[str, Any], total_sales: float) -> PersistentView:
    """Sales Statement for one payment received, with the running total."""
    body = "\n".join(
        [
            _field("Transaction ID", _val(sale.get("sale_id"), code=True)),
            _field("Company", config.company_name(sale.get("company"))),
            _field("Date", store.pretty_date(store._entry_date(sale))),
            _field("Client Name", _val(sale.get("client_name") or sale.get("customer"))),
            _field("Client Contact", _val(sale.get("client_contact"))),
            _field("Server Name", _val(sale.get("server_name"))),
            _field("Server UUID", _val(sale.get("server_uuid"), code=True)),
            _field("Service/Plan", _val(sale.get("service"))),
            _field("Amount Received", store.money(sale['amount'])),
            _field("Payment Method", _method(sale.get("method"))),
            _field("Transaction ID / Ref", _val(sale.get("transaction_id"), code=True)),
            _field("Remarks", _val(sale.get("remarks"))),
        ]
    )
    items = title_and_body("Sales Statement", body)
    items.append(discord.ui.Separator())
    items.append(discord.ui.TextDisplay(_field("Total Sales", store.money(total_sales))))
    footer_items(
        items, f"Recorded by {sale.get('recorded_by', '—')} · {_when(sale.get('created_at'))}"
    )
    return _single(PersistentView(timeout=None), items)


def sales_statement_card(
    payments: Sequence[Dict[str, Any]], period: str, total: float, company: str = ""
) -> PersistentView:
    """Sales Statement covering a period (one compact line per sale)."""
    lines = _fit(
        [
            (f"`{p['sale_id']}` · " if p.get("sale_id") else "")
            + f"`{store.pretty_date(store._entry_date(p))}` · "
            f"**{_val(p.get('client_name') or p.get('customer'))}** · "
            f"{_val(p.get('service'))} · **{store.money(p['amount'])}** · "
            f"{_method(p.get("method"))}"
            + (f" · `{p['transaction_id']}`" if p.get("transaction_id") else "")
            for p in payments
        ]
    )
    body = (_field("Company", company) + "\n" if company else "") + _field("Period", store.PERIODS.get(period, period))
    body += "\n\n" + ("\n".join(lines) if lines else "_No sales recorded in this period._")
    items = title_and_body("Sales Statement", body)
    items.append(discord.ui.Separator())
    items.append(discord.ui.TextDisplay(_field("Total Sales", store.money(total))))
    footer_items(items, f"{len(payments)} sale(s) · Generated {_when(store._stamp())}")
    return _single(PersistentView(timeout=None), items)


# --------------------------------------------------------------------------
# Expense Statement (one expense)
# --------------------------------------------------------------------------
def expense_card(expense: Dict[str, Any], total_expenses: float) -> PersistentView:
    """Expense Statement for one expense, with the running total."""
    body = "\n".join(
        [
            _field("Company", config.company_name(expense.get("company"))),
            _field("Date", store.pretty_date(str(expense.get("date", "")))),
            _field("Expense Description", _val(expense.get("description"))),
            _field("Category", _val(expense.get("category"))),
            _field("Amount", store.money(expense['amount'])),
            _field("Payment Method", _method(expense.get("method"))),
            _field("Paid To", _val(expense.get("paid_to") or expense.get("vendor"))),
            _field("Transaction ID", _val(expense.get("transaction_id"), code=True)),
            _field("Remarks", _val(expense.get("remarks") or expense.get("note"))),
        ]
    )
    items = title_and_body("Expense Statement", body)
    items.append(discord.ui.Separator())
    items.append(
        discord.ui.TextDisplay(_field("Total Expenses", store.money(total_expenses)))
    )
    footer_items(
        items,
        f"Recorded by {expense.get('recorded_by', '—')} · {_when(expense.get('created_at'))}",
    )
    return _single(PersistentView(timeout=None), items)


def expenses_statement_card(
    expenses: Sequence[Dict[str, Any]], period: str, total: float, company: str = ""
) -> PersistentView:
    """Expense Statement covering a period (one compact line per expense)."""
    lines = _fit(
        [
            f"`{store.pretty_date(str(e.get('date', '')))}` · "
            f"**{_val(e.get('description') or e.get('category'))}** · "
            f"{_val(e.get('category'))} · **{store.money(e['amount'])}** · "
            f"{_val(e.get('paid_to') or e.get('vendor'))}"
            for e in expenses
        ]
    )
    body = (_field("Company", company) + "\n" if company else "") + _field("Period", store.PERIODS.get(period, period))
    body += "\n\n" + ("\n".join(lines) if lines else "_No expenses recorded in this period._")
    items = title_and_body("Expense Statement", body)
    items.append(discord.ui.Separator())
    items.append(discord.ui.TextDisplay(_field("Total Expenses", store.money(total))))
    footer_items(items, f"{len(expenses)} expense(s) · Generated {_when(store._stamp())}")
    return _single(PersistentView(timeout=None), items)


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
def invoice_card(inv: Dict[str, Any]) -> PersistentView:
    """Invoice card — posted to INVOICE_CHANNEL_ID only."""
    paid = inv.get("status") == "paid"
    status = "Paid ✅" if paid else "Awaiting payment ⏳"
    if paid:
        status += f" · {_method(inv.get('paid_method'))} · {_when(inv.get('paid_at'))}"

    body = "\n".join(
        [
            _field("Company", config.company_name(inv.get("company"))),
            _field("Client Name", _val(inv.get("customer"))),
            _field("Client Contact", _val(inv.get("contact"))),
            _field("Service/Plan", _val(inv.get("service") or inv.get("description"))),
            _field("Amount", store.money(inv['amount'])),
            _field("Due Date", _val(inv.get("due"))),
            _field("Status", status),
        ]
    )
    items = title_and_body(f"Invoice {inv['number']}", body)
    footer_items(items, f"Raised by {inv.get('created_by', '—')} · {_when(inv.get('created_at'))}")

    if paid:
        button: discord.ui.Item = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Paid",
            emoji="✅",
            custom_id=f"inv:paid:{inv['number']}",
            disabled=True,
        )
    else:
        button = MarkPaidButton.build(inv["number"])
    items.append(discord.ui.ActionRow(button))
    return _single(PersistentView(timeout=None), items)


def quick_invoice_line(inv: Dict[str, Any]) -> str:
    """One compact line for a quick invoice."""
    state = "paid ✅" if inv.get("status") == "paid" else "unpaid ⏳"
    return (f"{'✅' if inv.get('status') == 'paid' else '⏳'} **{inv['number']}** · "
            f"{_val(inv.get('customer'))} · {store.money(inv['amount'])} ({state})"
            f" · due {_val(inv.get('due'))}")


def revenue_card(revenue: Dict[str, Any], totals: Optional[Dict[str, Any]] = None) -> PersistentView:
    """Revenue Statement for one entry (posted only to that company's channel).

    Same shape as the Expense Statement, plus ``totals`` (from
    ``Database.revenue_summary``) shows that company's running Total Revenue.
    Investment is listed separately because it is not operating revenue.
    """
    number = revenue.get("number") or f"REV-{int(revenue['id']):06d}"
    body = "\n".join(
        [
            _field("Transaction ID", f"`{number}`"),
            _field("Company", config.company_name(revenue.get("company_id"))),
            _field("Date", store.pretty_date(str(revenue.get("occurred_on") or revenue.get("date") or ""))),
            _field("Category", _val(revenue.get("category"))),
            _field("Amount", store.money(revenue["amount"] / 100)),
            _field("Received From", _val(revenue.get("received_from"))),
            _field("Payment Method", _method(revenue.get("method"))),
            _field("Transaction ID / Ref", _val(revenue.get("transaction_id"), code=True)),
            _field("Description", _val(revenue.get("description"))),
            _field("Remarks", _val(revenue.get("remarks"))),
            _field("Recorded By", _val(revenue.get("recorded_by"))),
        ]
    )
    items = title_and_body("Revenue Statement", body)
    if totals is not None:
        items.append(discord.ui.Separator())
        items.append(discord.ui.TextDisplay(
            _field("Total Revenue", store.money((totals.get("operating") or 0) / 100))))
        if totals.get("investment"):
            items.append(discord.ui.TextDisplay(_field(
                "Investment (tracked separately)", store.money(totals["investment"] / 100))))
        items.append(discord.ui.TextDisplay(_field(
            "Total received (revenue + investment)",
            store.money(((totals.get("operating") or 0) + (totals.get("investment") or 0)) / 100))))
    footer_items(items, f"Recorded {_when(revenue.get('created_at'))}")
    return _single(PersistentView(timeout=None), items)


def invoice_list_card(invoices: Sequence[Dict[str, Any]]) -> PersistentView:
    """Compact invoice rundown, always sent ephemerally."""
    unpaid = [i for i in invoices if i.get("status") != "paid"]
    outstanding = sum(i["amount"] for i in unpaid)
    lines = _fit(
        [
            f"`{i['number']}` · **{_val(i.get('customer'))}** · {store.money(i['amount'])} · "
            f"{'paid ✅' if i.get('status') == 'paid' else 'unpaid ⏳'}"
            for i in invoices
        ]
    )
    body = "\n".join(lines)
    items = title_and_body("Invoices", body)
    items.append(discord.ui.Separator())
    items.append(discord.ui.TextDisplay(_field("Outstanding", store.money(outstanding))))
    footer_items(items, f"{len(unpaid)} unpaid of {len(invoices)}")
    return _single(PersistentView(timeout=None), items)


# --------------------------------------------------------------------------
# "Mark as paid": button -> modal -> sale recorded
# --------------------------------------------------------------------------
class MarkPaidModal(discord.ui.Modal, title="Mark invoice as paid"):
    method = discord.ui.TextInput(
        label="Payment method", default="UPI", placeholder="UPI / Bank transfer / Card / Cash", max_length=40
    )
    transaction_id = discord.ui.TextInput(label="Transaction ID", required=False, max_length=100)
    server_name = discord.ui.TextInput(label="Server name", required=False, max_length=100)
    server_uuid = discord.ui.TextInput(label="Server UUID", required=False, max_length=100)
    remarks = discord.ui.TextInput(
        label="Remarks", style=discord.TextStyle.paragraph, required=False, max_length=300
    )

    def __init__(self, number: str, company: str = "") -> None:
        super().__init__()
        self.number = number
        self.company = company

    async def on_submit(self, interaction: discord.Interaction) -> None:
        stored = (store.invoice(self.number) or {}).get("company", "")
        company = config.get_company(self.company or stored)
        if company is None:
            await interaction.response.send_message(
                view=notice(
                    "No company assigned",
                    f"Invoice `{self.number}` has no company yet. Use `/invoice create` with a company, "
                    "or re-record the sale with `/sale add company:...`.",
                    error=True,
                ),
                ephemeral=True,
            )
            return
        # Check the company's statement channel first so a sale is never saved unannounced.
        channel = await resolve_channel(interaction.client, company.channel_id("statement"))
        if channel is None:
            await interaction.response.send_message(
                view=missing_company_channel_notice(company), ephemeral=True
            )
            return

        result = store.mark_paid(
            self.number,
            method=str(self.method.value),
            transaction_id=str(self.transaction_id.value or ""),
            server_name=str(self.server_name.value or ""),
            server_uuid=str(self.server_uuid.value or ""),
            remarks=str(self.remarks.value or ""),
            paid_by=interaction.user.display_name,
            company=company.key,
        )
        if result is None:
            await interaction.response.send_message(
                view=notice("Already paid", f"Invoice `{self.number}` is missing or already paid.", error=True),
                ephemeral=True,
            )
            return
        invoice, sale = result

        # 1) the invoice message flips to its paid state
        await interaction.response.edit_message(view=invoice_card(invoice))

        # keep the finance books (dashboard / P&L) in step with the ledger
        try:
            from desk.bridge import mirror_sale

            await mirror_sale(interaction.client, sale, interaction.user.id)
        except Exception:
            log.exception("Could not mirror the sale into the finance books")

        # 2) the Sales Statement goes to that company's channel, never another company's
        posted = None
        try:
            posted = await channel.send(view=sale_card(sale, store.total_sales(company=company.key)))
        except discord.HTTPException:
            await interaction.followup.send(
                view=notice(
                    "Could not post sales statement",
                    f"The invoice is marked paid, but I could not post to <#{channel.id}> "
                    f"for **{company.name}** — check my **Send Messages** permission there.",
                    error=True,
                ),
                ephemeral=True,
            )
        # remember the transaction id and the posted message (so it can be deleted by id later)
        from desk.bridge import register_statement

        await register_statement(
            interaction.client, sale.get("sale_id"), "sale", company.key, sale.get("id"), posted)


def _open_paid_modal(number: str, company_key: str):
    async def callback(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(MarkPaidModal(number, company_key))

    return callback


def company_picker(number: str, title: str = "Pick a company") -> PersistentView:
    """Ephemeral card whose buttons open 'mark as paid' for the chosen company.

    Used for older invoices that were saved before multi-company support, so the
    company is never guessed.
    """
    row = discord.ui.ActionRow()
    for company in config.COMPANIES.values():
        button = discord.ui.Button(label=company.name, style=discord.ButtonStyle.secondary)
        button.callback = _open_paid_modal(number, company.key)
        row.add_item(button)
    view = PersistentView(timeout=180)
    view.add_item(discord.ui.Container(*card_items(
        title,
        f"Invoice `{number}` predates multi-company support, so it has no company yet. "
        "Pick the company this payment belongs to.",
        rows=[row],
    )))
    return view


class MarkPaidButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"inv:pay:(?P<number>INV-[0-9]+)",
):
    """Keeps working on invoice messages across bot restarts."""

    def __init__(self, item: discord.ui.Button, number: str = "") -> None:
        super().__init__(item)
        self.number = number or item.custom_id.split(":", 2)[2]

    @classmethod
    def build(cls, number: str) -> "MarkPaidButton":
        return cls(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Mark as paid",
                custom_id=f"inv:pay:{number}",
            ),
            number,
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "MarkPaidButton":
        return cls(item, match["number"])

    async def callback(self, interaction: discord.Interaction) -> None:
        from desk.utils import is_staff

        if not is_staff(interaction.user):
            await interaction.response.send_message(
                view=notice("Staff only", "You need a staff role to mark invoices as paid.", error=True),
                ephemeral=True,
            )
            return
        inv = store.invoice(self.number)
        if inv is None:
            await interaction.response.send_message(
                view=notice("Not found", f"Invoice `{self.number}` no longer exists.", error=True),
                ephemeral=True,
            )
            return
        if inv.get("status") == "paid":
            await interaction.response.send_message(
                view=notice("Already paid", f"Invoice `{self.number}` is already marked as paid."),
                ephemeral=True,
            )
            return
        if config.get_company(inv.get("company")) is None:
            await interaction.response.send_message(view=company_picker(self.number), ephemeral=True)
            return
        await interaction.response.send_modal(MarkPaidModal(self.number, inv["company"]))
