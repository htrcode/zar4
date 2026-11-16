"""Buying-enquiry pipeline: stages, notes, follow-ups and reminders."""
import logging
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import ui
from desk.utils import (DEAL_STAGES, STAGE_META, fmt, parse_when, staff_interaction_check,
                   to_cents, today)
from desk.views import build_deal_view, simple_card

log = logging.getLogger("invoice-bot.sales")
TicketChannel = Union[discord.TextChannel, discord.Thread]


@app_commands.guild_only()
class Sales(commands.GroupCog, group_name="sales", group_description="Buying enquiries and follow-ups"):
    def __init__(self, bot):
        self.bot = bot
        self.bot.sales = self
        super().__init__()
        self.reminders.start()

    async def cog_unload(self):
        self.reminders.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    async def _ticket(self, channel) -> dict:
        if channel is None:
            raise ValueError("Pick a ticket channel.")
        ticket = await self.bot.db.ticket_by_channel(channel.id)
        if not ticket:
            raise ValueError("That channel is not a ticket. Use `/ticket open` first.")
        return ticket

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="add", description="Track a new deal manually (ticket optional)")
    @app_commands.choices(stage=[app_commands.Choice(name=s.title(), value=s) for s in DEAL_STAGES])
    async def add(self, interaction: discord.Interaction, client: Optional[discord.Member] = None,
                  service: str = "General", budget: app_commands.Range[float, 0.0, 100_000_000.0] = 0.0,
                  ticket: Optional[TicketChannel] = None, stage: Optional[app_commands.Choice[str]] = None,
                  followup: Optional[str] = None, note: Optional[str] = None,
                  client_name: Optional[str] = None):
        ticket_row = None
        if ticket:
            try:
                ticket_row = await self._ticket(ticket)
            except ValueError as e:
                return await ui.say(interaction, f"❌ {e}")
            if client is None and ticket_row.get("client_discord_id"):
                client = interaction.guild.get_member(ticket_row["client_discord_id"])
            client_name = client_name or ticket_row["client_name"]
        if client is None and not client_name:
            return await ui.say(interaction, "❌ Pick a client (or pass the ticket so I can read it from there).")
        try:
            when = parse_when(followup) if followup else today().isoformat()
            deal_id = await self.bot.db.create_deal(
                interaction.user.id, client_name=client_name or client.display_name,
                client_discord_id=client.id if client else None, service=service,
                budget=to_cents(budget), stage=stage.value if stage else "new",
                ticket_id=ticket_row["id"] if ticket_row else None, notes=note or "",
                next_follow_up=when,
            )
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        deal = await self.bot.db.get_deal(deal_id)
        await interaction.response.send_message(view=await build_deal_view(self.bot, deal_id), ephemeral=True)
        if ticket_row:
            try:
                channel = self.bot.get_channel(ticket_row["channel_id"])
                if channel:
                    await channel.send(view=ui.build_card(
                        f"Sales deal #{deal_id} created",
                        f"{ui._field('Client Name', deal['client_name'])}\n"
                        f"{ui._field('Service', deal['service'] or 'General')}", timeout=180))
            except discord.HTTPException:
                pass

    @app_commands.command(name="pipeline", description="Pipeline value, weighted forecast and follow-up load")
    async def pipeline(self, interaction: discord.Interaction):
        rows = await self.bot.db.pipeline()
        due = await self.bot.db.deals_due()
        upcoming = await self.bot.db.deals_due(parse_when(f"+{config.SALES_LOOKAHEAD_DAYS}d"))
        overdue = [d for d in due if d["next_follow_up"] < today().isoformat()]
        open_rows = [r for r in rows if r["stage"] not in ("won", "lost")]
        won = next(r for r in rows if r["stage"] == "won")
        lost = next(r for r in rows if r["stage"] == "lost")
        lines = [
            f"**Open pipeline:** {sum(r['count'] for r in open_rows)} deals · "
            f"value **{fmt(sum(r['value'] for r in open_rows))}** · "
            f"weighted forecast **{fmt(sum(r['weighted'] for r in open_rows))}**",
            f"**Won:** {won['count']} · {fmt(won['value'])}  ·  **Lost:** {lost['count']}",
            f"**Follow-ups due:** {len(due)}" + (f" ({len(overdue)} overdue)" if overdue else "")
            + f"  ·  next {config.SALES_LOOKAHEAD_DAYS} days: {len(upcoming)}",
            "",
            "**Stages**",
        ]
        for row in rows:
            label, chance = STAGE_META[row["stage"]]
            if row["stage"] in ("won", "lost"):
                lines.append(f"{label}: **{row['count']}** · {fmt(row['value'])}")
            else:
                lines.append(f"{label}: **{row['count']}** · value {fmt(row['value'])} · "
                             f"{int(chance * 100)}% · weighted {fmt(row['weighted'])}")
        if due:
            lines += ["", "**Due now**"]
            for deal in due[:8]:
                ticket = f"<#{deal['ticket_channel_id']}>" if deal.get("ticket_channel_id") else "no ticket"
                lines.append(f"• **#{deal['id']}** {deal['client_name'][:20]} · {deal['next_follow_up']} · "
                             f"{ticket} · {deal['stage']}")
        await interaction.response.send_message(view=simple_card("📈 Sales pipeline", "\n".join(lines)),
                                                ephemeral=True)

    @app_commands.command(name="list", description="List deals by stage")
    @app_commands.choices(stage=[app_commands.Choice(name=s.title(), value=s) for s in DEAL_STAGES])
    async def list_cmd(self, interaction: discord.Interaction,
                       stage: Optional[app_commands.Choice[str]] = None):
        rows = await self.bot.db.list_deals(stage.value if stage else None, limit=25)
        if not rows:
            return await interaction.response.send_message(
                view=simple_card("Deals", "No deals match that filter."), ephemeral=True)
        lines = []
        for deal in rows:
            label = STAGE_META.get(deal["stage"], (deal["stage"], 0))[0]
            ticket = f"<#{deal['ticket_channel_id']}>" if deal.get("ticket_channel_id") else "—"
            lines.append(f"**#{deal['id']}** · {label} · {deal['client_name'][:20]} · "
                         f"{deal.get('service') or 'General'} · {fmt(deal['value'] or deal['budget'])} · "
                         f"follow-up {deal['next_follow_up'] or '—'} · {ticket}")
        await interaction.response.send_message(
            view=simple_card("Deals" + (f" · {stage.value}" if stage else ""), "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="deal", description="Open a deal card with stage controls")
    async def deal(self, interaction: discord.Interaction, deal_id: int):
        view = await build_deal_view(self.bot, deal_id)
        if not view:
            return await ui.say(interaction, "❌ Deal not found.")
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="ticket", description="Open the deal linked to a ticket")
    async def deal_for_ticket(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        try:
            if ticket:
                ticket_row = await self.bot.db.get_ticket(ticket)
            else:
                ticket_row = await self.bot.db.ticket_by_channel(interaction.channel_id)
            if not ticket_row:
                raise ValueError("Ticket not found.")
            deal = await self.bot.db.deal_by_ticket(ticket_row["id"])
            if not deal:
                raise ValueError("This ticket has no sales deal (support tickets don't get one).")
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await interaction.response.send_message(view=await build_deal_view(self.bot, deal["id"]), ephemeral=True)

    @app_commands.command(name="stage", description="Move a deal to a stage")
    @app_commands.choices(stage=[app_commands.Choice(name=s.title(), value=s) for s in DEAL_STAGES])
    async def stage_cmd(self, interaction: discord.Interaction, deal_id: int,
                        stage: app_commands.Choice[str], detail: Optional[str] = None):
        try:
            await self.bot.db.set_deal_stage(interaction.user.id, deal_id, stage.value, detail or "")
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"✅ Deal **#{deal_id}** moved to **{stage.value}**.")

    @app_commands.command(name="followup", description="Set the next follow-up date")
    async def followup(self, interaction: discord.Interaction, deal_id: int, when: str,
                       note: Optional[str] = None):
        try:
            date = parse_when(when)
            await self.bot.db.set_deal_followup(interaction.user.id, deal_id, date, note or "")
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"📅 Deal **#{deal_id}** follow-up set for **{date}**.")

    @app_commands.command(name="note", description="Add a note to a deal")
    async def note(self, interaction: discord.Interaction, deal_id: int, note: str):
        try:
            await self.bot.db.add_deal_note(interaction.user.id, deal_id, note)
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, "📝 Deal note saved.")

    @app_commands.command(name="won", description="Mark a deal won (optionally link its invoice)")
    async def won(self, interaction: discord.Interaction, deal_id: int, invoice_id: Optional[int] = None):
        try:
            if invoice_id:
                await self.bot.db.link_deal_invoice(interaction.user.id, deal_id, invoice_id)
            await self.bot.db.set_deal_stage(interaction.user.id, deal_id, "won",
                                             f"Won via /sales won (invoice {invoice_id or 'none'})")
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"✅ Deal **#{deal_id}** marked won.")

    @app_commands.command(name="lost", description="Mark a deal lost")
    async def lost(self, interaction: discord.Interaction, deal_id: int, reason: Optional[str] = None):
        try:
            await self.bot.db.set_deal_stage(interaction.user.id, deal_id, "lost", reason or "No reason given")
        except ValueError as e:
            return await ui.say(interaction, f"❌ {e}")
        await ui.say(interaction, f"❌ Deal **#{deal_id}** marked lost.")

    @app_commands.command(name="due", description="List due and upcoming follow-ups")
    async def due(self, interaction: discord.Interaction, ahead: app_commands.Range[int, 0, 90] = 0):
        until = parse_when(f"+{ahead}d")
        rows = await self.bot.db.deals_due(until)
        if not rows:
            return await interaction.response.send_message(
                view=simple_card(f"Follow-ups until {until}", "Nothing due. 🎉"), ephemeral=True)
        lines = []
        for deal in rows:
            overdue = " ⚠️ overdue" if deal["next_follow_up"] < today().isoformat() else ""
            ticket = f"<#{deal['ticket_channel_id']}>" if deal.get("ticket_channel_id") else "no ticket"
            lines.append(f"**#{deal['id']}** · {deal['next_follow_up']}{overdue} · "
                         f"{deal['client_name'][:20]} · {deal['stage']} · {ticket}"
                         + (f"\n  ↳ {deal['follow_up_note'][:120]}" if deal.get("follow_up_note") else ""))
        await interaction.response.send_message(
            view=simple_card(f"Follow-ups until {until}", "\n".join(lines)), ephemeral=True)

    # ------------------------------------------------------------------ reminders
    @tasks.loop(minutes=max(config.FOLLOWUP_CHECK_MINUTES, 5))
    async def reminders(self):
        try:
            rows = await self.bot.db.deals_due_for_reminder()
            if not rows:
                return
            for deal in rows:
                text = "\n".join(filter(None, [
                    ui._field("Client Name", deal["client_name"]),
                    ui._field("Service", deal.get("service") or "General"),
                    ui._field("Stage", deal["stage"]),
                    ui._field("Value", fmt(deal["value"] or deal["budget"])),
                    ui._field("Date", deal["next_follow_up"]),
                    ui._field("Note", deal["follow_up_note"][:300]) if deal.get("follow_up_note") else "",
                    ui._field("Ticket", f"<#{deal['ticket_channel_id']}>") if deal.get("ticket_channel_id") else "",
                ]))
                targets = []
                if config.SALES_CHANNEL_ID:
                    targets.append(config.SALES_CHANNEL_ID)
                if deal.get("ticket_channel_id"):
                    targets.append(deal["ticket_channel_id"])
                sent = False
                for channel_id in dict.fromkeys(targets):
                    channel = self.bot.get_channel(channel_id)
                    if channel is None:
                        continue
                    try:
                        await channel.send(view=ui.build_card(f"📅 Follow-up due · Deal #{deal['id']}", text, timeout=180),
                                           allowed_mentions=discord.AllowedMentions.none())
                        sent = True
                    except discord.HTTPException:
                        log.warning("Could not send follow-up reminder to %s", channel_id)
                if sent:
                    await self.bot.db.mark_deal_reminded(deal["id"])
                else:
                    log.warning("No channel available for follow-up reminder on deal %s", deal["id"])
        except Exception:
            log.exception("Sales reminder task failed")

    @reminders.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Sales(bot))
