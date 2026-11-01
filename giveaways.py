"""Modern giveaways: one-click entries, live entry counts, automatic draw, rerolls.

State lives in SQLite (``giveaways`` / ``giveaway_entries`` / ``giveaway_winners``),
so a restart never loses an entry or a running giveaway; the Discord message is
only the display. The Enter button is a ``DynamicItem`` (``giveaway:enter:<id>``),
which keeps working on messages posted before a restart.

Cards use the bot's Components V2 style (``## title``, divider, ``**Label:**``
fields, ``-#`` footer, buttons at the bottom) via :mod:`ui`.
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

import ui
from desk.database import giveaway_number
from desk.utils import staff_interaction_check

log = logging.getLogger("invoice-bot.giveaways")
Channel = Union[discord.TextChannel, discord.Thread]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _epoch(value) -> int:
    """Unix timestamp for a stored ISO date (naive values are treated as UTC)."""
    if isinstance(value, (int, float)):
        return int(value)
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return int(time.time())
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp())


def _enter_button(giveaway: dict) -> discord.ui.Button:
    return discord.ui.Button(
        style=discord.ButtonStyle.success,
        label="Enter giveaway",
        emoji="🎉",
        custom_id=f"giveaway:enter:{giveaway['id']}",
    )


def giveaway_card(giveaway: dict, entries: int,
                  winners: Optional[Sequence[int]] = None) -> ui.PersistentView:
    """The giveaway card: prize, winners, countdown, live entry count, Enter button."""
    ended = giveaway.get("status") != "open"
    lines = [
        ui._field("Prize", ui._val(giveaway["prize"])),
        ui._field("Winners", str(giveaway["winners"])),
        ui._field("Hosted By", f"<@{giveaway['host_id']}>"),
        ui._field("Entries", str(entries)),
    ]
    if giveaway.get("required_role_id"):
        lines.append(ui._field("Requirement", f"<@&{giveaway['required_role_id']}>"))
    if ended:
        if winners:
            lines.append(ui._field("Won By", ", ".join(f"<@{user_id}>" for user_id in winners)))
        else:
            lines.append(ui._field("Result", "No valid entries — no winner."))
        footer = f"{giveaway_number(giveaway['id'])} · ended"
        rows: list = []
    else:
        ends = _epoch(giveaway["ends_at"])
        lines.append(ui._field("Ends", f"<t:{ends}:R> · <t:{ends}:f>"))
        footer = f"{giveaway_number(giveaway['id'])} · click Enter to join · draws automatically"
        rows = [discord.ui.ActionRow(_enter_button(giveaway))]
    return ui.build_card(f"🎉 Giveaway · {giveaway['prize']}"[:240], "\n".join(lines),
                         footer=footer, rows=rows)


def _pick(entries: Sequence[int], count: int, exclude: Sequence[int] = ()) -> list[int]:
    """Random winners: no duplicates, no repeat winners from ``exclude``."""
    skip = set(exclude)
    pool = [user_id for user_id in dict.fromkeys(entries) if user_id not in skip]
    random.shuffle(pool)
    return pool[:max(int(count), 0)]


async def _reply(interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


async def _channel(bot, channel_id):
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except (discord.HTTPException, TypeError):
        return None


async def _refresh_card(bot, giveaway: dict, entries: int, winners: Optional[Sequence[int]]) -> None:
    channel = await _channel(bot, giveaway.get("channel_id"))
    if channel is None or not giveaway.get("message_id"):
        return
    try:
        message = await channel.fetch_message(giveaway["message_id"])
        await message.edit(view=giveaway_card(giveaway, entries, winners))
    except discord.HTTPException:
        log.warning("Could not update the giveaway card for %s", giveaway.get("id"))


async def _announce(bot, giveaway: dict, winners: Sequence[int]) -> None:
    channel = await _channel(bot, giveaway.get("channel_id"))
    if channel is None:
        return
    if winners:
        content = ("🎉 Congratulations " + ", ".join(f"<@{user_id}>" for user_id in winners)
                   + f"! You won **{giveaway['prize']}**.")
        allowed = discord.AllowedMentions(users=True)
    else:
        content = f"**{giveaway['prize']}** ended with no valid entries."
        allowed = discord.AllowedMentions.none()
    try:
        await channel.send(content=content, allowed_mentions=allowed)
    except discord.HTTPException:
        log.warning("Could not announce the winners of giveaway %s", giveaway.get("id"))


async def _finish(bot, giveaway: dict, actor_id: int) -> Optional[list[int]]:
    """Draw and close a giveaway. None means somebody else closed it first."""
    entries = await bot.db.giveaway_entries(giveaway["id"])
    winners = _pick(entries, giveaway["winners"])
    if not await bot.db.finish_giveaway(actor_id, giveaway["id"], winners):
        return None
    ended = await bot.db.get_giveaway(giveaway["id"]) or giveaway
    await _refresh_card(bot, ended, len(entries), winners)
    await _announce(bot, ended, winners)
    return winners


# ---------------------------------------------------------------------------
# entry button (survives restarts through the dynamic-item template)
# ---------------------------------------------------------------------------
class GiveawayEnterButton(discord.ui.DynamicItem[discord.ui.Button],
                          template=r"giveaway:enter:(?P<id>[0-9]+)"):
    def __init__(self, item: discord.ui.Button, giveaway_id: int = 0) -> None:
        super().__init__(item)
        self.giveaway_id = giveaway_id or int(item.custom_id.rsplit(":", 1)[1])

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "GiveawayEnterButton":
        return cls(item, int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.bot:
            return
        db = getattr(interaction.client, "db", None)
        giveaway = await db.get_giveaway(self.giveaway_id) if db is not None else None
        if giveaway is None:
            return await _reply(interaction, ui.notice(
                "Giveaway gone", "That giveaway no longer exists.", error=True))
        if giveaway["status"] != "open" or _epoch(giveaway["ends_at"]) <= time.time():
            return await _reply(interaction, ui.notice(
                "Giveaway closed", f"**{giveaway['prize']}** has already ended.", error=True))
        role_id = giveaway.get("required_role_id")
        if role_id and all(role.id != role_id for role in getattr(interaction.user, "roles", [])):
            return await _reply(interaction, ui.notice(
                "Role required", f"You need <@&{role_id}> to enter this giveaway.", error=True))

        added = await db.enter_giveaway(giveaway["id"], interaction.user.id)
        count = await db.giveaway_entry_count(giveaway["id"])
        if not added:
            return await _reply(interaction, ui.notice(
                "Already entered", f"You are already in for **{giveaway['prize']}** · {count} entries."))
        try:
            await interaction.response.edit_message(view=giveaway_card(giveaway, count))
        except discord.HTTPException:
            log.warning("Could not refresh giveaway card %s after an entry", giveaway["id"])
        try:
            await interaction.followup.send(
                view=ui.notice("You're in 🎉",
                               f"**{giveaway['prize']}** · {count} entries so far · "
                               f"ends <t:{_epoch(giveaway['ends_at'])}:R>"),
                ephemeral=True,
            )
        except discord.HTTPException:
            log.warning("Could not confirm the giveaway entry to the member")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
@app_commands.guild_only()
class Giveaways(commands.GroupCog, group_name="giveaway", group_description="Giveaways with button entries"):
    def __init__(self, bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(GiveawayEnterButton)
        self.watcher.start()

    async def cog_unload(self) -> None:
        self.watcher.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    @app_commands.command(name="create", description="Start a giveaway with a one-click entry button")
    @app_commands.describe(
        prize="What the winner gets",
        minutes="How long it runs, in minutes",
        winners="How many winners to draw",
        channel="Where to post it (default: this channel)",
        required_role="Only members with this role can enter (optional)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        prize: str,
        minutes: app_commands.Range[int, 1, 10080] = 60,
        winners: app_commands.Range[int, 1, 20] = 1,
        channel: Optional[Channel] = None,
        required_role: Optional[discord.Role] = None,
    ) -> None:
        target = channel or interaction.channel
        if target is None:
            return await ui.say(interaction, "❌ Pick a channel for the giveaway.")
        me = getattr(interaction.guild, "me", None)
        if me is not None and not target.permissions_for(me).send_messages:
            return await ui.say(interaction, f"❌ I need **View Channel** and **Send Messages** in {target.mention}.")
        prize = prize.strip()
        if not prize:
            return await ui.say(interaction, "❌ Add the prize.")

        ends = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        giveaway_id = await self.bot.db.create_giveaway(
            interaction.user.id, guild_id=interaction.guild_id, channel_id=target.id,
            prize=prize, winners=winners,
            required_role_id=required_role.id if required_role else None,
            ends_at=ends.isoformat(timespec="seconds"),
        )
        giveaway = await self.bot.db.get_giveaway(giveaway_id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await target.send(view=giveaway_card(giveaway, 0))
        except discord.HTTPException as exc:
            # No half-open giveaway without a card to enter through.
            await self.bot.db.finish_giveaway(interaction.user.id, giveaway_id, [])
            return await ui.say(interaction, f"❌ Could not post the giveaway: `{exc.text or exc}`")
        await self.bot.db.set_giveaway_message(giveaway_id, message.id)
        await ui.say(
            interaction,
            f"🎉 **{giveaway_number(giveaway_id)}** started in {target.mention}\n"
            f"{ui._field('Prize', prize)}\n{ui._field('Winners', str(winners))}\n"
            f"{ui._field('Ends', f'<t:{_epoch(ends)}:R>')}",
        )

    @app_commands.command(name="end", description="End a giveaway now and draw the winners")
    @app_commands.describe(giveaway_id="Giveaway number (the GA-… id)")
    async def end_cmd(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        giveaway = await self.bot.db.get_giveaway(giveaway_id)
        if giveaway is None:
            return await ui.say(interaction, "❌ Giveaway not found.")
        if giveaway["status"] != "open":
            return await ui.say(interaction, "❌ That giveaway has already ended.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        winners = await _finish(self.bot, giveaway, interaction.user.id)
        if winners is None:
            return await ui.say(interaction, "❌ It was already being ended — nothing changed.")
        names = ", ".join(f"<@{user_id}>" for user_id in winners) or "no valid entries"
        await ui.say(interaction, f"🎉 **{giveaway_number(giveaway_id)}** ended · winners: {names}")

    @app_commands.command(name="reroll", description="Draw a replacement winner for an ended giveaway")
    @app_commands.describe(giveaway_id="Giveaway number (the GA-… id)")
    async def reroll(self, interaction: discord.Interaction, giveaway_id: int) -> None:
        giveaway = await self.bot.db.get_giveaway(giveaway_id)
        if giveaway is None:
            return await ui.say(interaction, "❌ Giveaway not found.")
        if giveaway["status"] == "open":
            return await ui.say(interaction, "❌ End the giveaway first, then reroll.")
        previous = [row["user_id"] for row in await self.bot.db.giveaway_winners(giveaway_id)]
        entries = await self.bot.db.giveaway_entries(giveaway_id)
        picks = _pick(entries, 1, exclude=previous)
        if not picks:
            return await ui.say(interaction, "❌ Every entrant has already won — nobody left to reroll.")
        await self.bot.db.add_giveaway_winner(interaction.user.id, giveaway_id, picks[0])
        channel = await _channel(self.bot, giveaway.get("channel_id"))
        if channel is not None:
            try:
                await channel.send(
                    content=f"🎲 Reroll for **{giveaway['prize']}** — congratulations <@{picks[0]}>!",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                log.warning("Could not announce the reroll for giveaway %s", giveaway_id)
        await ui.say(interaction, f"🎲 New winner for **{giveaway_number(giveaway_id)}**: <@{picks[0]}>")

    # ------------------------------------------------------------------ auto draw
    @tasks.loop(seconds=30)
    async def watcher(self) -> None:
        try:
            now = time.time()
            for giveaway in await self.bot.db.open_giveaways():
                if _epoch(giveaway["ends_at"]) <= now:
                    actor = self.bot.user.id if self.bot.user else 0
                    await _finish(self.bot, giveaway, actor)
        except Exception:
            log.exception("Giveaway watcher failed")

    @watcher.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Giveaways(bot))
