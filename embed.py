"""Free-form announcement cards (`/embed`) in the bot's Components V2 design.

Discord's Components V2 has no embeds: every message here is a LayoutView card
(``## title``, divider, body, divider, ``-# footer``) built with :mod:`ui`, so an
announcement looks exactly like the rest of the bot's messages.

Flow: ``/embed channel:#news title:"..." footer:"..."`` opens a form for the
message body (title and footer are pre-filled and can still be edited), and the
card is posted only to the channel that was chosen.
"""

from __future__ import annotations

from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import ui
from desk.utils import is_ticket_staff, staff_interaction_check

Channel = Union[discord.TextChannel, discord.Thread]

TITLE_LIMIT = 256
BODY_LIMIT = 3500
FOOTER_LIMIT = 200


def announcement_card(title: str, body: str, footer: str = "") -> ui.PersistentView:
    """A Components V2 card: title, message body and an optional footer line."""
    return ui.build_card(title.strip(), body.strip(), footer=(footer or "").strip() or None, timeout=None)


class EmbedModal(discord.ui.Modal):
    """The form: message body, plus title / footer pre-filled from the command."""

    message_body = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="What should the card say? **Markdown** works.",
        max_length=BODY_LIMIT,
    )
    heading = discord.ui.TextInput(label="Title", max_length=TITLE_LIMIT)
    footer_text = discord.ui.TextInput(label="Footer", required=False, max_length=FOOTER_LIMIT)

    def __init__(self, channel: Channel, title: str = "", footer: str = "") -> None:
        super().__init__(title=f"Post to #{channel.name}"[:45])
        self.channel = channel
        if title:
            self.heading.default = title[:TITLE_LIMIT]
        if footer:
            self.footer_text.default = footer[:FOOTER_LIMIT]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.heading.value).strip()
        body = str(self.message_body.value).strip()
        footer = str(self.footer_text.value or "").strip()
        if not title or not body:
            await interaction.response.send_message(
                view=ui.notice("Missing content", "A title and a message are both required.", error=True),
                ephemeral=True,
            )
            return

        me = getattr(self.channel.guild, "me", None)
        if me is not None and not self.channel.permissions_for(me).send_messages:
            await interaction.response.send_message(
                view=ui.notice(
                    "Missing permission",
                    f"I need **View Channel** and **Send Messages** in {self.channel.mention}.",
                    error=True,
                ),
                ephemeral=True,
            )
            return

        try:
            await self.channel.send(view=announcement_card(title, body, footer))
        except discord.Forbidden:
            await interaction.response.send_message(
                view=ui.notice(
                    "Missing permission",
                    f"I could not post in {self.channel.mention} — check my **Send Messages** permission there.",
                    error=True,
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                view=ui.notice("Could not post", f"Discord rejected the message: `{exc.text or exc}`",
                               error=True),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            view=ui.notice("Announcement sent", f"Posted to {self.channel.mention}."), ephemeral=True
        )


class Embeds(commands.Cog):
    """`/embed` — post a formatted announcement card anywhere."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    @app_commands.command(name="embed", description="Post a formatted announcement card in a channel")
    @app_commands.describe(
        channel="Channel the card is posted in",
        title="Card title (can still be edited in the form)",
        footer="Small footer line (optional, can be edited in the form)",
    )
    @app_commands.guild_only()
    async def embed(
        self,
        interaction: discord.Interaction,
        channel: Channel,
        title: Optional[str] = None,
        footer: Optional[str] = None,
    ) -> None:
        if not is_ticket_staff(interaction.user):
            return await ui.say(interaction, "🚫 You need a staff role to post announcements.")
        me = getattr(interaction.guild, "me", None)
        if me is not None and not channel.permissions_for(me).send_messages:
            return await ui.say(
                interaction,
                f"❌ I need **View Channel** and **Send Messages** in {channel.mention}.",
            )
        await interaction.response.send_modal(EmbedModal(channel, title or "", footer or ""))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Embeds(bot))
