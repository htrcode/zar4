"""Invoice, ticket and sales bot (Discord Components V2).

Run with ``python bot.py`` (or ``python app.py`` on panels whose default file is app.py).

Everything is configured in ``config.py`` — no .env file needed. Environment
variables, if you set any, override the values written there.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    raise SystemExit(
        f"Python 3.10+ is required (this server runs {sys.version.split()[0]}). "
        "In the Pterodactyl panel open Startup and pick a newer Python version/image."
    )

BASE_DIR = Path(__file__).resolve().parent
_DEPS = BASE_DIR / ".deps"

# Packages this bot needs. If the panel did not install requirements.txt, install
# whatever is missing next to the bot so it still starts.
_REQUIRED = {
    "discord": "discord.py>=2.6,<3",
    "aiosqlite": "aiosqlite>=0.20",
    "reportlab": "reportlab>=4.0,<5",
    "tzdata": "tzdata",
}


def _ensure_dependencies() -> None:
    if _DEPS.exists() and str(_DEPS) not in sys.path:
        sys.path.insert(0, str(_DEPS))
    missing = [pkg for mod, pkg in _REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"Installing missing packages: {', '.join(missing)} ...", flush=True)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet",
             "--target", str(_DEPS), *missing]
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SystemExit(
            f"Could not install {', '.join(missing)} automatically ({exc}).\n"
            "Add them to the panel's requirements file (see requirements.txt) and restart."
        )
    sys.path.insert(0, str(_DEPS))
    importlib.invalidate_caches()


_ensure_dependencies()

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.ext import commands  # noqa: E402

import config  # noqa: E402
import ui  # noqa: E402
from desk.bridge import post_transaction_event  # noqa: E402
from desk.database import Database  # noqa: E402
from desk.views import InvoiceView, TicketOpenPanelView  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("invoice_bot")


class _NoVoiceNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "voice will NOT be supported" not in record.getMessage()


logging.getLogger("discord.client").addFilter(_NoVoiceNoise())

# View Channels + Send Messages + Send Messages in Threads + Attach Files + Embed Links
# + Read Message History + Manage Channels (tickets) + Manage Messages (transcripts)
INVITE_PERMISSIONS = 1024 + 2048 + 274877906944 + 32768 + 16384 + 65536 + 8192 + 64 + 16

EXTENSIONS = (
    "desk.invoices",  # /invoice ... (quick + itemised invoices, PDFs, delivery)
    "desk.finance",   # /finance ... (income, expenses, dashboard, P&L)
    "desk.tickets",   # /ticket ...
    "desk.sales",     # /sales ...
    "cogs.finance",   # /sale, /expense, /revenue, /statement (company statements)
    "cogs.embed",     # /embed (announcement cards)
    "cogs.giveaways",  # /giveaway ... (button entries, auto draw, rerolls)
    "cogs.services",  # /service_add, /service_expiry, /service_info, /service_list, /service_cancel + daily reminders
)


class InvoiceTree(app_commands.CommandTree):
    """Command tree that always answers the user when a command fails."""

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure) and not isinstance(
            error, app_commands.MissingPermissions
        ):
            return  # the staff check already replied
        if isinstance(error, app_commands.CommandNotFound):
            # Discord still lists a command this bot no longer has (stale registration).
            title = "Outdated command"
            body = (
                "That command no longer exists. Type `/` and pick one of the current commands. "
                "If you still see old ones, restart Discord (Ctrl+R) to refresh the list."
            )
        elif isinstance(error, app_commands.MissingPermissions):
            title, body = "No permission", "You do not have permission to use that command."
        else:
            log.error("Command %r failed", getattr(interaction.command, "name", None), exc_info=error)
            title = "Something went wrong"
            body = f"`{type(error).__name__}` — the error was logged. Please try again."

        view = ui.notice(title, body, error=True)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=view, ephemeral=True)
            else:
                await interaction.response.send_message(view=view, ephemeral=True)
        except discord.HTTPException:
            pass


class InvoiceBot(commands.Bot):
    def __init__(self, message_content: bool = True) -> None:
        intents = discord.Intents.default()
        # Lets ticket transcripts contain message text (privileged intent).
        intents.message_content = message_content
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            tree_cls=InvoiceTree,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.message_content = message_content
        self.db = Database(config.DB_PATH)
        self._cleaned_guilds = False

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.db.subscribe(self._on_database_event)
        ui.set_tagline(config.BRAND_TAGLINE)

        # The quick-invoice "Mark as paid" button lives on messages posted in the
        # past, so it is registered globally rather than per-message.
        self.add_dynamic_items(ui.MarkPaidButton)

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded extension %s", extension)

        # Re-attach buttons on old invoice messages so they keep working after a restart.
        for row in await self.db.fetchall("SELECT id FROM invoices"):
            self.add_view(InvoiceView(self, row["id"]))
        self.add_view(TicketOpenPanelView(self))

        await self._sync_commands()

    async def _on_database_event(self, event: str, payload: dict) -> None:
        """Invoice / payment events become cards in the invoice / statements channels."""
        await post_transaction_event(self, event, payload)

    def invite_url(self) -> str:
        return (
            f"https://discord.com/oauth2/authorize?client_id={self.application_id or '{CLIENT_ID}'}"
            f"&scope=bot%20applications.commands&permissions={INVITE_PERMISSIONS}"
        )

    async def _sync_commands(self) -> None:
        """Publish this bot's commands and remove every stale one.

        A bot token keeps whatever commands any earlier version registered (global
        *and* per-server). Those no longer exist in the code, so Discord shows them
        but they fail with "Application command not found". Syncing over them
        replaces the lists.
        """
        names = sorted(c.qualified_name for c in self.tree.walk_commands())
        synced_any = False
        for guild_id in config.GUILD_IDS:
            guild = discord.Object(id=guild_id)
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                log.error(
                    "Missing Access for server %s. Check GUILD_ID, make sure the bot is IN that "
                    "server and re-invite it with the 'applications.commands' scope: %s",
                    guild_id, self.invite_url(),
                )
            except discord.HTTPException as exc:
                log.error("Could not sync commands to server %s: %s", guild_id, exc)
            else:
                synced_any = True
                print(f"Synced {len(synced)} commands to server {guild_id}", flush=True)

        if synced_any:
            # Commands now live in the server(s); wipe the global copies / stale ones.
            self.tree.clear_commands(guild=None)
            try:
                await self.tree.sync()
            except discord.HTTPException as exc:
                log.error("Could not clear old global commands: %s", exc)
            return

        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global commands (can take a few minutes to appear): "
                  f"{', '.join(names)}", flush=True)
        except discord.HTTPException as exc:
            log.error("Global command sync failed: %s", exc)

    async def _clear_stale_guild_commands(self) -> None:
        """Remove old per-server commands left in every other server."""
        keep = set(config.GUILD_IDS)
        for guild in self.guilds:
            if guild.id in keep:
                continue
            try:
                # Nothing is registered for this guild in the tree, so this sends an
                # empty list and deletes whatever was left there.
                await self.tree.sync(guild=guild)
            except discord.HTTPException as exc:
                log.warning("Could not clean commands in %s (%s): %s", guild.name, guild.id, exc)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        if not self._cleaned_guilds:
            self._cleaned_guilds = True
            await self._clear_stale_guild_commands()
        missing = [g for g in config.GUILD_IDS if self.get_guild(g) is None]
        if missing:
            log.warning("The bot is not in server(s) %s. Invite it with: %s", missing, self.invite_url())
        if not self.message_content:
            log.warning("Message Content intent is off: ticket transcripts will not include message text.")
        if config.INVOICE_CHANNEL_ID is None:
            log.warning("INVOICE_CHANNEL_ID is not set — new invoices have nowhere to go.")
        if config.STATEMENTS_CHANNEL_ID is None:
            log.warning("STATEMENTS_CHANNEL_ID is not set — Sales / Expense Statements have nowhere to go.")
        for company in config.COMPANIES.values():
            if not company.statement_channel_id:
                log.warning("%s is not set — %s cannot record sales, expenses, revenue or invoices until "
                            "that company has a statement channel.", company.statement_env, company.name)
        service = getattr(self, "tickets", None)
        for guild in self.guilds:
            cfg = config.tickets_for(guild)
            if not cfg.configured:
                if config.TICKET_GUILDS:
                    log.warning("Ticket system: server %s (%s) is not in ticket_guilds.json, so tickets "
                                "are disabled there.", guild.name, guild.id)
                continue
            for issue in (service.config_issues(guild, cfg) if service else []):
                log.warning("Ticket setup · %s: %s", guild.name, issue)
        if not config.TICKET_GUILDS and not config.TICKET_STAFF_ROLE_IDS:
            log.warning("STAFF_ROLE_IDS / TICKET_STAFF_ROLE_IDS are empty: only administrators can use staff commands.")
        if not config.SALES_CHANNEL_ID:
            log.warning("SALES_CHANNEL_ID is not set: follow-up reminders cannot be posted.")

    async def close(self) -> None:
        await self.db.close()
        await super().close()


def main() -> None:
    if not config.TOKEN:
        sys.exit(
            "No bot token set. Open config.py and paste your token into BOT_TOKEN "
            "(or set DISCORD_BOT_TOKEN on the panel)."
        )
    use_content = config.ENABLE_MESSAGE_CONTENT
    while True:
        bot = InvoiceBot(message_content=use_content)
        try:
            bot.run(config.TOKEN, log_handler=None)  # logging is configured above
            return
        except discord.PrivilegedIntentsRequired:
            if not use_content:
                sys.exit(
                    "Discord refused the connection (privileged intents). Open "
                    "https://discord.com/developers/applications → your bot → Bot and check the intents."
                )
            print(
                "MESSAGE CONTENT INTENT is disabled in the Developer Portal — starting without it. "
                "Ticket transcripts will not include message text. Enable it (Bot → Privileged Gateway "
                "Intents) and restart to turn that on.",
                flush=True,
            )
            use_content = False
        except discord.LoginFailure:
            sys.exit("Discord rejected the bot token. Check BOT_TOKEN in config.py.")


if __name__ == "__main__":
    main()
