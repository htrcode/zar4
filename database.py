import asyncio
import datetime as dt
import json
import logging
import os
import re
from decimal import Decimal, ROUND_HALF_UP

import aiosqlite

from config import CURRENCY, INVOICE_PREFIX, TICKET_PREFIX, tickets_for
from desk.utils import STAGE_META, fmt, normalize_stage, today

log = logging.getLogger("invoice-bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL CHECK (kind IN ('income','expense')),
    UNIQUE(name, kind)
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_discord_id INTEGER,
    client_name TEXT NOT NULL,
    client_contact TEXT,
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    currency TEXT NOT NULL,
    subtotal INTEGER NOT NULL,
    discount INTEGER NOT NULL DEFAULT 0,
    tax_percent REAL NOT NULL DEFAULT 0,
    tax INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL,
    notes TEXT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    company_id TEXT,
    ticket_channel_id INTEGER,
    voided INTEGER NOT NULL DEFAULT 0,
    void_reason TEXT,
    overdue_notified INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    delivery_note TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    description TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price INTEGER NOT NULL,
    line_total INTEGER NOT NULL,
    details TEXT,
    product_type TEXT,
    service_name TEXT,
    node TEXT,
    plan TEXT,
    server_id TEXT,
    delivery_link TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    company_id TEXT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    method TEXT,
    note TEXT,
    transaction_id TEXT,
    paid_at TEXT NOT NULL,
    created_by INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('income','expense')),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    company_id TEXT,
    amount INTEGER NOT NULL CHECK (amount > 0),
    description TEXT,
    occurred_on TEXT NOT NULL,
    invoice_id INTEGER REFERENCES invoices(id),
    payment_id INTEGER REFERENCES payments(id),
    voided INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS revenue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT,
    category TEXT NOT NULL CHECK (category IN ('Commission','Investment','General')),
    amount INTEGER NOT NULL CHECK (amount > 0),
    description TEXT,
    received_from TEXT,
    method TEXT,
    transaction_id TEXT,
    remarks TEXT,
    recorded_by TEXT,
    occurred_on TEXT NOT NULL,
    voided INTEGER NOT NULL DEFAULT 0,
    void_reason TEXT,
    voided_at TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS statement_txns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('sale','revenue')),
    company_id TEXT,
    record_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    message_state TEXT,
    deleted_by INTEGER,
    deleted_at TEXT,
    delete_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (kind, record_id)
);
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER,
    message_id INTEGER,
    prize TEXT NOT NULL,
    winners INTEGER NOT NULL DEFAULT 1,
    required_role_id INTEGER,
    host_id INTEGER NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','ended')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS giveaway_winners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
    user_id INTEGER NOT NULL,
    picked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    guild_seq INTEGER,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'support',
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'open',
    client_discord_id INTEGER,
    client_name TEXT NOT NULL,
    channel_id INTEGER,
    parent_ticket_id INTEGER REFERENCES tickets(id),
    claimed_by INTEGER,
    close_reason TEXT,
    transcript_path TEXT,
    deal_id INTEGER,
    company_id TEXT,
    last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    opened_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ticket_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    guild_id INTEGER,
    author_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER REFERENCES tickets(id),
    client_discord_id INTEGER,
    client_name TEXT NOT NULL,
    service TEXT,
    budget INTEGER NOT NULL DEFAULT 0,
    value INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'new',
    next_follow_up TEXT,
    follow_up_note TEXT,
    reminder_sent_on TEXT,
    invoice_id INTEGER REFERENCES invoices(id),
    notes TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS deal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES deals(id),
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    client_discord_id INTEGER NOT NULL,
    client_name TEXT NOT NULL,
    service_name TEXT NOT NULL,
    server_ref TEXT NOT NULL,
    start_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    ticket_channel_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
    cancel_reason TEXT,
    cancelled_by INTEGER,
    cancelled_at TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS service_expiry_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL REFERENCES services(id),
    previous_expiry TEXT NOT NULL,
    new_expiry TEXT NOT NULL,
    days_changed INTEGER NOT NULL,
    reason TEXT,
    changed_by INTEGER NOT NULL,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS service_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL REFERENCES services(id),
    expiry_date TEXT NOT NULL,
    days_left INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'reminder' CHECK (kind IN ('reminder','expiry_change')),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','retry')),
    attempts INTEGER NOT NULL DEFAULT 0,
    dm_status TEXT,
    ticket_status TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT,
    UNIQUE (service_id, expiry_date, days_left)
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(occurred_on);
CREATE INDEX IF NOT EXISTS idx_tx_company ON transactions(company_id);
CREATE INDEX IF NOT EXISTS idx_revenue_date ON revenue(occurred_on);
CREATE INDEX IF NOT EXISTS idx_revenue_company ON revenue(company_id);
CREATE INDEX IF NOT EXISTS idx_giveaways_status ON giveaways(status);
CREATE INDEX IF NOT EXISTS idx_giveaway_entries ON giveaway_entries(giveaway_id);
CREATE INDEX IF NOT EXISTS idx_giveaway_winners ON giveaway_winners(giveaway_id);
CREATE INDEX IF NOT EXISTS idx_pay_inv ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_client ON tickets(client_discord_id);
CREATE INDEX IF NOT EXISTS idx_deals_ticket ON deals(ticket_id);
CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals(stage);
CREATE INDEX IF NOT EXISTS idx_deals_followup ON deals(next_follow_up);
CREATE INDEX IF NOT EXISTS idx_services_expiry ON services(status, expiry_date);
CREATE INDEX IF NOT EXISTS idx_services_guild ON services(guild_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_services_active_ref ON services(guild_id, lower(server_ref)) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_service_expiry_log ON service_expiry_log(service_id);
"""

# Revenue statement categories. Investment is tracked apart from operating revenue.
REVENUE_CATEGORIES = ("Commission", "Investment", "General")

DEFAULT_CATEGORIES = [
    ("Hosting Revenue", "income"),
    ("Other Sales", "income"),
    ("Server Rental", "expense"),
    ("Software Subscriptions", "expense"),
    ("Marketing", "expense"),
    ("Commissions", "expense"),
    ("Other Expenses", "expense"),
]

# Columns added after v1. Existing databases are upgraded in place.
NEW_COLUMNS = {
    "invoices": {
        "client_contact": "TEXT", "delivered_at": "TEXT", "delivery_note": "TEXT",
        "company_id": "TEXT",
    },
    "payments": {"transaction_id": "TEXT", "company_id": "TEXT"},
    "transactions": {"company_id": "TEXT"},
    "revenue": {"received_from": "TEXT", "method": "TEXT", "transaction_id": "TEXT",
                "remarks": "TEXT", "recorded_by": "TEXT", "void_reason": "TEXT",
                "voided_at": "TEXT"},
    "tickets": {"purchase_completed_at": "TEXT", "panel_message_id": "INTEGER",
                "company_id": "TEXT", "guild_id": "INTEGER", "guild_seq": "INTEGER"},
    "ticket_notes": {"guild_id": "INTEGER"},
    "audit_log": {"guild_id": "INTEGER"},
    "invoice_items": {
        "details": "TEXT", "product_type": "TEXT", "service_name": "TEXT",
        "node": "TEXT", "plan": "TEXT", "server_id": "TEXT", "delivery_link": "TEXT",
    },
}


def invoice_number(invoice_id: int) -> str:
    return f"{INVOICE_PREFIX}-{invoice_id:05d}"


#: ticket id -> its per-server display label ("TICKET-0001"), filled from the
#: database at startup and whenever a ticket is created. Ticket numbers restart
#: in every server, so the label cannot be derived from the global row id.
_TICKET_LABELS: dict[int, str] = {}


def remember_ticket_label(ticket_id: int, guild_seq: int | None, guild_id: int | None = None) -> str:
    """Build and cache a ticket's display label (``TICKET_PREFIX`` is per server)."""
    prefix = tickets_for(guild_id).prefix if guild_id else TICKET_PREFIX
    label = f"{prefix}-{int(guild_seq or ticket_id):04d}"
    _TICKET_LABELS[int(ticket_id)] = label
    return label


def ticket_number(ticket_id: int) -> str:
    """Number shown to staff: per-server sequence when known, else the row id."""
    return _TICKET_LABELS.get(int(ticket_id)) or f"{TICKET_PREFIX}-{int(ticket_id):04d}"


def revenue_number(revenue_id: int) -> str:
    """Permanent Revenue transaction id, e.g. REV-000003 (row ids are AUTOINCREMENT: never reused)."""
    return f"REV-{revenue_id:06d}"


def giveaway_number(giveaway_id: int) -> str:
    return f"GA-{giveaway_id:04d}"


def service_number(service_id: int) -> str:
    return f"SVC-{service_id:04d}"


def _like(text: str) -> str:
    """Escape ``%``, ``_`` and ``\\`` so user text matches literally inside a LIKE (ESCAPE '\\')."""
    return (text or "").strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _finish(inv: dict) -> dict:
    """Add computed fields: number, balance, status, is_partial."""
    paid = inv.get("paid", 0) or 0
    inv["paid"] = paid
    inv["number"] = invoice_number(inv["id"])
    inv["balance"] = max(inv["total"] - paid, 0)
    inv["is_partial"] = 0 < paid < inv["total"]
    if inv["voided"]:
        inv["status"] = "void"
    elif paid >= inv["total"]:
        inv["status"] = "paid"
    elif today().isoformat() > inv["due_date"]:
        inv["status"] = "overdue"
    elif paid > 0:
        inv["status"] = "partial"
    else:
        inv["status"] = "unpaid"
    return inv


def primary_service(inv: dict) -> dict:
    """The item that describes the server/product (first one with service fields)."""
    items = inv.get("items") or []
    for item in items:
        if any(item.get(k) for k in ("product_type", "service_name", "node", "plan", "server_id", "delivery_link")):
            return item
    return items[0] if items else {}


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()
        self.listeners: list = []
        self._event_tasks: set = set()

    async def connect(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        for table, columns in NEW_COLUMNS.items():
            existing = {r["name"] for r in await self.fetchall(f"PRAGMA table_info({table})")}
            for col, decl in columns.items():
                if col not in existing:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets(guild_id)")
        for name, kind in DEFAULT_CATEGORIES:
            await self.conn.execute(
                "INSERT OR IGNORE INTO categories(name, kind) VALUES (?, ?)", (name, kind)
            )
        await self.conn.commit()
        await self._load_ticket_labels()

    async def _load_ticket_labels(self) -> None:
        """Cache every ticket's per-server number so ``ticket_number(id)`` is correct."""
        _TICKET_LABELS.clear()
        rows = await self.fetchall("SELECT id, guild_id, guild_seq FROM tickets")
        for row in rows:
            if row["guild_seq"]:
                remember_ticket_label(row["id"], row["guild_seq"], row["guild_id"])

    async def assign_ticket_guilds(self, resolve, default_guild_id: int | None = None) -> int:
        """Fill ``guild_id``/``guild_seq`` for tickets created before multi-server support.

        ``resolve(channel_id) -> guild_id | None`` looks a ticket channel up in the
        bot's cache. When a channel is gone and only one server is known, that
        server is used (reliable only in that single-server case); otherwise the
        ticket keeps a NULL guild rather than being guessed into the wrong books.
        Numbers are then assigned per server in opening order.
        """
        rows = await self.fetchall("SELECT id, channel_id, guild_id FROM tickets")
        known = {row["guild_id"] for row in rows if row["guild_id"]}
        assigned = 0
        for row in rows:
            if row["guild_id"]:
                continue
            guild_id = resolve(row["channel_id"]) if row["channel_id"] else None
            if guild_id is None and default_guild_id is not None and len(known) <= 1:
                guild_id = default_guild_id
            if not guild_id:
                continue
            await self.conn.execute(
                "UPDATE tickets SET guild_id=? WHERE id=? AND guild_id IS NULL", (guild_id, row["id"])
            )
            known.add(guild_id)
            assigned += 1
        await self.conn.commit()
        guild_ids = [r["guild_id"] for r in await self.fetchall(
            "SELECT DISTINCT guild_id FROM tickets WHERE guild_id IS NOT NULL")]
        for guild_id in guild_ids:
            row = await self.fetchone(
                "SELECT COALESCE(MAX(guild_seq),0) AS last FROM tickets WHERE guild_id=?", (guild_id,))
            number = row["last"] if row else 0
            missing = await self.fetchall(
                "SELECT id FROM tickets WHERE guild_id=? AND guild_seq IS NULL ORDER BY id", (guild_id,))
            for pending in missing:
                number += 1
                await self.conn.execute(
                    "UPDATE tickets SET guild_seq=? WHERE id=? AND guild_seq IS NULL", (number, pending["id"])
                )
        await self.conn.commit()
        await self._load_ticket_labels()
        return assigned

    async def close(self):
        for task in list(self._event_tasks):
            task.cancel()
        if self.conn:
            await self.conn.close()

    async def fetchall(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetchone(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _audit(self, actor_id, action, entity, entity_id, details=None):
        """Write an audit line; ticket lines also record the server the ticket belongs to."""
        guild_id = None
        if entity == "ticket" and entity_id:
            owner = await self.fetchone("SELECT guild_id FROM tickets WHERE id=?", (entity_id,))
            guild_id = owner["guild_id"] if owner else None
        await self.conn.execute(
            "INSERT INTO audit_log(actor_id, action, entity, entity_id, details, guild_id) "
            "VALUES (?,?,?,?,?,?)",
            (actor_id, action, entity, entity_id, json.dumps(details or {}, default=str), guild_id),
        )

    # ---------- event listeners (channel feeds are driven from here) ----------
    def subscribe(self, listener):
        """listener(event: str, payload: dict) -> awaitable. Errors never break a transaction."""
        self.listeners.append(listener)

    async def _emit(self, event: str, payload: dict):
        """Fire-and-forget so a slow channel post never delays a command response."""
        for listener in list(self.listeners):
            task = asyncio.create_task(self._run_listener(listener, event, payload))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

    async def _run_listener(self, listener, event, payload):
        try:
            await listener(event, payload)
        except Exception:  # a broken feed must never roll back money
            log.exception("Database event listener failed for %s", event)

    async def _touch(self, *, ticket_id=None, deal_id=None, invoice_id=None, channel_id=None):
        """Tell listeners which ticket dashboards changed (deal / invoice / channel resolve to tickets)."""
        try:
            ids = {ticket_id} if ticket_id else set()
            if deal_id:
                row = await self.fetchone("SELECT ticket_id FROM deals WHERE id=?", (deal_id,))
                if row and row["ticket_id"]:
                    ids.add(row["ticket_id"])
            if invoice_id:
                for row in await self.fetchall(
                        "SELECT ticket_id FROM deals WHERE invoice_id=? AND ticket_id IS NOT NULL", (invoice_id,)):
                    ids.add(row["ticket_id"])
                row = await self.fetchone(
                    "SELECT t.id FROM invoices i JOIN tickets t ON t.channel_id=i.ticket_channel_id "
                    "WHERE i.id=? ORDER BY t.id DESC LIMIT 1", (invoice_id,))
                if row:
                    ids.add(row["id"])
            if channel_id:
                row = await self.ticket_by_channel(channel_id)
                if row:
                    ids.add(row["id"])
            for tid in ids:
                await self._emit("ticket.updated", {"ticket_id": tid})
        except Exception:  # a dashboard refresh must never break a database write
            log.exception("Could not queue a ticket dashboard refresh")

    # ---------- categories ----------
    async def categories(self, kind: str | None = None):
        if kind:
            rows = await self.fetchall("SELECT * FROM categories WHERE kind=? ORDER BY name", (kind,))
        else:
            rows = await self.fetchall("SELECT * FROM categories ORDER BY kind, name")
        return [dict(r) for r in rows]

    async def category_id(self, name: str, kind: str) -> int:
        row = await self.fetchone(
            "SELECT id FROM categories WHERE name=? AND kind=?", (name.strip(), kind)
        )
        if not row:
            raise ValueError(f"Unknown {kind} category `{name}`. Pick one from the list or add it first.")
        return row["id"]

    async def ensure_category(self, name: str, kind: str) -> int:
        """Return the id of a category, creating it first if it does not exist yet."""
        name = (name or "").strip()[:50] or ("Other Sales" if kind == "income" else "Other Expenses")
        async with self.lock:
            await self.conn.execute("INSERT OR IGNORE INTO categories(name, kind) VALUES (?, ?)", (name, kind))
            await self.conn.commit()
        return await self.category_id(name, kind)

    async def add_category(self, actor_id: int, name: str, kind: str):
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    "INSERT INTO categories(name, kind) VALUES (?,?)", (name.strip()[:50], kind)
                )
            except aiosqlite.IntegrityError:
                raise ValueError("That category already exists.")
            await self._audit(actor_id, "category.add", "category", cur.lastrowid, {"name": name, "kind": kind})
            await self.conn.commit()

    # ---------- invoices ----------
    async def create_invoice(self, actor_id, *, client_discord_id, client_name, client_contact=None, items,
                             due_days, tax_percent, discount_cents, category, notes, ticket_channel_id,
                             company_id=None):
        category_id = await self.category_id(category, "income")
        lines = []
        for item in items:
            qty = float(item["quantity"])
            price = int(item["unit_price"])
            line_total = int((Decimal(str(qty)) * price).quantize(Decimal(1), ROUND_HALF_UP))
            lines.append((
                item["description"], qty, price, line_total, item.get("details"),
                item.get("product_type"), item.get("service_name"), item.get("node"),
                item.get("plan"), item.get("server_id"), item.get("delivery_link"),
            ))
        subtotal = sum(l[3] for l in lines)
        discount = min(discount_cents, subtotal)
        taxable = subtotal - discount
        tax = int((Decimal(taxable) * Decimal(str(tax_percent)) / 100).quantize(Decimal(1), ROUND_HALF_UP))
        total = taxable + tax
        issue = today()
        due = issue.fromordinal(issue.toordinal() + due_days)
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    """INSERT INTO invoices(client_discord_id, client_name, client_contact, issue_date, due_date, currency,
                       subtotal, discount, tax_percent, tax, total, notes, category_id, company_id,
                       ticket_channel_id, created_by)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (client_discord_id, client_name, (client_contact or None), issue.isoformat(), due.isoformat(), CURRENCY,
                     subtotal, discount, tax_percent, tax, total, notes or None, category_id,
                     (company_id or None), ticket_channel_id, actor_id),
                )
                invoice_id = cur.lastrowid
                await self.conn.executemany(
                    """INSERT INTO invoice_items(invoice_id, description, quantity, unit_price, line_total, details,
                       product_type, service_name, node, plan, server_id, delivery_link)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(invoice_id, *l) for l in lines],
                )
                await self._audit(actor_id, "invoice.create", "invoice", invoice_id,
                                  {"client": client_name, "total": total, "items": len(lines),
                                   "company": company_id})
                await self._sync_deal_new_invoice(invoice_id, ticket_channel_id, total, actor_id)
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        await self._emit("invoice.created", {"invoice_id": invoice_id})
        await self._touch(invoice_id=invoice_id, channel_id=ticket_channel_id)
        return invoice_id

    async def _sync_deal_new_invoice(self, invoice_id, ticket_channel_id, total, actor_id):
        """Link the invoice to the ticket's deal and move a fresh lead to `quoted`."""
        if not ticket_channel_id:
            return
        row = await self.fetchone(
            "SELECT id FROM tickets WHERE channel_id=? ORDER BY id DESC LIMIT 1", (ticket_channel_id,)
        )
        if not row:
            return
        deal = await self.fetchone("SELECT * FROM deals WHERE ticket_id=?", (row["id"],))
        if not deal:
            return
        stage = deal["stage"] if deal["stage"] in ("won", "lost", "negotiation") else "quoted"
        await self.conn.execute(
            "UPDATE deals SET invoice_id=?, value=?, stage=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (invoice_id, total, stage, deal["id"]),
        )
        await self._deal_event(deal["id"], actor_id, "invoice", f"Invoice #{invoice_number(invoice_id)} created for {fmt(total)}")

    async def get_invoice(self, invoice_id: int):
        row = await self.fetchone(
            "SELECT i.*, c.name AS category FROM invoices i JOIN categories c ON c.id=i.category_id WHERE i.id=?",
            (invoice_id,),
        )
        if not row:
            return None
        inv = dict(row)
        inv["items"] = [dict(r) for r in await self.fetchall(
            "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,))]
        inv["payments"] = [dict(r) for r in await self.fetchall(
            "SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (invoice_id,))]
        inv["paid"] = sum(p["amount"] for p in inv["payments"])
        return _finish(inv)

    async def list_invoices(self, status: str | None = None, month: str | None = None, limit: int = 15,
                            company_id: str | None = None):
        rows = await self.fetchall(
            """SELECT i.*, COALESCE((SELECT SUM(amount) FROM payments p WHERE p.invoice_id=i.id),0) AS paid
               FROM invoices i ORDER BY i.id DESC"""
        )
        out = []
        for r in rows:
            inv = _finish(dict(r))
            if company_id and inv.get("company_id") != company_id:
                continue
            if month and not inv["issue_date"].startswith(month):
                continue
            if status == "partial" and not (inv["is_partial"] and not inv["voided"]):
                continue
            if status and status != "partial" and inv["status"] != status:
                continue
            out.append(inv)
            if len(out) >= limit:
                break
        return out

    async def set_ticket(self, actor_id, invoice_id, channel_id):
        async with self.lock:
            await self.conn.execute("UPDATE invoices SET ticket_channel_id=? WHERE id=?", (channel_id, invoice_id))
            await self._audit(actor_id, "invoice.ticket", "invoice", invoice_id, {"channel_id": channel_id})
            await self.conn.commit()
        await self._touch(invoice_id=invoice_id, channel_id=channel_id)

    async def mark_delivered(self, actor_id, invoice_id, note=""):
        async with self.lock:
            await self.conn.execute(
                "UPDATE invoices SET delivered_at=? WHERE id=? AND delivered_at IS NULL",
                (today().isoformat(), invoice_id),
            )
            if note:
                await self.conn.execute("UPDATE invoices SET delivery_note=? WHERE id=?", (note[:300], invoice_id))
            await self._audit(actor_id, "invoice.deliver", "invoice", invoice_id, {"note": note[:200]})
            await self.conn.commit()

    async def add_payment(self, actor_id, invoice_id, amount, method="manual", note="", transaction_id=""):
        async with self.lock:  # prevents two simultaneous payments overpaying
            inv = await self.get_invoice(invoice_id)
            if not inv:
                raise ValueError("Invoice not found.")
            if inv["voided"]:
                raise ValueError("That invoice is void.")
            if amount <= 0:
                raise ValueError("Amount must be above zero.")
            if amount > inv["balance"]:
                raise ValueError(f"Amount exceeds the remaining balance ({fmt(inv['balance'])}).")
            try:
                paid_on = today().isoformat()
                cur = await self.conn.execute(
                    "INSERT INTO payments(invoice_id, company_id, amount, method, note, transaction_id, paid_at, created_by) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (invoice_id, inv.get("company_id"), amount, (method or "manual")[:40], (note or "")[:200],
                     (transaction_id or "").strip()[:100] or None, paid_on, actor_id),
                )
                pid = cur.lastrowid
                # Auto-record the income so invoices and books never drift apart
                await self.conn.execute(
                    """INSERT INTO transactions(kind, category_id, amount, description, occurred_on,
                       invoice_id, payment_id, company_id, created_by) VALUES ('income',?,?,?,?,?,?,?,?)""",
                    (inv["category_id"], amount, f"Payment for {inv['number']} ({inv['client_name']})",
                     paid_on, invoice_id, pid, inv.get("company_id"), actor_id),
                )
                await self._sync_deal_payment(invoice_id, inv, inv["paid"] + amount, actor_id)
                await self._audit(actor_id, "payment.add", "invoice", invoice_id,
                                  {"payment_id": pid, "amount": amount, "method": method,
                                   "balance_after": inv["balance"] - amount})
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        await self._emit("payment", {"invoice_id": invoice_id, "payment_id": pid})
        await self._touch(invoice_id=invoice_id)
        return pid

    async def _sync_deal_payment(self, invoice_id, inv, paid_total, actor_id):
        """Move the linked deal to won once the invoice is fully paid."""
        deal = await self.fetchone("SELECT * FROM deals WHERE invoice_id=?", (invoice_id,))
        if not deal or deal["stage"] in ("won", "lost"):
            return
        if paid_total >= inv["total"]:
            await self.conn.execute(
                "UPDATE deals SET stage='won', value=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (inv["total"], deal["id"]),
            )
            await self._deal_event(deal["id"], actor_id, "payment",
                                   f"Invoice #{inv['number']} paid in full — deal won")

    async def void_invoice(self, actor_id, invoice_id, reason):
        async with self.lock:
            inv = await self.get_invoice(invoice_id)
            if not inv:
                raise ValueError("Invoice not found.")
            if inv["voided"]:
                raise ValueError("Already void.")
            await self.conn.execute("UPDATE invoices SET voided=1, void_reason=? WHERE id=?", (reason[:200], invoice_id))
            await self.conn.execute("UPDATE transactions SET voided=1 WHERE invoice_id=?", (invoice_id,))
            await self._audit(actor_id, "invoice.void", "invoice", invoice_id,
                              {"reason": reason, "had_paid": inv["paid"]})
            await self.conn.commit()
        # Its Sales Statements must leave the sales totals too (the records stay for audit).
        await self._emit("invoice.void", {"invoice_id": invoice_id, "reason": reason})
        await self._touch(invoice_id=invoice_id)

    async def overdue_to_notify(self):
        rows = await self.list_invoices(limit=10_000)
        return [i for i in rows if i["status"] == "overdue" and not i["overdue_notified"]]

    async def mark_notified(self, invoice_id):
        async with self.lock:
            await self.conn.execute("UPDATE invoices SET overdue_notified=1 WHERE id=?", (invoice_id,))
            await self.conn.commit()

    async def outstanding(self, company_id: str | None = None) -> int:
        sql = """SELECT COALESCE(SUM(i.total - COALESCE(p.paid,0)),0) AS due FROM invoices i
               LEFT JOIN (SELECT invoice_id, SUM(amount) paid FROM payments GROUP BY invoice_id) p
               ON p.invoice_id=i.id WHERE i.voided=0"""
        params: list = []
        if company_id:
            sql += " AND i.company_id=?"
            params.append(company_id)
        row = await self.fetchone(sql, params)
        return row["due"]

    # ---------- gross sales ----------
    async def gross_sales(self, month: str | None = None, company_id: str | None = None) -> int:
        """Gross sales = all recorded income (invoice payments + manual income)."""
        sql = "SELECT COALESCE(SUM(amount),0) AS total FROM transactions WHERE voided=0 AND kind='income'"
        params: list = []
        if month:
            sql += " AND substr(occurred_on,1,7)=?"
            params.append(month)
        if company_id:
            sql += " AND company_id=?"
            params.append(company_id)
        row = await self.fetchone(sql, params)
        return row["total"] or 0

    # ---------- tickets ----------
    async def create_ticket(self, actor_id, *, subject, kind, priority, client_discord_id, client_name,
                            channel_id, parent_ticket_id=None, budget=0, service="", note="", company_id=None,
                            guild_id=None):
        async with self.lock:
            try:
                now = today().isoformat()
                guild_id = int(guild_id) if guild_id else None
                # Ticket numbers restart at 1 in every server.
                seq_row = await self.fetchone(
                    "SELECT COALESCE(MAX(guild_seq),0)+1 AS next FROM tickets WHERE guild_id IS ?",
                    (guild_id,),
                )
                guild_seq = seq_row["next"] if seq_row else 1
                cur = await self.conn.execute(
                    """INSERT INTO tickets(subject, kind, priority, status, client_discord_id, client_name,
                       channel_id, parent_ticket_id, company_id, opened_by, last_activity, guild_id, guild_seq)
                       VALUES (?,?,?, 'open', ?,?,?,?,?,?,?,?,?)""",
                    (subject[:120], kind, priority, client_discord_id, client_name[:100], channel_id,
                     parent_ticket_id, (company_id or None), actor_id, now, guild_id, guild_seq),
                )
                ticket_id = cur.lastrowid
                remember_ticket_label(ticket_id, guild_seq, guild_id)
                if note:
                    await self.conn.execute(
                        "INSERT INTO ticket_notes(ticket_id, author_id, note, guild_id) "
                        "VALUES (?,?,?,(SELECT guild_id FROM tickets WHERE id=?))",
                        (ticket_id, actor_id, note[:1500], ticket_id),
                    )
                if kind in ("purchase", "enquiry"):
                    # Enquiries get a follow-up today; purchases get a delivery check tomorrow.
                    follow_up = now if kind == "enquiry" else (today() + dt.timedelta(days=1)).isoformat()
                    deal = await self.conn.execute(
                        """INSERT INTO deals(ticket_id, client_discord_id, client_name, service, budget,
                           stage, next_follow_up, created_by, notes)
                           VALUES (?,?,?,?,?, 'new', ?,?,?)""",
                        (ticket_id, client_discord_id, client_name[:100], service or None, max(int(budget), 0),
                         follow_up, actor_id, note[:1000] or None),
                    )
                    deal_id = deal.lastrowid
                    await self.conn.execute("UPDATE tickets SET deal_id=? WHERE id=?", (deal_id, ticket_id))
                    await self._deal_event(deal_id, actor_id, "created",
                                           f"{kind.title()} ticket {ticket_number(ticket_id)} opened")
                if parent_ticket_id:
                    await self.conn.execute(
                        "INSERT INTO ticket_notes(ticket_id, author_id, note, guild_id) "
                        "VALUES (?,?,?,(SELECT guild_id FROM tickets WHERE id=?))",
                        (parent_ticket_id, actor_id,
                         f"Follow-up ticket {ticket_number(ticket_id)} created.", parent_ticket_id),
                    )
                await self._audit(actor_id, "ticket.create", "ticket", ticket_id,
                                  {"kind": kind, "client": client_name, "parent": parent_ticket_id})
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        await self._emit("ticket.created", {"ticket_id": ticket_id})
        if parent_ticket_id:
            await self._touch(ticket_id=parent_ticket_id)  # its notes changed
        return ticket_id

    async def get_ticket(self, ticket_id: int):
        row = await self.fetchone("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        return dict(row) if row else None

    async def ticket_by_channel(self, channel_id: int):
        row = await self.fetchone(
            "SELECT * FROM tickets WHERE channel_id=? ORDER BY id DESC LIMIT 1", (channel_id,)
        )
        return dict(row) if row else None

    async def list_tickets(self, status: str | None = "open", kind: str | None = None,
                           client_id: int | None = None, limit: int = 25, guild_id: int | None = None):
        sql = "SELECT * FROM tickets WHERE 1=1"
        params: list = []
        if guild_id:
            sql += " AND guild_id=?"
            params.append(guild_id)
        if status and status != "all":
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if client_id:
            sql += " AND client_discord_id=?"
            params.append(client_id)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await self.fetchall(sql, params)]

    async def ticket_children(self, ticket_id: int):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM tickets WHERE parent_ticket_id=? ORDER BY id", (ticket_id,))]

    async def ticket_parent(self, ticket_id: int):
        row = await self.fetchone(
            """SELECT p.* FROM tickets t JOIN tickets p ON p.id=t.parent_ticket_id WHERE t.id=?""",
            (ticket_id,),
        )
        return dict(row) if row else None

    async def ticket_stats(self, guild_id: int | None = None) -> dict:
        sql = """SELECT
                 SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open,
                 SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed,
                 SUM(CASE WHEN status!='closed' AND claimed_by IS NULL THEN 1 ELSE 0 END) AS unclaimed
               FROM tickets"""
        params: tuple = ()
        if guild_id:
            sql += " WHERE guild_id=?"
            params = (guild_id,)
        row = await self.fetchone(sql, params)
        stats = {k: (row[k] or 0) for k in ("open", "pending", "closed", "unclaimed")}
        return stats

    async def stale_tickets(self, days: int, guild_id: int | None = None):
        import datetime as _dt

        cutoff = (today() - _dt.timedelta(days=max(int(days), 1))).isoformat()
        sql = """SELECT * FROM tickets WHERE status!='closed'
                 AND date(last_activity) <= date(?)"""
        params: list = [cutoff]
        if guild_id:
            sql += " AND guild_id=?"
            params.append(guild_id)
        sql += " ORDER BY last_activity ASC"
        return [dict(r) for r in await self.fetchall(sql, params)]

    async def count_open_tickets(self, client_id: int, guild_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM tickets WHERE client_discord_id=? AND status!='closed'"
        params: list = [client_id]
        if guild_id:
            sql += " AND guild_id=?"
            params.append(guild_id)
        row = await self.fetchone(sql, params)
        return row["n"] if row else 0

    async def set_ticket_status(self, actor_id, ticket_id, status, reason=None, transcript_path=None):
        async with self.lock:
            fields = ["status=?", "updated_at=CURRENT_TIMESTAMP"]
            params: list = [status]
            if status == "closed":
                fields += ["close_reason=?", "closed_at=CURRENT_TIMESTAMP"]
                params.append((reason or "")[:300] or None)
            else:
                fields += ["closed_at=NULL", "close_reason=NULL"]
            if transcript_path:
                fields.append("transcript_path=?")
                params.append(transcript_path)
            params.append(ticket_id)
            await self.conn.execute(f"UPDATE tickets SET {', '.join(fields)} WHERE id=?", params)
            await self._audit(actor_id, f"ticket.{status}", "ticket", ticket_id, {"reason": reason})
            await self.conn.commit()
        await self._touch(ticket_id=ticket_id)

    async def set_ticket_channel(self, actor_id, ticket_id, channel_id):
        async with self.lock:
            await self.conn.execute(
                "UPDATE tickets SET channel_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (channel_id, ticket_id)
            )
            await self._audit(actor_id, "ticket.channel", "ticket", ticket_id, {"channel_id": channel_id})
            await self.conn.commit()
        await self._touch(ticket_id=ticket_id)

    async def set_ticket_company(self, actor_id, ticket_id, company_id):
        """Set (or clear, with None) the financial company of a ticket.

        Moving a ticket between Discord categories never touches this: the
        category is a workflow label, the company is an accounting fact.
        """
        async with self.lock:
            ticket = await self.fetchone("SELECT * FROM tickets WHERE id=?", (ticket_id,))
            if not ticket:
                raise ValueError("Ticket not found.")
            await self.conn.execute(
                "UPDATE tickets SET company_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (company_id or None, ticket_id),
            )
            await self._audit(actor_id, "ticket.company", "ticket", ticket_id,
                              {"from": ticket["company_id"], "to": company_id})
            await self.conn.commit()
        await self._touch(ticket_id=ticket_id)

    async def claim_purchase_completion(self, actor_id, ticket_id, details: dict) -> bool:
        """Atomically mark a ticket's purchase as completed. False if it already was (duplicate submit)."""
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE tickets SET purchase_completed_at=datetime('now'), updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND purchase_completed_at IS NULL AND status!='closed'", (ticket_id,))
            if cur.rowcount != 1:
                await self.conn.rollback()
                return False
            await self._audit(actor_id, "ticket.purchase_completed", "ticket", ticket_id, details)
            await self.conn.commit()
        await self._touch(ticket_id=ticket_id)
        return True

    async def release_purchase_completion(self, actor_id, ticket_id):
        """Undo claim_purchase_completion when the channel move failed, so staff can retry."""
        async with self.lock:
            await self.conn.execute("UPDATE tickets SET purchase_completed_at=NULL WHERE id=?", (ticket_id,))
            await self._audit(actor_id, "ticket.purchase_completion_failed", "ticket", ticket_id, {})
            await self.conn.commit()
        await self._touch(ticket_id=ticket_id)

    async def set_ticket_claimed(self, actor_id, ticket_id, user_id):
        async with self.lock:
            await self.conn.execute(
                "UPDATE tickets SET claimed_by=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (user_id, ticket_id)
            )
            await self._audit(actor_id, "ticket.claim", "ticket", ticket_id, {"claimed_by": user_id})
            await self.conn.commit()
        await self._touch(ticket_id=ticket_id)

    async def set_ticket_priority(self, actor_id, ticket_id, priority):
        async with self.lock:
            await self.conn.execute(
                "UPDATE tickets SET priority=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (priority, ticket_id)
            )
            await self._audit(actor_id, "ticket.priority", "ticket", ticket_id, {"priority": priority})
            await self.conn.commit()
        await self._touch(ticket_id=ticket_id)

    async def touch_ticket(self, channel_id: int):
        async with self.lock:
            await self.conn.execute(
                "UPDATE tickets SET last_activity=datetime('now') WHERE channel_id=? AND status!='closed'",
                (channel_id,),
            )
            await self.conn.commit()

    async def set_ticket_panel(self, ticket_id, message_id):
        """Remember which message in the ticket channel is the live dashboard (edited in place)."""
        async with self.lock:
            await self.conn.execute("UPDATE tickets SET panel_message_id=? WHERE id=?", (message_id, ticket_id))
            await self.conn.commit()

    async def record_ticket_move(self, actor_id, ticket_id, details: dict):
        """History entry for a category move / rename (the channel itself is edited by the caller)."""
        async with self.lock:
            await self.conn.execute("UPDATE tickets SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ticket_id,))
            await self._audit(actor_id, "ticket.move", "ticket", ticket_id, details)
            await self.conn.commit()
        await self._touch(ticket_id=ticket_id)

    async def add_ticket_note(self, actor_id, ticket_id, note):
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT INTO ticket_notes(ticket_id, author_id, note, guild_id) "
                "VALUES (?,?,?,(SELECT guild_id FROM tickets WHERE id=?))",
                (ticket_id, actor_id, note[:1500], ticket_id),
            )
            await self.conn.execute(
                "UPDATE tickets SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ticket_id,)
            )
            await self._audit(actor_id, "ticket.note", "ticket", ticket_id, {"note": note[:200]})
            await self.conn.commit()
            note_id = cur.lastrowid
        await self._touch(ticket_id=ticket_id)
        return note_id

    async def ticket_notes(self, ticket_id: int, limit: int = 10):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM ticket_notes WHERE ticket_id=? ORDER BY id DESC LIMIT ?", (ticket_id, limit))]

    # ---------- deals (sales pipeline) ----------
    async def _deal_event(self, deal_id, actor_id, kind, detail):
        await self.conn.execute(
            "INSERT INTO deal_events(deal_id, actor_id, kind, detail) VALUES (?,?,?,?)",
            (deal_id, actor_id, kind, (detail or "")[:300]),
        )

    async def get_deal(self, deal_id: int):
        row = await self.fetchone(
            """SELECT d.*, t.channel_id AS ticket_channel_id, t.subject AS ticket_subject,
                      t.status AS ticket_status, t.kind AS ticket_kind
               FROM deals d LEFT JOIN tickets t ON t.id=d.ticket_id WHERE d.id=?""", (deal_id,)
        )
        return dict(row) if row else None

    async def deal_by_ticket(self, ticket_id: int):
        row = await self.fetchone(
            """SELECT d.*, t.channel_id AS ticket_channel_id, t.subject AS ticket_subject,
                      t.status AS ticket_status, t.kind AS ticket_kind
               FROM deals d LEFT JOIN tickets t ON t.id=d.ticket_id WHERE d.ticket_id=?""", (ticket_id,)
        )
        return dict(row) if row else None

    async def create_deal(self, actor_id, *, client_name, client_discord_id=None, service="", budget=0,
                          stage="new", ticket_id=None, notes="", next_follow_up=None):
        stage = normalize_stage(stage)
        if ticket_id and await self.fetchone("SELECT 1 FROM deals WHERE ticket_id=?", (ticket_id,)):
            raise ValueError("That ticket already has a sales deal. Use `/sales deal` to update it.")
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    """INSERT INTO deals(ticket_id, client_discord_id, client_name, service, budget,
                       stage, next_follow_up, notes, created_by)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (ticket_id, client_discord_id, client_name[:100], service[:120] or None,
                     max(int(budget), 0), stage, next_follow_up or today().isoformat(),
                     notes[:1000] or None, actor_id),
                )
                deal_id = cur.lastrowid
                if ticket_id:
                    await self.conn.execute("UPDATE tickets SET deal_id=? WHERE id=?", (deal_id, ticket_id))
                await self._deal_event(deal_id, actor_id, "created", f"Deal created at stage {stage}")
                await self._audit(actor_id, "deal.create", "deal", deal_id,
                                  {"client": client_name, "budget": budget, "ticket": ticket_id})
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        await self._touch(ticket_id=ticket_id)
        return deal_id

    async def set_deal_stage(self, actor_id, deal_id, stage, detail=""):
        stage = normalize_stage(stage)
        async with self.lock:
            deal = await self.fetchone("SELECT * FROM deals WHERE id=?", (deal_id,))
            if not deal:
                raise ValueError("Deal not found.")
            await self.conn.execute(
                "UPDATE deals SET stage=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (stage, deal_id)
            )
            if stage in ("won", "lost"):
                await self.conn.execute(
                    "UPDATE deals SET next_follow_up=NULL, reminder_sent_on=NULL WHERE id=?", (deal_id,)
                )
            await self._deal_event(deal_id, actor_id, "stage", detail or f"Stage {deal['stage']} → {stage}")
            await self._audit(actor_id, "deal.stage", "deal", deal_id, {"from": deal["stage"], "to": stage})
            await self.conn.commit()
        await self._touch(deal_id=deal_id)

    async def advance_deal(self, actor_id, deal_id):
        order = ["new", "contacted", "quoted", "negotiation"]
        deal = await self.get_deal(deal_id)
        if not deal:
            raise ValueError("Deal not found.")
        if deal["stage"] not in order:
            raise ValueError("This deal is already closed. Use `/sales stage` to reopen it.")
        nxt = order[order.index(deal["stage"]) + 1]
        await self.set_deal_stage(actor_id, deal_id, nxt, f"Advanced to {nxt}")
        return nxt

    async def set_deal_followup(self, actor_id, deal_id, when, note=""):
        async with self.lock:
            deal = await self.fetchone("SELECT * FROM deals WHERE id=?", (deal_id,))
            if not deal:
                raise ValueError("Deal not found.")
            if deal["stage"] in ("won", "lost"):
                raise ValueError("This deal is closed. Reopen it with `/sales stage` before scheduling a follow-up.")
            await self.conn.execute(
                """UPDATE deals SET next_follow_up=?, follow_up_note=?, reminder_sent_on=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (when, note[:300] or None, deal_id),
            )
            await self._deal_event(deal_id, actor_id, "followup", f"Follow-up {when}: {note[:150]}")
            await self._audit(actor_id, "deal.followup", "deal", deal_id, {"date": when, "note": note[:200]})
            await self.conn.commit()
        await self._touch(deal_id=deal_id)

    async def add_deal_note(self, actor_id, deal_id, note):
        async with self.lock:
            deal = await self.fetchone("SELECT * FROM deals WHERE id=?", (deal_id,))
            if not deal:
                raise ValueError("Deal not found.")
            await self.conn.execute(
                "UPDATE deals SET notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (note[:1000], deal_id),
            )
            await self._deal_event(deal_id, actor_id, "note", note[:250])
            await self._audit(actor_id, "deal.note", "deal", deal_id, {"note": note[:200]})
            await self.conn.commit()
        await self._touch(deal_id=deal_id)

    async def link_deal_invoice(self, actor_id, deal_id, invoice_id):
        inv = await self.get_invoice(invoice_id)
        if not inv:
            raise ValueError("Invoice not found.")
        async with self.lock:
            await self.conn.execute(
                "UPDATE deals SET invoice_id=?, value=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (invoice_id, inv["total"], deal_id),
            )
            await self._deal_event(deal_id, actor_id, "invoice", f"Linked invoice #{inv['number']}")
            await self._audit(actor_id, "deal.invoice", "deal", deal_id, {"invoice_id": invoice_id})
            await self.conn.commit()
        await self._touch(deal_id=deal_id)

    async def list_deals(self, stage=None, limit=25):
        sql = """SELECT d.*, t.channel_id AS ticket_channel_id, t.subject AS ticket_subject,
                        t.status AS ticket_status
                 FROM deals d LEFT JOIN tickets t ON t.id=d.ticket_id"""
        params: list = []
        if stage and stage != "all":
            sql += " WHERE d.stage=?"
            params.append(normalize_stage(stage))
        sql += " ORDER BY CASE WHEN d.stage IN ('won','lost') THEN 1 ELSE 0 END, d.next_follow_up, d.updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await self.fetchall(sql, params)]

    async def pipeline(self):
        rows = await self.fetchall(
            "SELECT stage, COUNT(*) AS count, COALESCE(SUM(value),0) AS value, COALESCE(SUM(budget),0) AS budget FROM deals GROUP BY stage"
        )
        by_stage = {r["stage"]: dict(r) for r in rows}
        out = []
        for stage in ("new", "contacted", "quoted", "negotiation", "won", "lost"):
            row = by_stage.get(stage, {"stage": stage, "count": 0, "value": 0, "budget": 0})
            row["probability"] = STAGE_META[stage][1]
            row["weighted"] = int((row["value"] or 0) * STAGE_META[stage][1])
            out.append(row)
        return out

    async def deals_due(self, until: str | None = None):
        """Open follow-ups due on/before `until` (defaults to today)."""
        until = until or today().isoformat()
        return [dict(r) for r in await self.fetchall(
            """SELECT d.*, t.channel_id AS ticket_channel_id, t.subject AS ticket_subject
               FROM deals d LEFT JOIN tickets t ON t.id=d.ticket_id
               WHERE d.stage NOT IN ('won','lost') AND d.next_follow_up IS NOT NULL
                 AND d.next_follow_up <= ? ORDER BY d.next_follow_up ASC""",
            (until,),
        )]

    async def deals_due_for_reminder(self):
        return [dict(r) for r in await self.fetchall(
            """SELECT d.*, t.channel_id AS ticket_channel_id
               FROM deals d LEFT JOIN tickets t ON t.id=d.ticket_id
               WHERE d.stage NOT IN ('won','lost') AND d.next_follow_up IS NOT NULL
                 AND d.next_follow_up <= ? AND (d.reminder_sent_on IS NULL OR d.reminder_sent_on < ?)""",
            (today().isoformat(), today().isoformat()),
        )]

    async def mark_deal_reminded(self, deal_id, on=None):
        async with self.lock:
            await self.conn.execute(
                "UPDATE deals SET reminder_sent_on=? WHERE id=?", ((on or today().isoformat()), deal_id)
            )
            await self.conn.commit()

    async def deal_timeline(self, deal_id, limit=12):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM deal_events WHERE deal_id=? ORDER BY id DESC LIMIT ?", (deal_id, limit))]

    # ---------- transactions ----------
    async def add_transaction(self, actor_id, kind, category, amount, description, occurred_on,
                              company_id=None, *, voided=False):
        """Record an income/expense entry; ``voided`` keeps history out of every total."""
        if amount <= 0:
            raise ValueError("Amount must be above zero.")
        cid = await self.category_id(category, kind)
        async with self.lock:
            cur = await self.conn.execute(
                """INSERT INTO transactions(kind, category_id, company_id, amount, description, occurred_on,
                   created_by, voided) VALUES (?,?,?,?,?,?,?,?)""",
                (kind, cid, (company_id or None), amount, description[:200], occurred_on,
                 actor_id, 1 if voided else 0),
            )
            await self._audit(actor_id, f"transaction.{kind}", "transaction", cur.lastrowid,
                              {"category": category, "amount": amount, "date": occurred_on,
                               "company": company_id, "voided": bool(voided)})
            await self.conn.commit()
            return cur.lastrowid

    async def void_transaction(self, actor_id, tx_id, reason):
        async with self.lock:
            row = await self.fetchone("SELECT * FROM transactions WHERE id=?", (tx_id,))
            if not row:
                raise ValueError("Transaction not found.")
            if row["voided"]:
                raise ValueError("Already void.")
            if row["invoice_id"]:
                raise ValueError("This income came from an invoice payment. Void the invoice instead.")
            await self.conn.execute("UPDATE transactions SET voided=1 WHERE id=?", (tx_id,))
            await self._audit(actor_id, "transaction.void", "transaction", tx_id,
                              {"reason": reason, "amount": row["amount"]})
            await self.conn.commit()
            return dict(row)

    async def transactions(self, month: str, kind: str | None = None, limit: int = 20,
                           company_id: str | None = None):
        sql = """SELECT t.*, c.name AS category FROM transactions t JOIN categories c ON c.id=t.category_id
                 WHERE t.voided=0 AND substr(t.occurred_on,1,7)=?"""
        params = [month]
        if kind:
            sql += " AND t.kind=?"
            params.append(kind)
        if company_id:
            sql += " AND t.company_id=?"
            params.append(company_id)
        sql += " ORDER BY t.occurred_on DESC, t.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await self.fetchall(sql, params)]

    # ---------- revenue statements (Commission / Investment / General) ----------
    async def add_revenue(self, actor_id, company_id, category, amount, description, occurred_on,
                          *, received_from="", method="", transaction_id="", remarks="",
                          recorded_by=""):
        """Record a revenue statement entry: unique REV id, company, category, amount, date, staff.

        The payment details mirror an expense entry (who sent it, how it was sent,
        the reference and remarks), so ``/revenue`` captures the same fields as
        ``/expense add``. They are optional so existing callers keep working.
        """
        if category not in REVENUE_CATEGORIES:
            raise ValueError("Category must be one of: " + ", ".join(REVENUE_CATEGORIES) + ".")
        if amount <= 0:
            raise ValueError("Amount must be above zero.")
        async with self.lock:
            cur = await self.conn.execute(
                """INSERT INTO revenue(company_id, category, amount, description, received_from, method,
                   transaction_id, remarks, recorded_by, occurred_on, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                ((company_id or None), category, amount, (description or "")[:200],
                 (received_from or "")[:100], (method or "")[:40], (transaction_id or "")[:100],
                 (remarks or "")[:200], (recorded_by or "")[:100], occurred_on, actor_id),
            )
            await self._audit(actor_id, "revenue.add", "revenue", cur.lastrowid,
                              {"company": company_id, "category": category, "amount": amount,
                               "date": occurred_on})
            await self.conn.commit()
            return cur.lastrowid

    async def get_revenue(self, revenue_id: int):
        row = await self.fetchone("SELECT * FROM revenue WHERE id=?", (revenue_id,))
        return dict(row) if row else None

    async def void_revenue(self, actor_id, revenue_id: int, reason: str):
        """Void a Revenue Statement: it stays in the books for audit but leaves every total."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("A reason is required.")
        async with self.lock:
            row = await self.fetchone("SELECT * FROM revenue WHERE id=?", (revenue_id,))
            if not row:
                raise ValueError("Revenue statement not found.")
            if row["voided"]:
                raise ValueError("Already void.")
            await self.conn.execute(
                "UPDATE revenue SET voided=1, void_reason=?, voided_at=? WHERE id=?",
                (reason[:200], dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 revenue_id),
            )
            await self._audit(actor_id, "revenue.void", "revenue", revenue_id,
                              {"reason": reason, "amount": row["amount"],
                               "company": row["company_id"]})
            await self.conn.commit()
            return dict(await self.fetchone("SELECT * FROM revenue WHERE id=?", (revenue_id,)))

    async def recent_revenue(self, company_id: str | None = None, limit: int = 15,
                             include_voided: bool = False):
        """Newest Revenue Statements, with the ids the void command takes."""
        sql = "SELECT * FROM revenue WHERE 1=1"
        params: list = []
        if company_id:
            sql += " AND company_id=?"
            params.append(company_id)
        if not include_voided:
            sql += " AND voided=0"
        sql += " ORDER BY occurred_on DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await self.fetchall(sql, params)]

    async def revenue_totals(self, month: str, company_id: str | None = None) -> dict:
        """Per-category totals for a month (missing categories are absent from the dict)."""
        sql = ("SELECT category, COALESCE(SUM(amount),0) AS total FROM revenue "
               "WHERE voided=0 AND substr(occurred_on,1,7)=?")
        params: list = [month]
        if company_id:
            sql += " AND company_id=?"
            params.append(company_id)
        sql += " GROUP BY category"
        return {r["category"]: r["total"] for r in await self.fetchall(sql, params)}

    async def list_revenue(self, month: str, company_id: str | None = None, limit: int = 25):
        sql = "SELECT * FROM revenue WHERE voided=0 AND substr(occurred_on,1,7)=?"
        params: list = [month]
        if company_id:
            sql += " AND company_id=?"
            params.append(company_id)
        sql += " ORDER BY occurred_on DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await self.fetchall(sql, params)]

    async def revenue_summary(self, company_id: str | None = None) -> dict:
        """All-time revenue statement totals, split the way the reports need them.

        ``operating`` is Commission + General (real revenue); ``investment`` is
        capital and is never added to revenue or profit.
        """
        sql = "SELECT category, COALESCE(SUM(amount),0) AS total FROM revenue WHERE voided=0"
        params: list = []
        if company_id:
            sql += " AND company_id=?"
            params.append(company_id)
        sql += " GROUP BY category"
        totals = {r["category"]: r["total"] for r in await self.fetchall(sql, params)}
        return {**{name: totals.get(name, 0) for name in REVENUE_CATEGORIES},
                "operating": sum(v for k, v in totals.items() if k != "Investment"),
                "investment": totals.get("Investment", 0)}

    async def month_report(self, month: str, company_id: str | None = None) -> dict:
        """Income, expenses and revenue for a month, optionally for one company.

        ``revenue`` is *operating* revenue (invoice/manual income plus Commission
        and General revenue statements). Investment contributions are reported in
        ``revenue_rows``/``investment`` only, so they never inflate revenue or
        profit, and no entry is ever counted twice.
        """
        where = "t.voided=0 AND substr(t.occurred_on,1,7)=?"
        params: list = [month]
        if company_id:
            where += " AND t.company_id=?"
            params.append(company_id)
        rows = await self.fetchall(
            f"""SELECT c.name, t.kind, SUM(t.amount) AS total FROM transactions t
               JOIN categories c ON c.id=t.category_id
               WHERE {where}
               GROUP BY c.name, t.kind ORDER BY total DESC""",
            params,
        )
        income = [(r["name"], r["total"]) for r in rows if r["kind"] == "income"]
        expenses = [(r["name"], r["total"]) for r in rows if r["kind"] == "expense"]
        rev = await self.revenue_totals(month, company_id)
        investment = rev.get("Investment", 0)
        # Everything recorded through /revenue except Investment is operating revenue.
        statement_revenue = sum(v for k, v in rev.items() if k != "Investment")
        income_total = sum(t for _, t in income)
        revenue = income_total + statement_revenue
        spent = sum(t for _, t in expenses)
        return {"month": month, "company": company_id,
                "income": income, "income_total": income_total, "expenses": expenses,
                "statement_rows": [(name, rev.get(name, 0)) for name in REVENUE_CATEGORIES
                                   if name != "Investment"],
                "statement_total": statement_revenue,
                "revenue": revenue, "expense_total": spent, "net": revenue - spent,
                "revenue_rows": [(name, rev.get(name, 0)) for name in REVENUE_CATEGORIES],
                "revenue_total": sum(rev.values()), "investment": investment}

    # ---------- permanent transaction ids (SALE-xxxxxx / REV-xxxxxx) ----------
    async def register_statement(self, txn_id, kind, company_id, record_id, *,
                                 guild_id=None, channel_id=None, message_id=None):
        """Remember a transaction id, its record and the Discord message that announced it.

        Safe to call again: the Discord ids are only filled in, never wiped.
        """
        async with self.lock:
            await self.conn.execute(
                """INSERT INTO statement_txns(txn_id, kind, company_id, record_id,
                   guild_id, channel_id, message_id) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(txn_id) DO UPDATE SET
                     guild_id=COALESCE(excluded.guild_id, guild_id),
                     channel_id=COALESCE(excluded.channel_id, channel_id),
                     message_id=COALESCE(excluded.message_id, message_id)""",
                (txn_id, kind, (company_id or None), record_id, guild_id, channel_id, message_id),
            )
            await self.conn.commit()

    async def statement_txn(self, txn_id: str):
        row = await self.fetchone("SELECT * FROM statement_txns WHERE txn_id=?", (txn_id,))
        return dict(row) if row else None

    async def backfill_statement_txns(self, sales) -> int:
        """Give every existing sale/revenue record a registry row (Discord ids stay empty).

        ``sales`` are the ledger payments (each already carries its ``sale_id``). Returns the
        number of rows added; running it again adds nothing.
        """
        async with self.lock:
            before = self.conn.total_changes
            for sale in sales:
                if sale.get("sale_id") and sale.get("id"):
                    await self.conn.execute(
                        "INSERT OR IGNORE INTO statement_txns(txn_id, kind, company_id, record_id) "
                        "VALUES (?,?,?,?)",
                        (sale["sale_id"], "sale", (sale.get("company") or None), sale["id"]))
            for row in await self.fetchall("SELECT id, company_id FROM revenue"):
                await self.conn.execute(
                    "INSERT OR IGNORE INTO statement_txns(txn_id, kind, company_id, record_id) "
                    "VALUES (?,?,?,?)",
                    (revenue_number(row["id"]), "revenue", row["company_id"], row["id"]))
            await self.conn.commit()
            return self.conn.total_changes - before

    async def record_statement_delete(self, actor_id, txn_id, kind, company_id, record_id,
                                      amount_cents, reason, details=None):
        """Stamp who deleted a transaction, when and why, and write the audit line (one commit)."""
        reason = (reason or "").strip()
        async with self.lock:
            await self.conn.execute(
                """INSERT INTO statement_txns(txn_id, kind, company_id, record_id) VALUES (?,?,?,?)
                   ON CONFLICT(txn_id) DO NOTHING""",
                (txn_id, kind, (company_id or None), record_id))
            await self.conn.execute(
                """UPDATE statement_txns SET deleted_by=?, deleted_at=?, delete_reason=?
                   WHERE txn_id=? AND deleted_at IS NULL""",
                (actor_id, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 reason[:200], txn_id))
            await self._audit(actor_id, "statement.delete", kind, record_id,
                              {"txn_id": txn_id, "company": company_id, "amount": amount_cents,
                               "reason": reason, **(details or {})})
            await self.conn.commit()

    async def set_statement_message_state(self, actor_id, txn_id, state: str):
        """Remember what happened to the Discord statement: removed / missing / failed / untracked."""
        async with self.lock:
            await self.conn.execute(
                "UPDATE statement_txns SET message_state=? WHERE txn_id=?", (state, txn_id))
            await self._audit(actor_id, f"statement.message_{state}", "statement", None,
                              {"txn_id": txn_id})
            await self.conn.commit()

    # ---------- giveaways ----------
    async def create_giveaway(self, actor_id, *, guild_id, channel_id, prize, winners, required_role_id,
                              ends_at):
        async with self.lock:
            cur = await self.conn.execute(
                """INSERT INTO giveaways(guild_id, channel_id, prize, winners, required_role_id,
                   host_id, ends_at) VALUES (?,?,?,?,?,?,?)""",
                (guild_id, channel_id, prize[:200], max(int(winners), 1), required_role_id,
                 actor_id, ends_at),
            )
            giveaway_id = cur.lastrowid
            await self._audit(actor_id, "giveaway.create", "giveaway", giveaway_id,
                              {"prize": prize, "winners": winners, "ends_at": ends_at,
                               "role": required_role_id})
            await self.conn.commit()
            return giveaway_id

    async def set_giveaway_message(self, giveaway_id, message_id):
        async with self.lock:
            await self.conn.execute("UPDATE giveaways SET message_id=? WHERE id=?", (message_id, giveaway_id))
            await self.conn.commit()

    async def get_giveaway(self, giveaway_id: int):
        row = await self.fetchone("SELECT * FROM giveaways WHERE id=?", (giveaway_id,))
        return dict(row) if row else None

    async def open_giveaways(self):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM giveaways WHERE status='open' ORDER BY ends_at ASC")]

    async def giveaway_entry_count(self, giveaway_id: int) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM giveaway_entries WHERE giveaway_id=?", (giveaway_id,))
        return row["n"] if row else 0

    async def enter_giveaway(self, giveaway_id: int, user_id: int) -> bool:
        """Add one entry. False when that member already entered (duplicate guard)."""
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    "INSERT OR IGNORE INTO giveaway_entries(giveaway_id, user_id) VALUES (?,?)",
                    (giveaway_id, user_id),
                )
            except aiosqlite.IntegrityError:
                await self.conn.rollback()
                return False
            await self.conn.commit()
            return cur.rowcount == 1

    async def giveaway_entries(self, giveaway_id: int):
        return [r["user_id"] for r in await self.fetchall(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id=? ORDER BY id", (giveaway_id,))]

    async def giveaway_winners(self, giveaway_id: int):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM giveaway_winners WHERE giveaway_id=? ORDER BY id", (giveaway_id,))]

    async def finish_giveaway(self, actor_id, giveaway_id, winner_ids) -> bool:
        """Close a giveaway exactly once and record the drawn winners."""
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE giveaways SET status='ended', ended_at=datetime('now') "
                "WHERE id=? AND status='open'", (giveaway_id,))
            if cur.rowcount != 1:
                await self.conn.rollback()
                return False
            for user_id in winner_ids:
                await self.conn.execute(
                    "INSERT INTO giveaway_winners(giveaway_id, user_id) VALUES (?,?)",
                    (giveaway_id, user_id))
            await self._audit(actor_id, "giveaway.end", "giveaway", giveaway_id,
                              {"winners": list(winner_ids)})
            await self.conn.commit()
        return True

    async def add_giveaway_winner(self, actor_id, giveaway_id, user_id):
        """Record a rerolled winner on an already-ended giveaway."""
        async with self.lock:
            await self.conn.execute(
                "INSERT INTO giveaway_winners(giveaway_id, user_id) VALUES (?,?)",
                (giveaway_id, user_id))
            await self._audit(actor_id, "giveaway.reroll", "giveaway", giveaway_id, {"winner": user_id})
            await self.conn.commit()

    # ---------- services (expiry management) ----------
    #: a reminder that failed for a temporary Discord reason is retried this many times in total
    SERVICE_REMINDER_ATTEMPTS = 3

    async def create_service(self, actor_id, *, guild_id, client_discord_id, client_name, service_name,
                             server_ref, start_date, expiry_date, ticket_channel_id=None):
        """Register a service. Returns its id, or None when that server is already tracked as active."""
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    """INSERT INTO services(guild_id, client_discord_id, client_name, service_name,
                       server_ref, start_date, expiry_date, ticket_channel_id, created_by)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (guild_id, client_discord_id, client_name[:100], service_name[:100],
                     server_ref[:100], start_date, expiry_date, ticket_channel_id, actor_id))
            except aiosqlite.IntegrityError:
                await self.conn.rollback()
                return None
            service_id = cur.lastrowid
            await self._audit(actor_id, "service.create", "service", service_id,
                              {"client": client_discord_id, "service": service_name, "server": server_ref,
                               "start": start_date, "expiry": expiry_date, "ticket_channel": ticket_channel_id})
            await self.conn.commit()
            return service_id

    async def get_service(self, service_id: int):
        row = await self.fetchone("SELECT * FROM services WHERE id=?", (service_id,))
        return dict(row) if row else None

    async def find_services(self, guild_id: int, query: str, limit: int = 10):
        """Look a service up by id (``12`` / ``SVC-0012``) or by server name/UUID (exact, then 6+ char prefix)."""
        text = (query or "").strip()
        if not text:
            return []
        match = re.fullmatch(r"(?i)(?:svc-?)?0*(\d+)", text)
        if match:
            row = await self.fetchone("SELECT * FROM services WHERE id=? AND guild_id=?",
                                      (int(match.group(1)), guild_id))
            if row:
                return [dict(row)]
        rows = [dict(r) for r in await self.fetchall(
            "SELECT * FROM services WHERE guild_id=? AND lower(server_ref)=lower(?) ORDER BY id DESC",
            (guild_id, text))]
        if not rows and len(text) >= 6:
            rows = [dict(r) for r in await self.fetchall(
                "SELECT * FROM services WHERE guild_id=? AND server_ref LIKE ? ESCAPE '\\' "
                "ORDER BY id DESC LIMIT ?", (guild_id, _like(text) + "%", limit))]
        active = [r for r in rows if r["status"] == "active"]
        return active if active and len(rows) > 1 else rows

    async def search_services(self, guild_id: int, text: str, limit: int = 25):
        """Autocomplete: active services first (soonest expiry), matching client / service / server / id."""
        like = f"%{_like(text)}%"
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM services WHERE guild_id=? AND ("
            "client_name LIKE ? ESCAPE '\\' OR service_name LIKE ? ESCAPE '\\' "
            "OR server_ref LIKE ? ESCAPE '\\' OR printf('SVC-%04d', id) LIKE ? ESCAPE '\\') "
            "ORDER BY (status='active') DESC, expiry_date ASC, id ASC LIMIT ?",
            (guild_id, like, like, like, like, limit))]

    async def list_services(self, guild_id: int, *, status: str = "active", until: str | None = None):
        sql, params = "SELECT * FROM services WHERE guild_id=? AND status=?", [guild_id, status]
        if until:
            sql += " AND expiry_date<=?"
            params.append(until)
        return [dict(r) for r in await self.fetchall(sql + " ORDER BY expiry_date ASC, id ASC", params)]

    async def ticket_by_number(self, guild_id: int, number: int):
        """A ticket by the number staff see (per-server sequence; legacy tickets use their row id)."""
        row = await self.fetchone(
            "SELECT * FROM tickets WHERE guild_id=? AND (guild_seq=? OR (guild_seq IS NULL AND id=?)) "
            "ORDER BY id DESC LIMIT 1", (guild_id, number, number))
        return dict(row) if row else None

    async def change_service_expiry(self, actor_id, service_id, new_expiry, reason, today_iso):
        """Move an active service's expiry. Returns the change, or None (missing / cancelled).

        Reminders are keyed by (service, expiry date, days left), so the new date starts a fresh
        schedule by itself. When today is one of the reminder days for the new date, that slot is
        claimed for the change notice (``claimed``) so the client is not messaged twice in a day.
        """
        async with self.lock:
            row = await self.fetchone("SELECT * FROM services WHERE id=? AND status='active'", (service_id,))
            if row is None:
                return None
            previous = row["expiry_date"]
            delta = (dt.date.fromisoformat(new_expiry) - dt.date.fromisoformat(previous)).days
            left = (dt.date.fromisoformat(new_expiry) - dt.date.fromisoformat(today_iso)).days
            await self.conn.execute(
                "UPDATE services SET expiry_date=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_expiry, service_id))
            await self.conn.execute(
                "INSERT INTO service_expiry_log(service_id, previous_expiry, new_expiry, days_changed, reason, "
                "changed_by) VALUES (?,?,?,?,?,?)",
                (service_id, previous, new_expiry, delta, (reason or "")[:300] or None, actor_id))
            claimed = False
            if 0 <= left <= 5:
                cur = await self.conn.execute(
                    "INSERT OR IGNORE INTO service_reminders(service_id, expiry_date, days_left, kind, state, attempts) "
                    "VALUES (?,?,?,'expiry_change','pending',1)", (service_id, new_expiry, left))
                claimed = cur.rowcount == 1
            await self._audit(actor_id, "service.expiry", "service", service_id,
                              {"previous": previous, "new": new_expiry, "days": delta, "reason": reason})
            await self.conn.commit()
            return {"previous": previous, "new": new_expiry, "days_changed": delta, "days_left": left,
                    "claimed": claimed}

    async def finish_service_notice(self, service_id, expiry_date, days_left, dm_status, ticket_status):
        """Settle the slot claimed by an expiry change: kept when the client was told, freed otherwise."""
        async with self.lock:
            if "sent" in (dm_status, ticket_status):
                await self.conn.execute(
                    "UPDATE service_reminders SET state='done', dm_status=?, ticket_status=?, "
                    "sent_at=CURRENT_TIMESTAMP WHERE service_id=? AND expiry_date=? AND days_left=? "
                    "AND kind='expiry_change'", (dm_status, ticket_status, service_id, expiry_date, days_left))
            else:  # nothing reached the client: let the normal reminder go out today
                await self.conn.execute(
                    "DELETE FROM service_reminders WHERE service_id=? AND expiry_date=? AND days_left=? "
                    "AND kind='expiry_change' AND state='pending'", (service_id, expiry_date, days_left))
            await self.conn.commit()

    async def cancel_service(self, actor_id, service_id, reason=None) -> bool:
        """Stop reminders. The row, its expiry history and reminder history stay."""
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE services SET status='cancelled', cancel_reason=?, cancelled_by=?, "
                "cancelled_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'",
                ((reason or "")[:300] or None, actor_id, service_id))
            if cur.rowcount != 1:
                await self.conn.rollback()
                return False
            await self._audit(actor_id, "service.cancel", "service", service_id, {"reason": reason})
            await self.conn.commit()
            return True

    async def due_service_candidates(self, today_iso: str, horizon: int = 5):
        """Active services expiring today or within ``horizon`` days (the reminder window)."""
        last = (dt.date.fromisoformat(today_iso) + dt.timedelta(days=horizon)).isoformat()
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM services WHERE status='active' AND expiry_date BETWEEN ? AND ? "
            "ORDER BY expiry_date ASC, id ASC", (today_iso, last))]

    async def claim_service_reminder(self, service_id, expiry_date, days_left) -> bool:
        """Reserve today's reminder before sending it. False = already sent / being sent (restart-safe)."""
        async with self.lock:
            live = await self.fetchone(
                "SELECT 1 FROM services WHERE id=? AND status='active' AND expiry_date=?",
                (service_id, expiry_date))
            if live is None:  # cancelled or rescheduled since it was picked
                return False
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO service_reminders(service_id, expiry_date, days_left, kind, state, attempts) "
                "VALUES (?,?,?,'reminder','pending',1)", (service_id, expiry_date, days_left))
            if cur.rowcount != 1:  # a temporary failure may be retried a few times
                cur = await self.conn.execute(
                    "UPDATE service_reminders SET state='pending', attempts=attempts+1 WHERE service_id=? "
                    "AND expiry_date=? AND days_left=? AND kind='reminder' AND state='retry' AND attempts<?",
                    (service_id, expiry_date, days_left, self.SERVICE_REMINDER_ATTEMPTS))
            await self.conn.commit()
            return cur.rowcount == 1

    async def finish_service_reminder(self, service_id, expiry_date, days_left, dm_status, ticket_status,
                                      retry: bool = False):
        async with self.lock:
            await self.conn.execute(
                "UPDATE service_reminders SET state=?, dm_status=?, ticket_status=?, sent_at=CURRENT_TIMESTAMP "
                "WHERE service_id=? AND expiry_date=? AND days_left=? AND kind='reminder'",
                ("retry" if retry else "done", dm_status, ticket_status, service_id, expiry_date, days_left))
            await self.conn.commit()

    async def service_expiry_history(self, service_id: int, limit: int = 5):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM service_expiry_log WHERE service_id=? ORDER BY id DESC LIMIT ?", (service_id, limit))]

    async def service_reminder_history(self, service_id: int, limit: int = 8):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM service_reminders WHERE service_id=? AND state!='pending' ORDER BY id DESC LIMIT ?",
            (service_id, limit))]

    # ---------- audit ----------
    async def recent_audit(self, limit: int = 15):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]
