"""Configuration for the invoice / ticket / sales bot.

No .env file needed
-------------------
Fill in the values in the "EDIT THESE" block below (use the Pterodactyl file
manager to edit this file on the server). If you *do* set environment
variables / Startup variables on the panel, those take priority over the
values written here (same names, e.g. ``DISCORD_BOT_TOKEN``, ``GUILD_ID``).

Channel routing
---------------
``INVOICE_CHANNEL_ID``     new invoices are posted here (and nowhere else).
``STATEMENTS_CHANNEL_ID``  Sales Statements and Expense Statements are posted here.

Do not commit your real token to a public repository.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

BASE_DIR = Path(__file__).resolve().parent

# ==========================================================================
# EDIT THESE
# ==========================================================================
BOT_TOKEN = ""  # paste your bot token between the quotes

# Your server id(s). One id, or several separated by commas: "123,456".
# The bot must already be IN the server. Leave "" to sync commands globally.
GUILD_ID = ""

INVOICE_CHANNEL_ID = 0  # channel id where new invoices are posted
STATEMENTS_CHANNEL_ID = 0  # channel id for Sales / Expense Statements

# --- Companies (multi-company finance) ---------------------------------------
# Every sale, expense, revenue entry and invoice is stored against one of these
# three companies (key -> name), and its statement is posted only to that
# company's own channel. A company channel may live in another server; the bot
# must be a member there with View Channel + Send Messages.
#
# Fill in the channel ids here, or as panel / environment variables:
#   COMPANY_ZARBUNEX_STATEMENT_CHANNEL_ID, COMPANY_HECTORHOST_STATEMENT_CHANNEL_ID,
#   COMPANY_MATRIXMC_STATEMENT_CHANNEL_ID
#   COMPANY_ZARBUNEX_INVOICE_CHANNEL_ID, ... (optional: invoice cards; defaults
#   to the same company's statement channel)
#   COMPANY_ZARBUNEX_NAME, ... (optional display-name override)
# Leave an id at 0 until you have the real channel: the commands then refuse to
# save instead of posting one company's money into another company's channel.
COMPANY_CONFIG = {
    "zarbunex": {"name": "Zarbunex PVT LTD", "statement_channel_id": 0, "invoice_channel_id": 0},
    "hectorhost": {"name": "HectorHost", "statement_channel_id": 0, "invoice_channel_id": 0},
    "matrixmc": {"name": "MatrixMC", "statement_channel_id": 0, "invoice_channel_id": 0},
}

# Roles allowed to use the finance / invoice / ticket / sales commands
# (server administrators always can). Role ids separated by commas: "111,222"
STAFF_ROLE_IDS = ""

BRAND_TAGLINE = ""  # small line under every card title, e.g. "Hector Hosting"
UTC_OFFSET_HOURS = 5.5  # fallback offset for dates (5.5 = India / IST)
TIMEZONE = "Asia/Kolkata"  # used for due dates, follow-ups and reports
CURRENCY = "INR"  # INR shows ₹ with Indian digit grouping

# Optional extra channel ids (comma separated) that also receive every
# invoice / payment card. Usually leave empty.
TRANSACTION_CHANNEL_IDS = ""
LOG_CHANNEL_ID = 0  # overdue-invoice alerts and general logs (optional)

# --- Tickets -------------------------------------------------------------
# Category ids (Discord: User Settings > Advanced > Developer Mode, then right-click the
# category > Copy Category ID). Leave 0 for any category you do not have.
TICKET_DEFAULT_CATEGORY_ID = 0  # "Tickets" - every new ticket is created here (falls back to Billing if 0)
TICKET_CATEGORY_ID = 0  # "Billing" category (kept as before)
TICKET_SUPPORT_CATEGORY_ID = 0  # "Technical Support" category (also a Complete Purchase target)
TICKET_PARTNERSHIP_CATEGORY_ID = 0  # "Partnership" category (also a Complete Purchase target)
TICKET_RECRUITMENT_CATEGORY_ID = 0  # "Recruitment" category
TICKET_ACTIVE_CLIENTS_CATEGORY_ID = 0  # "Active Clients" category
TICKET_OUTDATED_CLIENTS_CATEGORY_ID = 0  # "Outdated Clients" category
TICKET_FOLLOWUP_CATEGORY_ID = 0  # "Follow Up" category
TICKET_CLOSED_CATEGORY_ID = 0  # closed tickets are moved here (optional)
TICKET_STAFF_ROLE_IDS = ""  # roles that see every ticket (falls back to STAFF_ROLE_IDS)

# --- Department staff roles (one set per ticket category) ------------------
# Staff with a category's role(s) can see and manage that category's tickets, and
# access is handed over when a ticket is moved. One id, or several separated by
# commas: "111,222". Leave "" for a category your team does not split up: then
# TICKET_STAFF_ROLE_IDS (any ticket staff) cover it, exactly as before.
TICKET_DEFAULT_STAFF_ROLE_IDS = ""  # "Tickets" (the default category)
TICKET_BILLING_STAFF_ROLE_IDS = ""  # "Billing"
TICKET_SUPPORT_STAFF_ROLE_IDS = ""  # "Technical Support"
TICKET_PARTNERSHIP_STAFF_ROLE_IDS = ""  # "Partnership"
TICKET_RECRUITMENT_STAFF_ROLE_IDS = ""  # "Recruitment"
TICKET_ACTIVE_CLIENTS_STAFF_ROLE_IDS = ""  # "Active Clients"
TICKET_OUTDATED_CLIENTS_STAFF_ROLE_IDS = ""  # "Outdated Clients"
TICKET_FOLLOWUP_STAFF_ROLE_IDS = ""  # "Follow Up"
# Roles that may see and manage EVERY ticket whatever its department (managers,
# supervisors). They are never removed when a ticket moves. Administrators and
# the ticket owner always keep access too.
TICKET_CROSS_DEPARTMENT_ROLE_IDS = ""

TICKET_LOG_CHANNEL_ID = 0  # transcripts, close/reopen logs, stale-ticket digest
TICKET_PREFIX = "ticket"
TICKET_CLOSED_PREFIX = "closed"
TICKET_STALE_DAYS = 3  # tickets idle this long show up as stale
TICKET_MAX_OPEN_PER_USER = 3  # 0 = unlimited (staff are never limited)

# Public panel content. Each server can override these in ticket_guilds.json
# (panel_title / panel_description / panel_button).
TICKET_PANEL_TITLE = "Need Hosting? Create a Ticket"
TICKET_PANEL_DESCRIPTION = ("Create a ticket and our team will assist you. Our staff will identify your request "
                            "and route your ticket to the appropriate department.")
TICKET_PANEL_BUTTON = "Create Ticket"

# --- Sales pipeline ------------------------------------------------------
SALES_CHANNEL_ID = 0  # follow-up reminders (falls back to the ticket log / log channel)
FOLLOWUP_CHECK_MINUTES = 30
SALES_LOOKAHEAD_DAYS = 7

# --- Business details printed on invoice PDFs -----------------------------
BUSINESS_NAME = "My Hosting Company"
BUSINESS_TAGLINE = "Reliable hosting. Powerful solutions."
BUSINESS_EMAIL = "support@example.com"
BUSINESS_PHONE = "+91 00000 00000"
BUSINESS_ADDRESS = "City, State"
BUSINESS_TAX_ID = ""  # e.g. "GSTIN: 32ABCDE1234F1Z5"
PAYMENT_INSTRUCTIONS = "Pay via UPI: yourname@upi"
REFUND_POLICY = "Refunds are only available within 24 hours after the purchase has been made."
TERMS = "Payment is due by the due date shown|Services may be suspended for overdue invoices"
FOOTER_SERVICES = ""  # e.g. "web hosting, game server hosting and virtual machines"
BRAND_COLOR = "#6D28D9"  # accent colour on PDFs
INVOICE_PREFIX = "INV"

# Needed so ticket transcripts contain message text. Turn on
# "Message Content Intent" in the Discord Developer Portal (Bot tab).
# If it is not enabled the bot still starts, just without transcript text.
ENABLE_MESSAGE_CONTENT = True
# ==========================================================================


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _ids(name: str, fallback: Union[str, int, List[int], None]) -> List[int]:
    """List of ids: environment variable first, then the value written above."""
    raw = _env(name)
    if not raw:
        raw = fallback  # type: ignore[assignment]
    if raw in (None, "", 0):
        return []
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [int(x) for x in raw if int(x)]
    return [int(x) for x in str(raw).replace(";", ",").split(",") if x.strip().isdigit()]


def _one(name: str, fallback: Union[str, int, None]) -> Optional[int]:
    found = _ids(name, fallback)
    return found[0] if found else None


def _text(name: str, fallback: str) -> str:
    return (_env(name) or str(fallback)).strip()


def _number(name: str, fallback: float, cast=int):
    raw = _env(name)
    try:
        return cast(raw) if raw else cast(fallback)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}")


def _flag(name: str, fallback: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return bool(fallback)
    return raw not in ("0", "false", "no", "off")


def _path(value: str, *candidates: str) -> str:
    """First existing file among the configured value and the bundled defaults."""
    for candidate in (value, *candidates):
        if not candidate:
            continue
        p = Path(candidate)
        p = p if p.is_absolute() else BASE_DIR / p
        if p.exists():
            return str(p)
    return str(BASE_DIR / (candidates[-1] if candidates else value))


# ---- core -----------------------------------------------------------------
TOKEN: str = _text("DISCORD_BOT_TOKEN", "") or _text("DISCORD_TOKEN", "") or BOT_TOKEN.strip()
GUILD_IDS: List[int] = _ids("GUILD_ID", GUILD_ID)
GUILD_ID: Optional[int] = GUILD_IDS[0] if GUILD_IDS else None  # first server (kept for older code)
INVOICE_CHANNEL_ID: Optional[int] = _one("INVOICE_CHANNEL_ID", INVOICE_CHANNEL_ID)
STATEMENTS_CHANNEL_ID: Optional[int] = _one("STATEMENTS_CHANNEL_ID", STATEMENTS_CHANNEL_ID)
STAFF_ROLE_IDS: List[int] = _ids("STAFF_ROLE_IDS", STAFF_ROLE_IDS)
LOG_CHANNEL_ID: Optional[int] = _one("LOG_CHANNEL_ID", LOG_CHANNEL_ID)
TRANSACTION_CHANNEL_IDS: List[int] = _ids("TRANSACTION_CHANNEL_IDS", TRANSACTION_CHANNEL_IDS)

BRAND_TAGLINE: str = _text("BRAND_TAGLINE", BRAND_TAGLINE)
UTC_OFFSET_HOURS: float = _number("UTC_OFFSET_HOURS", UTC_OFFSET_HOURS, float)
TIMEZONE: str = _text("TIMEZONE", TIMEZONE)
CURRENCY: str = _text("CURRENCY", CURRENCY).upper()
ENABLE_MESSAGE_CONTENT: bool = _flag("ENABLE_MESSAGE_CONTENT", ENABLE_MESSAGE_CONTENT)

# ---- storage --------------------------------------------------------------
#: JSON ledger behind Sales / Expense Statements and quick invoices.
DATA_FILE: Path = Path(os.environ.get("INVOICE_BOT_DATA") or BASE_DIR / "data.json")
#: SQLite database behind itemised invoices, tickets, deals and P&L.
_db = Path(_env("DB_PATH") or "data/bot.db")
DB_PATH: str = str(_db if _db.is_absolute() else BASE_DIR / _db)

# ---- tickets --------------------------------------------------------------
TICKET_CATEGORY_ID: Optional[int] = _one("TICKET_CATEGORY_ID", TICKET_CATEGORY_ID)
TICKET_CLOSED_CATEGORY_ID: Optional[int] = _one("TICKET_CLOSED_CATEGORY_ID", TICKET_CLOSED_CATEGORY_ID)
TICKET_SUPPORT_CATEGORY_ID: Optional[int] = _one("TICKET_SUPPORT_CATEGORY_ID", TICKET_SUPPORT_CATEGORY_ID)
TICKET_PARTNERSHIP_CATEGORY_ID: Optional[int] = _one("TICKET_PARTNERSHIP_CATEGORY_ID", TICKET_PARTNERSHIP_CATEGORY_ID)
TICKET_DEFAULT_CATEGORY_ID: Optional[int] = _one("TICKET_DEFAULT_CATEGORY_ID", TICKET_DEFAULT_CATEGORY_ID)
TICKET_RECRUITMENT_CATEGORY_ID: Optional[int] = _one("TICKET_RECRUITMENT_CATEGORY_ID", TICKET_RECRUITMENT_CATEGORY_ID)
TICKET_ACTIVE_CLIENTS_CATEGORY_ID: Optional[int] = _one("TICKET_ACTIVE_CLIENTS_CATEGORY_ID", TICKET_ACTIVE_CLIENTS_CATEGORY_ID)
TICKET_OUTDATED_CLIENTS_CATEGORY_ID: Optional[int] = _one("TICKET_OUTDATED_CLIENTS_CATEGORY_ID", TICKET_OUTDATED_CLIENTS_CATEGORY_ID)
TICKET_FOLLOWUP_CATEGORY_ID: Optional[int] = _one("TICKET_FOLLOWUP_CATEGORY_ID", TICKET_FOLLOWUP_CATEGORY_ID)
# Every ticket category staff can move a ticket to: key -> (label, config variable, category id).
TICKET_CATEGORIES = {
    "tickets": ("Tickets", "TICKET_DEFAULT_CATEGORY_ID", TICKET_DEFAULT_CATEGORY_ID),
    "billing": ("Billing", "TICKET_CATEGORY_ID", TICKET_CATEGORY_ID),
    "support": ("Technical Support", "TICKET_SUPPORT_CATEGORY_ID", TICKET_SUPPORT_CATEGORY_ID),
    "partnership": ("Partnership", "TICKET_PARTNERSHIP_CATEGORY_ID", TICKET_PARTNERSHIP_CATEGORY_ID),
    "recruitment": ("Recruitment", "TICKET_RECRUITMENT_CATEGORY_ID", TICKET_RECRUITMENT_CATEGORY_ID),
    "active": ("Active Clients", "TICKET_ACTIVE_CLIENTS_CATEGORY_ID", TICKET_ACTIVE_CLIENTS_CATEGORY_ID),
    "outdated": ("Outdated Clients", "TICKET_OUTDATED_CLIENTS_CATEGORY_ID", TICKET_OUTDATED_CLIENTS_CATEGORY_ID),
    "followup": ("Follow Up", "TICKET_FOLLOWUP_CATEGORY_ID", TICKET_FOLLOWUP_CATEGORY_ID),
}
#: Ticket category key -> the variable that lists that department's staff roles.
TICKET_DEPARTMENT_ROLE_ENV: Dict[str, str] = {
    "tickets": "TICKET_DEFAULT_STAFF_ROLE_IDS",
    "billing": "TICKET_BILLING_STAFF_ROLE_IDS",
    "support": "TICKET_SUPPORT_STAFF_ROLE_IDS",
    "partnership": "TICKET_PARTNERSHIP_STAFF_ROLE_IDS",
    "recruitment": "TICKET_RECRUITMENT_STAFF_ROLE_IDS",
    "active": "TICKET_ACTIVE_CLIENTS_STAFF_ROLE_IDS",
    "outdated": "TICKET_OUTDATED_CLIENTS_STAFF_ROLE_IDS",
    "followup": "TICKET_FOLLOWUP_STAFF_ROLE_IDS",
}
#: Roles that may manage every ticket, whatever its department (never removed on a move).
TICKET_CROSS_DEPARTMENT_ROLE_IDS: List[int] = _ids("TICKET_CROSS_DEPARTMENT_ROLE_IDS",
                                                   TICKET_CROSS_DEPARTMENT_ROLE_IDS)
#: Department staff role ids per category key (parsed once, at import).
TICKET_DEPARTMENT_ROLE_IDS: Dict[str, List[int]] = {
    key: _ids(var, globals().get(var, "")) for key, var in TICKET_DEPARTMENT_ROLE_ENV.items()
}
#: Every role used by any department (used to clean up access when a ticket moves).
ALL_DEPARTMENT_ROLE_IDS: List[int] = sorted({rid for ids in TICKET_DEPARTMENT_ROLE_IDS.values()
                                             for rid in ids})
TICKET_STAFF_ROLE_IDS: List[int] = _ids("TICKET_STAFF_ROLE_IDS", TICKET_STAFF_ROLE_IDS) or STAFF_ROLE_IDS
TICKET_LOG_CHANNEL_ID: Optional[int] = _one("TICKET_LOG_CHANNEL_ID", TICKET_LOG_CHANNEL_ID) or LOG_CHANNEL_ID
TICKET_PREFIX: str = _text("TICKET_PREFIX", TICKET_PREFIX)
TICKET_CLOSED_PREFIX: str = _text("TICKET_CLOSED_PREFIX", TICKET_CLOSED_PREFIX)
TICKET_STALE_DAYS: int = _number("TICKET_STALE_DAYS", TICKET_STALE_DAYS)
TICKET_MAX_OPEN_PER_USER: int = _number("TICKET_MAX_OPEN_PER_USER", TICKET_MAX_OPEN_PER_USER)
TICKET_PANEL_TITLE: str = _text("TICKET_PANEL_TITLE", TICKET_PANEL_TITLE)
TICKET_PANEL_DESCRIPTION: str = _text("TICKET_PANEL_DESCRIPTION", TICKET_PANEL_DESCRIPTION)
TICKET_PANEL_BUTTON: str = _text("TICKET_PANEL_BUTTON", TICKET_PANEL_BUTTON)
_tdir = Path(_env("TICKET_TRANSCRIPT_DIR") or "data/transcripts")
TICKET_TRANSCRIPT_DIR: str = str(_tdir if _tdir.is_absolute() else BASE_DIR / _tdir)

# ---- per-guild ticket systems ---------------------------------------------
# Every Discord server can run its own ticket system: its own categories, staff
# roles, panel and log channels. The setup for each server lives in
# ``ticket_guilds.json`` next to this file (copy ``ticket_guilds.example.json``
# and fill in the ids). Servers that are not listed there cannot open or manage
# tickets, so a category or role id is never borrowed from another server.
#
# When the file does not exist, the flat ``TICKET_*`` values above are used for
# every server: that is the single-server behaviour this bot had before, so an
# existing installation keeps working without any change.
TICKET_GUILDS_FILE: Path = Path(_env("TICKET_GUILDS_FILE") or BASE_DIR / "ticket_guilds.json")
TICKET_CATEGORY_LABELS: Dict[str, str] = {key: entry[0] for key, entry in TICKET_CATEGORIES.items()}


def _as_ids(value) -> List[int]:
    """Ids from a JSON list, a single int, or a comma separated string."""
    if value in (None, "", 0):
        return []
    if isinstance(value, int):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        found = []
        for item in value:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number:
                found.append(number)
        return found
    return _ids("", value)


def _first_id(value) -> Optional[int]:
    found = _as_ids(value)
    return found[0] if found else None


@dataclass(frozen=True)
class GuildTickets:
    """One server's ticket setup (categories, staff roles and channels).

    Everything a ticket workflow needs is read from here for the guild the
    interaction happened in, so two servers can use totally different
    categories, roles and prefixes without touching each other.
    """

    guild_id: Optional[int]
    name: str
    categories: Dict[str, int]           # category key -> Discord category id
    department_roles: Dict[str, List[int]]
    staff_role_ids: tuple = ()
    cross_department_role_ids: tuple = ()
    log_channel_id: Optional[int] = None
    closed_category_id: Optional[int] = None
    panel_channel_id: Optional[int] = None
    panel_title: str = TICKET_PANEL_TITLE
    panel_description: str = TICKET_PANEL_DESCRIPTION
    panel_button: str = TICKET_PANEL_BUTTON
    prefix: str = "ticket"
    closed_prefix: str = "closed"
    stale_days: int = 3
    max_open_per_user: int = 3
    source: str = "config.py"

    @property
    def configured(self) -> bool:
        """True when this server has at least one ticket category."""
        return bool(self.categories)

    @property
    def all_department_role_ids(self) -> List[int]:
        return sorted({rid for ids in self.department_roles.values() for rid in ids})

    def category_id(self, key) -> Optional[int]:
        return self.categories.get(str(key or "").strip().lower())

    def category_key(self, category_id) -> Optional[str]:
        """Key of this server's category for a Discord category id (None when not ours)."""
        if not category_id:
            return None
        for key, cid in self.categories.items():
            if int(cid) == int(category_id):
                return key
        return None

    def label(self, key) -> str:
        return TICKET_CATEGORY_LABELS.get(str(key or "").strip().lower(), "Support")

    def roles(self, key) -> List[int]:
        return list(self.department_roles.get(str(key or "").strip().lower(), ()))

    def category_hint(self, key) -> str:
        """Where this server configures a category, for error messages."""
        key = str(key or "").strip().lower()
        if self.source == "ticket_guilds.json":
            return f'`categories.{key}` in `ticket_guilds.json` (server `{self.guild_id}`)'
        entry = TICKET_CATEGORIES.get(key)
        return f"`{entry[1]}` in config.py" if entry else "`ticket_guilds.json`"

    def roles_hint(self, key) -> str:
        key = str(key or "").strip().lower()
        if self.source == "ticket_guilds.json":
            return f'`department_roles.{key}` in `ticket_guilds.json` (server `{self.guild_id}`)'
        return f"`{TICKET_DEPARTMENT_ROLE_ENV.get(key, 'TICKET_STAFF_ROLE_IDS')}` in config.py"

    def can_manage(self, member, department: Optional[str] = None) -> bool:
        """Department-scoped staff check for THIS server only."""
        if member is None:
            return False
        permissions = getattr(member, "guild_permissions", None)
        if permissions is not None and (permissions.administrator or permissions.manage_channels):
            return True
        role_ids = {role.id for role in getattr(member, "roles", ()) or ()}
        if role_ids & set(self.staff_role_ids):
            return True
        if role_ids & set(self.cross_department_role_ids):
            return True
        if department:
            return bool(role_ids & set(self.roles(department)))
        # A ticket outside every configured department: any of this server's
        # department staff may help.
        return bool(role_ids & set(self.all_department_role_ids))

    def is_ticket_staff(self, member) -> bool:
        return self.can_manage(member)


def _flat_tickets(guild_id: Optional[int]) -> GuildTickets:
    """The single-server setup from the flat ``TICKET_*`` values above.

    Built fresh on every call so a reloaded or edited setting is picked up (and
    so tests can patch the module globals).
    """
    return GuildTickets(
        guild_id=guild_id,
        name="config.py (single server)",
        categories={key: cid for key, (_label, _var, cid) in TICKET_CATEGORIES.items() if cid},
        department_roles={key: list(ids) for key, ids in TICKET_DEPARTMENT_ROLE_IDS.items() if ids},
        staff_role_ids=tuple(sorted(set(TICKET_STAFF_ROLE_IDS) | set(STAFF_ROLE_IDS))),
        cross_department_role_ids=tuple(TICKET_CROSS_DEPARTMENT_ROLE_IDS),
        log_channel_id=TICKET_LOG_CHANNEL_ID,
        closed_category_id=TICKET_CLOSED_CATEGORY_ID,
        panel_title=TICKET_PANEL_TITLE,
        panel_description=TICKET_PANEL_DESCRIPTION,
        panel_button=TICKET_PANEL_BUTTON,
        prefix=TICKET_PREFIX,
        closed_prefix=TICKET_CLOSED_PREFIX,
        stale_days=TICKET_STALE_DAYS,
        max_open_per_user=TICKET_MAX_OPEN_PER_USER,
        source="config.py",
    )


def _parse_guild_tickets(guild_id: int, raw: dict) -> GuildTickets:
    if not isinstance(raw, dict):
        raise RuntimeError(f"ticket_guilds.json: the entry for server {guild_id} must be an object.")
    categories: Dict[str, int] = {}
    for key, value in (raw.get("categories") or {}).items():
        key = str(key).strip().lower()
        if key not in TICKET_CATEGORIES:
            continue  # unknown department: ignored, /ticket setup lists the valid keys
        cid = _first_id(value)
        if cid:
            categories[key] = cid
    department_roles: Dict[str, List[int]] = {}
    for key, value in (raw.get("department_roles") or {}).items():
        key = str(key).strip().lower()
        if key in TICKET_CATEGORIES:
            ids = _as_ids(value)
            if ids:
                department_roles[key] = ids
    try:
        stale_days = int(raw.get("stale_days", TICKET_STALE_DAYS))
        max_open = int(raw.get("max_open_per_user", TICKET_MAX_OPEN_PER_USER))
    except (TypeError, ValueError):
        raise RuntimeError(f"ticket_guilds.json: stale_days and max_open_per_user must be numbers "
                           f"(server {guild_id}).")
    return GuildTickets(
        guild_id=guild_id,
        name=str(raw.get("name") or f"server {guild_id}")[:80],
        categories=categories,
        department_roles=department_roles,
        staff_role_ids=tuple(_as_ids(raw.get("staff_role_ids"))),
        cross_department_role_ids=tuple(_as_ids(raw.get("cross_department_role_ids"))),
        log_channel_id=_first_id(raw.get("log_channel_id")),
        closed_category_id=_first_id(raw.get("closed_category_id")),
        panel_channel_id=_first_id(raw.get("panel_channel_id")),
        panel_title=str(raw.get("panel_title") or TICKET_PANEL_TITLE)[:256],
        panel_description=str(raw.get("panel_description") or TICKET_PANEL_DESCRIPTION)[:3500],
        panel_button=str(raw.get("panel_button") or TICKET_PANEL_BUTTON)[:80],
        prefix=str(raw.get("prefix") or TICKET_PREFIX)[:20],
        closed_prefix=str(raw.get("closed_prefix") or TICKET_CLOSED_PREFIX)[:20],
        stale_days=max(stale_days, 1),
        max_open_per_user=max(max_open, 0),
        source="ticket_guilds.json",
    )


def _load_ticket_guilds(path: Path) -> Dict[int, GuildTickets]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{path.name} could not be read ({exc}). Fix the JSON or remove the file.")
    if not isinstance(raw, dict):
        raise RuntimeError(f"{path.name} must be a JSON object keyed by Discord server id.")
    guilds: Dict[int, GuildTickets] = {}
    for key, value in raw.items():
        if str(key).startswith("_") or str(key).startswith("//"):
            continue  # comments
        try:
            guild_id = int(key)
        except (TypeError, ValueError):
            raise RuntimeError(f"{path.name}: {key!r} is not a Discord server id.")
        guilds[guild_id] = _parse_guild_tickets(guild_id, value)
    return guilds


#: Server id -> its ticket setup (empty when ``ticket_guilds.json`` is absent).
TICKET_GUILDS: Dict[int, GuildTickets] = _load_ticket_guilds(TICKET_GUILDS_FILE)


def tickets_for(guild) -> GuildTickets:
    """Ticket configuration for a server (a Guild, a guild id, or None).

    With ``ticket_guilds.json`` present only the servers listed there are
    configured; any other server gets an empty setup so nothing leaks across
    servers. Without the file the flat ``TICKET_*`` values apply everywhere.
    """
    guild_id = getattr(guild, "id", guild)
    try:
        guild_id = int(guild_id) if guild_id else None
    except (TypeError, ValueError):
        guild_id = None
    if guild_id and guild_id in TICKET_GUILDS:
        return TICKET_GUILDS[guild_id]
    if TICKET_GUILDS:
        return GuildTickets(guild_id=guild_id, name=f"unconfigured server {guild_id or '?'}",
                            categories={}, department_roles={}, staff_role_ids=(),
                            cross_department_role_ids=(), source="ticket_guilds.json")
    return _flat_tickets(guild_id)


def ticket_guild_ids() -> List[int]:
    """Servers that have a ticket setup of their own."""
    return sorted(TICKET_GUILDS)

# ---- sales pipeline -------------------------------------------------------
SALES_CHANNEL_ID: Optional[int] = _one("SALES_CHANNEL_ID", SALES_CHANNEL_ID) or TICKET_LOG_CHANNEL_ID or LOG_CHANNEL_ID
FOLLOWUP_CHECK_MINUTES: int = _number("FOLLOWUP_CHECK_MINUTES", FOLLOWUP_CHECK_MINUTES)
SALES_LOOKAHEAD_DAYS: int = _number("SALES_LOOKAHEAD_DAYS", SALES_LOOKAHEAD_DAYS)

# ---- invoice PDFs ---------------------------------------------------------
BUSINESS_NAME: str = _text("BUSINESS_NAME", BUSINESS_NAME)
BUSINESS_TAGLINE: str = _text("BUSINESS_TAGLINE", BUSINESS_TAGLINE)
BUSINESS_ADDRESS: str = _text("BUSINESS_ADDRESS", BUSINESS_ADDRESS)
BUSINESS_EMAIL: str = _text("BUSINESS_EMAIL", BUSINESS_EMAIL)
BUSINESS_PHONE: str = _text("BUSINESS_PHONE", BUSINESS_PHONE)
BUSINESS_TAX_ID: str = _text("BUSINESS_TAX_ID", BUSINESS_TAX_ID)
TERMS: str = _text("TERMS", TERMS)
PAYMENT_INSTRUCTIONS: str = _text("PAYMENT_INSTRUCTIONS", PAYMENT_INSTRUCTIONS)
REFUND_POLICY: str = _text("REFUND_POLICY", REFUND_POLICY)
FOOTER_SERVICES: str = _text("FOOTER_SERVICES", FOOTER_SERVICES)
BRAND_COLOR: str = _text("BRAND_COLOR", BRAND_COLOR)
INVOICE_PREFIX: str = _text("INVOICE_PREFIX", INVOICE_PREFIX)
SIGNATURE_PATH: str = _path(_env("SIGNATURE_PATH"), "assets/signature.png", "signature.png")
LOGO_PATH: str = _path(_env("LOGO_PATH"), "assets/logo.png", "logo.png")

# ---- companies ------------------------------------------------------------
@dataclass(frozen=True)
class Company:
    """One of the configured companies: a stable key, a display name and its channels."""

    key: str
    name: str
    statement_channel_id: Optional[int] = None
    invoice_channel_id: Optional[int] = None

    @property
    def statement_env(self) -> str:
        return f"COMPANY_{self.key.upper()}_STATEMENT_CHANNEL_ID"

    @property
    def invoice_env(self) -> str:
        return f"COMPANY_{self.key.upper()}_INVOICE_CHANNEL_ID"

    def channel_id(self, kind: str = "statement") -> Optional[int]:
        """Statement channel, or the invoice channel (which falls back to the statement channel)."""
        if kind == "invoice":
            return self.invoice_channel_id or self.statement_channel_id
        return self.statement_channel_id


COMPANIES: Dict[str, Company] = {
    key: Company(
        key=key,
        name=_text(f"COMPANY_{key.upper()}_NAME", cfg.get("name", key)),
        statement_channel_id=_one(f"COMPANY_{key.upper()}_STATEMENT_CHANNEL_ID",
                                  cfg.get("statement_channel_id", 0)),
        invoice_channel_id=_one(f"COMPANY_{key.upper()}_INVOICE_CHANNEL_ID",
                                cfg.get("invoice_channel_id", 0)),
    )
    for key, cfg in COMPANY_CONFIG.items()
}


def get_company(value) -> Optional[Company]:
    """Company by key (``hectorhost``) or display name (``HectorHost``); None if unknown/empty."""
    if not value:
        return None
    text = str(value).strip()
    if text.lower() in COMPANIES:
        return COMPANIES[text.lower()]
    for company in COMPANIES.values():
        if company.name.casefold() == text.casefold():
            return company
    return None


def company_name(value) -> str:
    """Display name for a stored company key (``—`` when unset, so old rows stay readable)."""
    company = get_company(value)
    if company:
        return company.name
    return str(value).strip() if value else "—"


# ---- ticket departments ---------------------------------------------------
def department_roles(key, guild=None) -> List[int]:
    """Staff role ids for a ticket category key in a server (empty when unset)."""
    return tickets_for(guild).roles(key)


def department_role_env(key) -> str:
    """The variable that configures a ticket category's staff roles."""
    return TICKET_DEPARTMENT_ROLE_ENV.get(str(key or "").strip().lower(), "TICKET_STAFF_ROLE_IDS")


def department_label(key) -> str:
    """Display label of a ticket category key (``Support`` when it is unknown)."""
    return TICKET_CATEGORY_LABELS.get(str(key or "").strip().lower(), "Support")


def category_key(category_id, guild=None) -> Optional[str]:
    """Key of a server's ticket category for a Discord category id (None when not ours)."""
    return tickets_for(guild).category_key(category_id)
