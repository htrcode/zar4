"""Live USD prices on pricing cards.

Cards posted with ``/embed`` that contain INR prices show the USD equivalent next to each one
(``₹999 / month`` -> ``₹999 / $10.64 per month``). The bot remembers the card (channel + message id and
its original INR text), so a restart loses nothing, and a single background task re-checks the exchange
rate every 30-60 minutes and edits only the cards whose USD figures actually changed.

``/price`` (staff only): ``status``, ``refresh``, ``track`` (adopt a card posted before this feature),
``untrack``. Settings: ``FX_ENABLED``, ``FX_REFRESH_MINUTES``, ``FX_API_URLS`` (see config.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional, Union
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import ui
from cogs.embed import announcement_card
from desk.fx import (FORCED_COOLDOWN, TEXT_LIMIT, RateKeeper, card_fingerprint, card_text_length, convert_text,
                     has_inr_price, refresh_minutes)
from desk.utils import staff_interaction_check

log = logging.getLogger("invoice-bot.pricing")

Channel = Union[discord.TextChannel, discord.Thread]
EDIT_GAP = 0.4  # seconds between card edits, so a rate change never bursts against Discord's rate limit
LINK = re.compile(r"/channels/(?P<guild>\d+|@me)/(?P<channel>\d+)/(?P<message>\d+)")


def card_parts(components: list) -> Optional[tuple[str, str, str]]:
    """(title, body, footer) of a card built by :func:`cogs.embed.announcement_card`, from its component data."""
    blocks: list[str] = []
    for top in components:
        if isinstance(top, dict) and top.get("type") == 17:  # the card's container
            blocks = [child.get("content", "") for child in top.get("components", []) if child.get("type") == 10]
    if len(blocks) < 2 or not blocks[0].startswith("## "):
        return None
    title = blocks[0][3:].split("\n", 1)[0]  # (a "-# tagline" line under the title is added again when posting)
    rest = blocks[1:]
    footer = rest.pop()[3:] if len(rest) >= 2 and rest[-1].startswith("-# ") else ""
    return title, "\n\n".join(rest), footer


class Pricing(commands.GroupCog, group_name="price", group_description="USD prices on pricing cards"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.bot.pricing = self  # /embed asks this cog to convert the cards it posts
        super().__init__()
        self.rates = RateKeeper(bot.db)
        self._lock = asyncio.Lock()
        self._applied = None   # the rate the tracked cards were last brought up to date with
        self._dirty = False    # an edit failed temporarily: look at the cards again next time

    # ------------------------------------------------------------------ lifecycle
    async def cog_load(self) -> None:
        await self.rates.load()  # the last good rate is available straight away, even if the API is down
        if config.FX_ENABLED and not self.watcher.is_running():  # exactly one background task
            self.watcher.start()

    async def cog_unload(self) -> None:
        self.watcher.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    @tasks.loop(minutes=refresh_minutes())
    async def watcher(self) -> None:
        try:
            await self.cycle()
        except Exception:
            log.exception("USD price update failed")

    @watcher.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ converting cards
    def prepare(self, title: str, body: str, footer: str = "") -> tuple[str, str, str, str]:
        """(title, body, footer, state) with the USD prices added.

        state: ``none`` (no INR price, or the feature is off), ``waiting`` (INR prices but no rate yet:
        posted as is, converted as soon as a rate exists), ``too_long`` (USD would not fit in one message),
        ``converted``.
        """
        if not config.FX_ENABLED or not any(has_inr_price(text) for text in (title, body, footer)):
            return title, body, footer, "none"
        rate = self.rates.rate
        if rate is None:
            return title, body, footer, "waiting"
        shown = tuple(convert_text(text, rate) for text in (title, body, footer))
        if card_text_length(*shown) > TEXT_LIMIT:
            return title, body, footer, "too_long"
        return (*shown, "converted")

    async def track(self, message: discord.Message, title: str, body: str, footer: str, user_id: int,
                    shown: tuple[str, str, str]) -> Optional[int]:
        """Keep this card up to date from now on. Stores the ORIGINAL text, never the converted one."""
        return await self.bot.db.price_card_add(
            guild_id=getattr(message.guild, "id", None), channel_id=message.channel.id, message_id=message.id,
            title=title, body=body, footer=footer, shown_hash=card_fingerprint(*shown), created_by=user_id)

    async def _edit(self, card: dict, title: str, body: str, footer: str) -> str:
        """ok / missing (message or channel is gone) / error (try again later)."""
        try:
            channel = self.bot.get_channel(card["channel_id"]) or await self.bot.fetch_channel(card["channel_id"])
            await channel.get_partial_message(card["message_id"]).edit(view=announcement_card(title, body, footer))
            return "ok"
        except discord.NotFound:
            return "missing"
        except discord.HTTPException as exc:  # Forbidden, rate limit, Discord outage ...
            log.warning("Could not edit price card %s: %s", card["message_id"], exc)
            return "error"

    async def update_cards(self) -> int:
        """Bring every tracked card up to the current rate. A card is edited only if what it shows changes."""
        rate = self.rates.rate
        if rate is None:
            return 0
        self._dirty, edited = False, 0
        for card in await self.bot.db.price_cards("active"):
            title, body, footer, state = self.prepare(card["title"], card["body"], card["footer"])
            if state != "converted":
                continue
            fingerprint = card_fingerprint(title, body, footer)
            if fingerprint == card["shown_hash"]:
                continue  # same figures as before: no Discord call at all
            outcome = await self._edit(card, title, body, footer)
            if outcome == "ok":
                await self.bot.db.price_card_set(card["id"], shown_hash=fingerprint)
                edited += 1
            elif outcome == "missing":
                await self.bot.db.price_card_set(card["id"], status="missing")  # deleted: stop touching it
            else:
                self._dirty = True
            await asyncio.sleep(EDIT_GAP)
        self._applied = rate
        return edited

    async def cycle(self, force: bool = False) -> tuple[str, int]:
        """One background pass: check the rate (only calls the feed when a new rate can exist), then the cards."""
        async with self._lock:
            outcome = await self.rates.refresh(force=force)
            edited = 0
            if self.rates.rate is not None and (self._applied != self.rates.rate or self._dirty):
                edited = await self.update_cards()
            return outcome, edited

    # ------------------------------------------------------------------ commands
    def _locate(self, interaction: discord.Interaction, text: str, channel: Optional[Channel]):
        """(channel id, message id) from a message link, or a bare message id plus a channel."""
        found = LINK.search(text or "")
        if found:
            return int(found["channel"]), int(found["message"])
        if (text or "").strip().isdigit():
            return (channel or interaction.channel).id, int((text or "").strip())
        return None

    @app_commands.command(name="status", description="Show the USD/INR rate and the cards that follow it")
    async def status(self, interaction: discord.Interaction) -> None:
        keeper = self.rates
        if not config.FX_ENABLED:
            return await ui.say(interaction, "ℹ️ USD prices are switched off\nSet `FX_ENABLED` back on in `config.py`.")
        cards = await self.bot.db.price_cards(None, interaction.guild_id)
        lines = [
            ui._field("Rate", f"1 USD = ₹{keeper.rate:.2f}" if keeper.rate else "not available yet"),
            ui._field("Updated", ui._when(keeper.updated_at) if keeper.updated_at else "—"),
            ui._field("Source", urlparse(keeper.source).netloc or "—"),
            ui._field("Checks", f"every {refresh_minutes()} min (the feed is only called when a new rate can exist)"),
        ]
        if "er-api" in keeper.source:
            lines.append("-# Rates By Exchange Rate API · exchangerate-api.com")
        shown = []
        for card in cards[:10]:
            link = f"https://discord.com/channels/{card['guild_id'] or '@me'}/{card['channel_id']}/{card['message_id']}"
            shown.append(f"• [{card['title'][:40]}]({link}) · {card['status']}")
        extra = f"\n…and {len(cards) - 10} more" if len(cards) > 10 else ""
        blocks = ["\n".join(lines), f"**Tracked cards** ({sum(c['status'] == 'active' for c in cards)} active)\n"
                  + ("\n".join(shown) + extra if shown else "None yet. Post prices with `/embed`, or adopt an "
                                                          "existing card with `/price track`.")]
        await interaction.response.send_message(view=ui.build_card("💱 USD prices", *blocks, timeout=180),
                                                ephemeral=True)

    @app_commands.command(name="refresh", description="Check the exchange rate now and update the cards")
    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not config.FX_ENABLED:
            return await ui.say(interaction, "ℹ️ USD prices are switched off")
        outcome, edited = await self.cycle(force=True)
        keeper = self.rates
        if outcome == "cooldown":
            return await ui.say(interaction, f"ℹ️ Checked a moment ago\nManual checks are limited to one every "
                                             f"{FORCED_COOLDOWN // 60} minutes.")
        if keeper.rate is None:
            return await ui.say(interaction, "❌ No exchange rate yet\nNone of the feeds answered. The bot keeps "
                                             "trying every few minutes; prices stay INR-only until then.")
        note = "" if outcome != "failed" else "\nThe feeds could not be reached, so the last good rate is kept."
        await ui.say(interaction, f"✅ 1 USD = ₹{keeper.rate:.2f}\n{edited} card(s) edited.{note}")

    @app_commands.command(name="track", description="Keep a pricing card posted earlier up to date with USD prices")
    @app_commands.describe(message="Message link (or message id) of a card posted by this bot",
                           channel="Channel of the card when you give a message id")
    async def track_cmd(self, interaction: discord.Interaction, message: str,
                        channel: Optional[Channel] = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        where = self._locate(interaction, message, channel)
        if where is None:
            return await ui.say(interaction, "❌ I need a message link, or a message id plus the channel.")
        existing = await self.bot.db.price_card_get(*where)
        if existing:
            await self.bot.db.price_card_set(existing["id"], status="active", shown_hash="")
            await self.cycle()
            return await ui.say(interaction, "✅ Tracking it\nThat card follows the exchange rate (again).")
        try:
            target = self.bot.get_channel(where[0]) or await self.bot.fetch_channel(where[0])
            found = await target.fetch_message(where[1])
        except discord.HTTPException:
            return await ui.say(interaction, "❌ I could not read that message. Check the link and my access.")
        if self.bot.user is None or found.author.id != self.bot.user.id:
            return await ui.say(interaction, "❌ That message was not posted by me, so I cannot edit it.")
        parts = card_parts([component.to_dict() for component in found.components])
        if parts is None:
            return await ui.say(interaction, "❌ That is not a card I can read (only `/embed` cards work).")
        title, body, footer = parts
        shown_title, shown_body, shown_footer, state = self.prepare(title, body, footer)
        if state == "none":
            return await ui.say(interaction, "❌ No INR prices found\nWrite prices like `₹999 / month` in the card.")
        if state == "too_long":
            return await ui.say(interaction, "❌ Too long\nThe USD prices would not fit in one message.")
        card_id = await self.bot.db.price_card_add(
            guild_id=getattr(found.guild, "id", None), channel_id=where[0], message_id=where[1], title=title,
            body=body, footer=footer, shown_hash=card_fingerprint(title, body, footer), created_by=interaction.user.id)
        if card_id is None:
            return await ui.say(interaction, "ℹ️ It was just added by someone else.")
        if state == "converted":
            outcome = await self._edit({"channel_id": where[0], "message_id": where[1]},
                                       shown_title, shown_body, shown_footer)
            if outcome == "ok":
                await self.bot.db.price_card_set(card_id, shown_hash=card_fingerprint(
                    shown_title, shown_body, shown_footer))
                return await ui.say(interaction, "✅ Tracking it\nUSD prices were added and will follow the rate.")
            self._dirty = True
            return await ui.say(interaction, "⚠️ Tracking it, but I could not edit the message yet\n"
                                             "Check my access to that channel; I will retry automatically.")
        await ui.say(interaction, "✅ Tracking it\nNo exchange rate is available yet. USD prices will be added as "
                                  "soon as one is.")

    @app_commands.command(name="untrack", description="Stop updating a pricing card and put it back to INR only")
    @app_commands.describe(message="Message link (or message id) of the card",
                           channel="Channel of the card when you give a message id")
    async def untrack_cmd(self, interaction: discord.Interaction, message: str,
                          channel: Optional[Channel] = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        where = self._locate(interaction, message, channel)
        card = await self.bot.db.price_card_get(*where) if where else None
        if not card or card["status"] == "stopped":
            return await ui.say(interaction, "❌ Not tracked\nThat message is not one of the tracked cards.")
        await self.bot.db.price_card_set(card["id"], status="stopped")
        outcome = await self._edit(card, card["title"], card["body"], card["footer"])  # back to the INR-only original
        note = "" if outcome == "ok" else "\nI could not restore the message text; it may still show the last USD prices."
        await ui.say(interaction, f"✅ Stopped\nThe card is back to its original INR prices and no longer changes.{note}")


async def setup(bot) -> None:
    await bot.add_cog(Pricing(bot))
