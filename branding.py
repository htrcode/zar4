"""/branding: change a company's logo (invoice header) and signature (invoice footer) from Discord.

Uploads are stored next to the database (``data/branding/<company>/logo.png`` / ``signature.png``) and
picked up by the next PDF that is generated. A company without an upload keeps using the bundled
``logo.png`` / ``signature.png`` (or ``LOGO_PATH`` / ``SIGNATURE_PATH``). Staff only.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import ui
from desk.utils import (branding_file, clear_branding, company_choices, require_company, save_branding,
                        staff_interaction_check)

log = logging.getLogger("invoice-bot.branding")

MAX_BYTES = 5 * 1024 * 1024
KIND_CHOICES = [app_commands.Choice(name="Logo (top of the invoice)", value="logo"),
                app_commands.Choice(name="Signature (bottom of the invoice)", value="signature")]
LABEL = {"logo": "Logo", "signature": "Signature"}


@app_commands.guild_only()
class Branding(commands.GroupCog, group_name="branding",
               group_description="Company logo and signature on invoices"):
    def __init__(self, bot) -> None:
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    @app_commands.command(name="set", description="Upload a company's invoice logo or signature")
    @app_commands.describe(company="Company the image belongs to", kind="Which image to replace",
                           image="PNG / JPG / WEBP. A transparent PNG looks best")
    @app_commands.choices(company=company_choices(), kind=KIND_CHOICES)
    async def set_image(self, interaction: discord.Interaction, company: app_commands.Choice[str],
                        kind: app_commands.Choice[str], image: discord.Attachment) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if image.size > MAX_BYTES:
            return await ui.say(interaction, "❌ File too big\nKeep it under 5 MB.")
        try:
            data = await image.read()
            path = await asyncio.to_thread(save_branding, company.value, kind.value, data)
        except ValueError as exc:
            return await ui.say(interaction, f"❌ Not saved\n{exc}")
        except Exception:
            log.exception("Could not save branding")
            return await ui.say(interaction, "❌ Not saved\nI could not write the file (it was logged).")
        name = require_company(company.value).name
        card = ui.notice(f"{LABEL[kind.value]} updated · {name}",
                         f"New invoice PDFs for **{name}** use this {kind.value}. PDFs already sent are not changed.\n"
                         "-# Not right? Upload again, or `/branding reset` for the default.")
        await interaction.followup.send(view=card, file=discord.File(path, filename=f"{company.value}-{kind.value}.png"),
                                        ephemeral=True)

    @app_commands.command(name="reset", description="Go back to the default logo or signature for a company")
    @app_commands.describe(company="Company to reset", kind="Which image to reset")
    @app_commands.choices(company=company_choices(), kind=KIND_CHOICES)
    async def reset_image(self, interaction: discord.Interaction, company: app_commands.Choice[str],
                          kind: app_commands.Choice[str]) -> None:
        name = require_company(company.value).name
        removed = clear_branding(company.value, kind.value)
        await ui.say(interaction, (f"✅ {LABEL[kind.value]} reset · {name}\nInvoices use the default {kind.value} again."
                                   if removed else
                                   f"ℹ️ Nothing to reset\n**{name}** already uses the default {kind.value}."))

    @app_commands.command(name="show", description="See which logo and signature each company's invoices use")
    async def show_images(self, interaction: discord.Interaction) -> None:
        lines = [f"**{c.name}**: logo {'custom' if branding_file(c.key, 'logo') else 'default'} · "
                 f"signature {'custom' if branding_file(c.key, 'signature') else 'default'}"
                 for c in config.COMPANIES.values()]
        await interaction.response.send_message(
            view=ui.build_card("Invoice branding", "\n".join(lines), timeout=180), ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Branding(bot))
