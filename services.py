"""Service expiry management: /service_add, /service_expiry, /service_info, /service_list, /service_cancel.

* Services live in SQLite (``services`` / ``service_expiry_log`` / ``service_reminders``, created by
  ``desk/database.py``), so nothing is lost on a restart.
* One background loop sends the daily reminders (5, 4, 3, 2, 1 days before expiry and on the day) to the
  client by DM and in the linked ticket channel. Each reminder is *claimed* in the database before it is sent
  (``UNIQUE(service, expiry date, days left)``), so a restart or a second tick can never send it twice, and
  changing the expiry date automatically starts a new schedule for the new date.
* Dates are plain calendar dates in ``config.TIMEZONE`` (Asia/Kolkata in this project).
* Every command is staff only (administrator or ``STAFF_ROLE_IDS``), like the rest of the bot.

Optional settings (add to ``config.py`` only if you want to change them):
``SERVICE_REMINDER_HOUR`` - local hour the daily reminders start going out (default 9).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import ui
from desk.database import service_number
from desk.utils import _tz, is_staff, parse_date, staff_interaction_check, today

log = logging.getLogger("invoice-bot.services")

REMINDER_DAYS = (5, 4, 3, 2, 1, 0)           # days before expiry (0 = the expiry date itself)
REMINDER_HOUR = int(getattr(config, "SERVICE_REMINDER_HOUR", 9))
TICK_MINUTES = 15                              # how often the loop looks for reminders that are due


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
def _d(iso: str) -> str:
    """2026-11-05 -> 05 Nov 2026 (a plain date: Discord timestamps would shift it for other time zones)."""
    try:
        return dt.date.fromisoformat(iso).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(iso or "—")


def days_until(expiry_iso: str, day: Optional[dt.date] = None) -> int:
    return (dt.date.fromisoformat(expiry_iso) - (day or today())).days


def _left(days: int) -> str:
    if days > 1:
        return f"{days} days left"
    if days == 1:
        return "1 day left"
    if days == 0:
        return "expires today"
    return f"expired {-days} day{'s' if days != -1 else ''} ago"


def _delta(days: int) -> str:
    if days == 0:
        return "no change"
    return f"+{days} day{'s' if days != 1 else ''} added" if days > 0 else \
        f"{-days} day{'s' if days != -1 else ''} removed"


DM_LABEL = {
    "sent": "✅ delivered",
    "closed": "⚠️ not delivered (DMs closed or user not found)",
    "error": "❌ failed (Discord error, see the bot log)",
}


def _ticket_label(status: str, svc: dict) -> str:
    return {
        "sent": f"✅ posted in <#{svc.get('ticket_channel_id')}>",
        "none": "— no ticket linked",
        "missing": "⚠️ ticket channel not found",
        "denied": "⚠️ no permission to post in the ticket",
        "error": "❌ failed (Discord error, see the bot log)",
    }.get(status, status)


# ---------------------------------------------------------------------------
# cards (Components V2, same flat design as the rest of the bot)
# ---------------------------------------------------------------------------
def _service_lines(svc: dict, *, mention: bool = False) -> list[str]:
    client = ui._val(svc["client_name"])
    if mention:
        client = f"<@{svc['client_discord_id']}>"
    return [
        ui._field("Service ID", service_number(svc["id"])),
        ui._field("Client", client),
        ui._field("Service", ui._val(svc["service_name"])),
        ui._field("Server", ui._val(svc["server_ref"], code=True)),
    ]


def reminder_card(svc: dict, days_left: int, *, mention: bool = False) -> ui.PersistentView:
    title = "⚠️ Your service expires today" if days_left == 0 else "⏰ Your service is expiring soon"
    lines = _service_lines(svc, mention=mention) + [
        ui._field("Expiry Date", _d(svc["expiry_date"])),
        ui._field("Time Left", _left(days_left)),
    ]
    return ui.build_card(title, "\n".join(lines),
                         footer="Open or reply in your ticket, or contact staff, to renew · "
                                f"{service_number(svc['id'])}")


def change_card(svc: dict, previous: str, new: str, days_changed: int, reason: Optional[str],
                *, mention: bool = False) -> ui.PersistentView:
    lines = _service_lines(svc, mention=mention) + [
        ui._field("Previous Expiry", _d(previous)),
        ui._field("New Expiry", _d(new)),
        ui._field("Change", _delta(days_changed)),
        ui._field("Time Left", _left(days_until(new))),
    ]
    if reason:
        lines.append(ui._field("Reason", ui._val(reason)))
    return ui.build_card("🔄 Your service expiry was updated", "\n".join(lines),
                         footer=f"Contact staff if this looks wrong · {service_number(svc['id'])}")


def info_card(svc: dict, changes: list[dict], reminders: list[dict]) -> ui.PersistentView:
    left = days_until(svc["expiry_date"])
    if svc["status"] == "cancelled":
        status = "🚫 Cancelled (reminders stopped)"
    elif left < 0:
        status = "⌛ Expired — still active, not cancelled"
    else:
        status = "✅ Active"
    lines = [
        ui._field("Service ID", service_number(svc["id"])),
        ui._field("Status", status),
        ui._field("Client", f"{ui._val(svc['client_name'])} (<@{svc['client_discord_id']}>)"),
        ui._field("Service", ui._val(svc["service_name"])),
        ui._field("Server", ui._val(svc["server_ref"], code=True)),
        ui._field("Started", _d(svc["start_date"])),
        ui._field("Expires", f"{_d(svc['expiry_date'])} · {_left(left)}"),
        ui._field("Ticket", f"<#{svc['ticket_channel_id']}>" if svc.get("ticket_channel_id") else "—"),
        ui._field("Added By", f"<@{svc['created_by']}> · {ui._when(svc['created_at'])}"),
    ]
    if svc["status"] == "cancelled":
        lines.append(ui._field("Cancelled", f"{ui._when(svc.get('cancelled_at'))} by <@{svc['cancelled_by']}>"
                                            + (f" · {svc['cancel_reason']}" if svc.get("cancel_reason") else "")))
    blocks = ["\n".join(lines)]
    if changes:
        rows = [f"{_d(c['previous_expiry'])} → {_d(c['new_expiry'])} ({_delta(c['days_changed'])})"
                f"{' · ' + c['reason'] if c.get('reason') else ''} · <@{c['changed_by']}>" for c in changes]
        blocks.append("**Expiry changes**\n" + "\n".join(ui._fit(rows, 900)))
    if reminders:
        rows = []
        for r in reminders:
            kind = "change notice" if r["kind"] == "expiry_change" else _left(r["days_left"])
            rows.append(f"{_d(r['expiry_date'])} · {kind} · DM {r['dm_status'] or '—'} · ticket {r['ticket_status'] or '—'}")
        blocks.append("**Reminder history**\n" + "\n".join(ui._fit(rows, 900)))
    return ui.build_card(f"🛠️ Service · {svc['service_name']}"[:240], *blocks,
                         footer=f"{service_number(svc['id'])} · dates in {config.TIMEZONE}")


def list_card(rows: list[dict], title: str) -> ui.PersistentView:
    day = today()
    soon = sum(1 for r in rows if 0 <= days_until(r["expiry_date"], day) <= 5)
    over = sum(1 for r in rows if days_until(r["expiry_date"], day) < 0)
    lines = []
    for r in rows:
        left = days_until(r["expiry_date"], day)
        mark = "⌛" if left < 0 else "🔴" if left <= 1 else "🟠" if left <= 5 else "🟢"
        lines.append(f"{mark} `{service_number(r['id'])}` · **{r['client_name']}** · {r['service_name']} · "
                     f"`{r['server_ref']}` · {_d(r['expiry_date'])} ({_left(left)})")
    summary = f"**{len(rows)}** service{'s' if len(rows) != 1 else ''} · **{soon}** expiring within 5 days"
    if over:
        summary += f" · **{over}** past expiry"
    return ui.build_card(title, summary, "\n".join(ui._fit(lines)),
                         footer=f"sorted by expiry · dates in {config.TIMEZONE}")


# ---------------------------------------------------------------------------
# delivery (DM + ticket) - never raises
# ---------------------------------------------------------------------------
async def _send_dm(bot, user_id: int, view) -> str:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(view=view)
        return "sent"
    except (discord.Forbidden, discord.NotFound):
        return "closed"
    except Exception:
        log.exception("DM to %s failed", user_id)
        return "error"


async def _send_ticket(bot, svc: dict, view) -> str:
    channel_id = svc.get("ticket_channel_id")
    if not channel_id:
        return "none"
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            return "missing"
        await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=svc["client_discord_id"])]))
        return "sent"
    except discord.NotFound:
        return "missing"
    except discord.Forbidden:
        return "denied"
    except Exception:
        log.exception("Ticket post in %s failed", channel_id)
        return "error"


async def notify_client(bot, svc: dict, make_view) -> tuple[str, str]:
    """Send one notice by DM and in the ticket. ``make_view(mention)`` builds a fresh card per message."""
    dm = await _send_dm(bot, svc["client_discord_id"], make_view(False))
    ticket = await _send_ticket(bot, svc, make_view(True))
    return dm, ticket


# ---------------------------------------------------------------------------
# input parsing
# ---------------------------------------------------------------------------
_SNOWFLAKE = re.compile(r"\d{15,20}")


def parse_user_id(text: str) -> Optional[int]:
    """A user mention (``<@123>``, ``<@!123>``) or a raw id."""
    match = _SNOWFLAKE.search(text or "")
    return int(match.group()) if match else None


async def resolve_ticket(db, guild_id: int, text: str) -> tuple[Optional[int], Optional[str]]:
    """``#channel``, a channel id, or a ticket number (``TICKET-0012`` / ``12``) -> (channel id, error)."""
    raw = (text or "").strip()
    if not raw:
        return None, None
    ticket = None
    channel_id = parse_user_id(raw) if raw.startswith("<#") or raw.isdigit() and len(raw) >= 15 else None
    if channel_id:
        ticket = await db.ticket_by_channel(channel_id)
    else:
        match = re.fullmatch(r"(?i)(?:[a-z]+-?)?0*(\d+)", raw)
        if match:
            ticket = await db.ticket_by_number(guild_id, int(match.group(1)))
    if ticket is None or ticket.get("guild_id") not in (None, guild_id) or not ticket.get("channel_id"):
        return None, ("I could not find that ticket in this server. Use the ticket channel (`#ticket-0012`), "
                      "its channel id, or its number (`TICKET-0012`).")
    return int(ticket["channel_id"]), None


def _next_reminder_text(expiry_iso: str, notified_today: bool) -> str:
    left = days_until(expiry_iso)
    if left < 0:
        return "no reminders (already past expiry)"
    if left > 5:
        first = dt.date.fromisoformat(expiry_iso) - dt.timedelta(days=5)
        return f"first reminder on {_d(first.isoformat())} (5 days before)"
    if left == 0:
        return "no more reminders after today" if notified_today else "reminder goes out today"
    nxt = today() + dt.timedelta(days=1)
    return f"next reminder {_d(nxt.isoformat())}" if notified_today else "next reminder goes out today"


# ---------------------------------------------------------------------------
# cog
# ---------------------------------------------------------------------------
class Services(commands.Cog):
    """Service expiry tracking with persistent, de-duplicated reminders."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._run_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        if not self.watcher.is_running():  # never two loops, even across extension reloads
            self.watcher.start()

    async def cog_unload(self) -> None:
        self.watcher.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    # -------------------------------------------------------------- helpers
    async def _lookup(self, interaction: discord.Interaction, query: str) -> Optional[dict]:
        """Resolve a service id / server UUID or tell the staff member why not."""
        found = await self.bot.db.find_services(interaction.guild_id, query)
        if not found:
            await ui.say(interaction, "❌ Service not found\nUse the service ID (`SVC-0001`) or the server "
                                      "name/UUID it was added with. `/service_list` shows every active one.")
            return None
        if len(found) > 1:
            ids = ", ".join(f"`{service_number(r['id'])}`" for r in found[:8])
            await ui.say(interaction, f"❌ More than one service matches\nUse the service ID instead: {ids}")
            return None
        return found[0]

    async def _autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # autocomplete does not run the cog's staff check, so repeat it: no client names for non-staff
        if not interaction.guild_id or not is_staff(interaction.user):
            return []
        rows = await self.bot.db.search_services(interaction.guild_id, current)
        out = []
        for r in rows:
            tag = "" if r["status"] == "active" else " (cancelled)"
            label = f"{service_number(r['id'])} · {r['client_name']} · {r['server_ref']}{tag}"
            out.append(app_commands.Choice(name=label[:100], value=service_number(r["id"])))
        return out

    # -------------------------------------------------------------- /service_add
    @app_commands.command(name="service_add", description="Register a client service and start its expiry reminders")
    @app_commands.describe(
        client_name="Client's name",
        client="Client's Discord user ID or @mention (reminders are DMed to them)",
        service="Service name, e.g. Minecraft 4GB",
        server="Server name or UUID",
        expiry_date="Expiry date, YYYY-MM-DD",
        start_date="Starting date, YYYY-MM-DD (default: today)",
        ticket="Ticket channel, channel ID or ticket number (optional)",
    )
    @app_commands.guild_only()
    async def service_add(
        self,
        interaction: discord.Interaction,
        client_name: app_commands.Range[str, 1, 100],
        client: str,
        service: app_commands.Range[str, 1, 100],
        server: app_commands.Range[str, 1, 100],
        expiry_date: str,
        start_date: Optional[str] = None,
        ticket: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            start = parse_date(start_date)
            expiry = parse_date(expiry_date)
        except ValueError as exc:
            return await ui.say(interaction, f"❌ Invalid date\n{exc}")
        if expiry < start:
            return await ui.say(interaction, "❌ Invalid dates\nThe expiry date is before the starting date.")
        if expiry < today().isoformat():
            return await ui.say(interaction, "❌ Invalid expiry\nThe expiry date is already in the past.")

        client_id = parse_user_id(client)
        user = None
        if client_id:
            try:
                user = await self.bot.fetch_user(client_id)
            except discord.HTTPException:
                user = None
        if user is None:
            return await ui.say(interaction, "❌ Client not found\nGive a valid Discord user ID or @mention.")

        channel_id, error = await resolve_ticket(self.bot.db, interaction.guild_id, ticket or "")
        if error:
            return await ui.say(interaction, f"❌ Ticket not found\n{error}")

        service_id = await self.bot.db.create_service(
            interaction.user.id, guild_id=interaction.guild_id, client_discord_id=user.id,
            client_name=client_name.strip(), service_name=service.strip(), server_ref=server.strip(),
            start_date=start, expiry_date=expiry, ticket_channel_id=channel_id)
        if service_id is None:
            existing = await self.bot.db.find_services(interaction.guild_id, server.strip())
            active = next((r for r in existing if r["status"] == "active"), None)
            hint = f" as `{service_number(active['id'])}`" if active else ""
            return await ui.say(interaction, f"❌ Already tracked\nThat server is already an active service{hint}. "
                                             "Use `/service_expiry` to change its date, or `/service_cancel` first.")
        svc = await self.bot.db.get_service(service_id)
        card = ui.build_card(
            f"✅ Service added · {service_number(service_id)}",
            "\n".join(_service_lines(svc) + [
                ui._field("Discord User", f"{user.mention} (`{user.id}`)"),
                ui._field("Started", _d(start)),
                ui._field("Expires", f"{_d(expiry)} · {_left(days_until(expiry))}"),
                ui._field("Ticket", f"<#{channel_id}>" if channel_id else "—"),
                ui._field("Reminders", _next_reminder_text(expiry, False)),
            ]),
            footer="Reminders: 5, 4, 3, 2, 1 days before and on the expiry date · DM + ticket")
        await interaction.followup.send(view=card, ephemeral=True)

    # -------------------------------------------------------------- /service_expiry
    @app_commands.command(name="service_expiry", description="Change a service's expiry date and tell the client")
    @app_commands.describe(
        service="Service ID (SVC-0001) or server name/UUID",
        new_expiry="New expiry date, YYYY-MM-DD",
        reason="Why the date changed (shown to the client, optional)",
    )
    @app_commands.guild_only()
    async def service_expiry(
        self,
        interaction: discord.Interaction,
        service: str,
        new_expiry: str,
        reason: Optional[app_commands.Range[str, 1, 300]] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        svc = await self._lookup(interaction, service)
        if svc is None:
            return
        if svc["status"] != "active":
            return await ui.say(interaction, f"❌ Service cancelled\n`{service_number(svc['id'])}` is cancelled, "
                                             "so its expiry cannot be changed. Add it again with `/service_add`.")
        try:
            new = parse_date(new_expiry)
        except ValueError as exc:
            return await ui.say(interaction, f"❌ Invalid date\n{exc}")
        if new < today().isoformat():
            return await ui.say(interaction, "❌ Invalid expiry\nThe new expiry date is in the past.")
        if new == svc["expiry_date"]:
            return await ui.say(interaction, f"❌ No change\n`{service_number(svc['id'])}` already expires on "
                                             f"{_d(new)}.")

        reason = (reason or "").strip() or None
        change = await self.bot.db.change_service_expiry(
            interaction.user.id, svc["id"], new, reason, today().isoformat())
        if change is None:  # cancelled by someone else a moment ago
            return await ui.say(interaction, "❌ Service cancelled\nIt was cancelled before the change could be saved.")

        updated = {**svc, "expiry_date": new}
        dm, ticket_status = await notify_client(
            self.bot, updated,
            lambda mention: change_card(updated, change["previous"], new, change["days_changed"], reason,
                                        mention=mention))
        if change["claimed"]:
            await self.bot.db.finish_service_notice(svc["id"], new, change["days_left"], dm, ticket_status)
        in_window = 0 <= change["days_left"] <= 5
        # claimed: the notice counts as today's reminder only if it reached the client;
        # not claimed: that day's slot was already used by a reminder that went out earlier
        notified_today = in_window and (("sent" in (dm, ticket_status)) if change["claimed"] else True)

        lines = _service_lines(updated) + [
            ui._field("Previous Expiry", _d(change["previous"])),
            ui._field("New Expiry", _d(new)),
            ui._field("Change", _delta(change["days_changed"])),
            ui._field("Time Left", _left(change["days_left"])),
        ]
        if reason:
            lines.append(ui._field("Reason", ui._val(reason)))
        lines += [
            ui._field("Client DM", DM_LABEL.get(dm, dm)),
            ui._field("Ticket", _ticket_label(ticket_status, updated)),
            ui._field("Reminders", "recalculated · " + _next_reminder_text(new, notified_today)),
        ]
        await interaction.followup.send(
            view=ui.build_card(f"🔄 Expiry updated · {service_number(svc['id'])}", "\n".join(lines),
                               footer="saved to the database"),
            ephemeral=True)

    # -------------------------------------------------------------- /service_info
    @app_commands.command(name="service_info", description="Show a service's details, expiry and history")
    @app_commands.describe(service="Service ID (SVC-0001) or server name/UUID")
    @app_commands.guild_only()
    async def service_info(self, interaction: discord.Interaction, service: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        svc = await self._lookup(interaction, service)
        if svc is None:
            return
        changes = await self.bot.db.service_expiry_history(svc["id"], 5)
        reminders = await self.bot.db.service_reminder_history(svc["id"], 8)
        await interaction.followup.send(view=info_card(svc, changes, reminders), ephemeral=True)

    # -------------------------------------------------------------- /service_list
    @app_commands.command(name="service_list", description="List active services, soonest expiry first")
    @app_commands.describe(days="Only services expiring within this many days (optional)")
    @app_commands.guild_only()
    async def service_list(self, interaction: discord.Interaction,
                           days: Optional[app_commands.Range[int, 1, 365]] = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        until = (today() + dt.timedelta(days=days)).isoformat() if days else None
        span = f"{days} day{'s' if days != 1 else ''}" if days else ""
        rows = await self.bot.db.list_services(interaction.guild_id, until=until)
        if not rows:
            return await ui.say(interaction, "✅ Nothing to show\n" + (
                f"No active service expires within {span}." if days else "There are no active services."))
        title = f"🛠️ Services expiring within {span}" if days else "🛠️ Active services"
        await interaction.followup.send(view=list_card(rows, title), ephemeral=True)

    # -------------------------------------------------------------- /service_cancel
    @app_commands.command(name="service_cancel", description="Stop a service's reminders (history is kept)")
    @app_commands.describe(service="Service ID (SVC-0001) or server name/UUID",
                           reason="Why it is cancelled (optional)")
    @app_commands.guild_only()
    async def service_cancel(self, interaction: discord.Interaction, service: str,
                             reason: Optional[app_commands.Range[str, 1, 300]] = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        svc = await self._lookup(interaction, service)
        if svc is None:
            return
        if svc["status"] == "cancelled":
            return await ui.say(interaction, f"❌ Already cancelled\n`{service_number(svc['id'])}` is already cancelled.")
        if not await self.bot.db.cancel_service(interaction.user.id, svc["id"], (reason or "").strip() or None):
            return await ui.say(interaction, "❌ Already cancelled\nSomeone cancelled it a moment ago.")
        await interaction.followup.send(
            view=ui.notice(f"Service cancelled · {service_number(svc['id'])}",
                           f"{ui._field('Client', ui._val(svc['client_name']))}\n"
                           f"{ui._field('Server', ui._val(svc['server_ref'], code=True))}\n"
                           "Reminders are stopped. The service, its expiry changes and reminder history are kept "
                           "(`/service_info`)."),
            ephemeral=True)

    service_expiry.autocomplete("service")(_autocomplete)
    service_info.autocomplete("service")(_autocomplete)
    service_cancel.autocomplete("service")(_autocomplete)

    # -------------------------------------------------------------- reminders
    async def run_reminders(self, now: Optional[dt.datetime] = None) -> int:
        """Send every reminder that is due and not yet claimed. Returns how many were attempted."""
        local = now or dt.datetime.now(_tz())
        if local.hour < REMINDER_HOUR:  # the day's reminders start at REMINDER_HOUR, then catch up after downtime
            return 0
        day = local.date()
        sent = 0
        async with self._run_lock:
            for svc in await self.bot.db.due_service_candidates(day.isoformat(), max(REMINDER_DAYS)):
                try:
                    days_left = days_until(svc["expiry_date"], day)
                    if days_left not in REMINDER_DAYS:
                        continue
                    if not await self.bot.db.claim_service_reminder(svc["id"], svc["expiry_date"], days_left):
                        continue  # already sent (or being sent): this is what stops duplicates after a restart
                    sent += 1
                    dm, ticket = await notify_client(
                        self.bot, svc, lambda mention, s=svc, d=days_left: reminder_card(s, d, mention=mention))
                    delivered = "sent" in (dm, ticket)
                    retry = not delivered and "error" in (dm, ticket)  # temporary Discord failure: try again later
                    await self.bot.db.finish_service_reminder(
                        svc["id"], svc["expiry_date"], days_left, dm, ticket, retry=retry)
                    if not delivered and not retry:
                        await self._alert_staff(svc, days_left, dm, ticket)
                except Exception:  # one bad record must not stop the others
                    log.exception("Reminder for service %s failed", svc.get("id"))
        return sent

    async def _alert_staff(self, svc: dict, days_left: int, dm: str, ticket: str) -> None:
        """Nobody could be reached: say so in the log channel (if configured) so staff can follow up."""
        log.warning("Reminder for %s could not be delivered (DM: %s, ticket: %s)",
                    service_number(svc["id"]), dm, ticket)
        channel = await ui.resolve_channel(self.bot, getattr(config, "LOG_CHANNEL_ID", None))
        if channel is None:
            return
        client = f"{ui._val(svc['client_name'])} (<@{svc['client_discord_id']}>)"
        expires = f"{_d(svc['expiry_date'])} · {_left(days_left)}"
        body = "\n".join([
            ui._field("Client", client), ui._field("Expires", expires),
            ui._field("DM", DM_LABEL.get(dm, dm)), ui._field("Ticket", _ticket_label(ticket, svc)),
            "Please contact the client yourself.",
        ])
        try:
            await channel.send(view=ui.notice(
                f"Reminder not delivered · {service_number(svc['id'])}", body, error=True))
        except discord.HTTPException:
            log.warning("Could not post the undelivered-reminder alert")

    @tasks.loop(minutes=TICK_MINUTES)
    async def watcher(self) -> None:
        try:
            await self.run_reminders()
        except Exception:
            log.exception("Service reminder loop failed")

    @watcher.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Services(bot))
