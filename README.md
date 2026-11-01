# Invoice, Ticket & Sales Bot (Discord Components V2)

One bot with two halves that share the same flat Components V2 design
(`## Title`, divider, `**Label:** value` fields, `-#` footer, buttons at the bottom):

* **Statements ledger** (`data.json`): quick invoices, **Sales Statements** and **Expense Statements**, each showing the running total.
* **Desk** (`data/bot.db`, folder `desk/`): itemised PDF invoices, tickets, sales pipeline, income/expense books, dashboard and P&L.

They are kept in step automatically (see "How they connect").

## Setup (no .env file)

1. Open `config.py` and fill the **EDIT THESE** block: `BOT_TOKEN`, `GUILD_ID`, `INVOICE_CHANNEL_ID`,
   `STATEMENTS_CHANNEL_ID`, `STAFF_ROLE_IDS`, and the ticket / business settings you need.
   (Panel variables with the same names override the file.)
   Then fill in the three **company** channels described below — money commands refuse to save until
   the chosen company has a statement channel.
2. Pterodactyl: **App py file** = `bot.py` (or `app.py`), **Requirements file** = `requirements.txt`.
   If requirements were not installed, the bot installs any missing package itself on start.
3. Optional but recommended: Developer Portal → Bot → enable **Message Content Intent** (ticket transcripts include text).
   Without it the bot still starts; transcripts just have no message text.
4. Invite with scopes `bot` + `applications.commands`.

Staff commands need **Administrator** or a role listed in `STAFF_ROLE_IDS`.

## Channels

| Setting | Receives |
|---|---|
| `COMPANY_ZARBUNEX_STATEMENT_CHANNEL_ID` | Sales / Expense / Revenue Statements for **Zarbunex PVT LTD** |
| `COMPANY_HECTORHOST_STATEMENT_CHANNEL_ID` | Sales / Expense / Revenue Statements for **HectorHost** |
| `COMPANY_MATRIXMC_STATEMENT_CHANNEL_ID` | Sales / Expense / Revenue Statements for **MatrixMC** |
| `COMPANY_*_INVOICE_CHANNEL_ID` | optional: that company's invoice cards (defaults to its statement channel) |
| `INVOICE_CHANNEL_ID` | only invoices that predate companies (legacy rows) |
| `STATEMENTS_CHANNEL_ID` | only legacy statements (records with no company) |
| `TICKET_LOG_CHANNEL_ID` | transcripts, close/reopen logs, stale-ticket digest |
| `SALES_CHANNEL_ID` | follow-up reminders |
| `TRANSACTION_CHANNEL_IDS` | optional extra copies of invoice / payment cards |

## Ticket departments (staff roles per category)

Every ticket category has its own staff roles, so a department sees and manages only
its own tickets. Set the ids in `config.py` or as panel variables (comma separated):

| Variable | Category |
|---|---|
| `TICKET_DEFAULT_STAFF_ROLE_IDS` | Tickets (the default category) |
| `TICKET_BILLING_STAFF_ROLE_IDS` | Billing |
| `TICKET_SUPPORT_STAFF_ROLE_IDS` | Technical Support |
| `TICKET_PARTNERSHIP_STAFF_ROLE_IDS` | Partnership |
| `TICKET_RECRUITMENT_STAFF_ROLE_IDS` | Recruitment |
| `TICKET_ACTIVE_CLIENTS_STAFF_ROLE_IDS` | Active Clients |
| `TICKET_OUTDATED_CLIENTS_STAFF_ROLE_IDS` | Outdated Clients |
| `TICKET_FOLLOWUP_STAFF_ROLE_IDS` | Follow Up |
| `TICKET_STAFF_ROLE_IDS` | every ticket (supervisors) |
| `TICKET_CROSS_DEPARTMENT_ROLE_IDS` | every ticket, never removed by a move |

Those roles sit alongside the category ids you already set (`TICKET_DEFAULT_CATEGORY_ID`,
`TICKET_CATEGORY_ID`, `TICKET_SUPPORT_CATEGORY_ID`, `TICKET_PARTNERSHIP_CATEGORY_ID`,
`TICKET_RECRUITMENT_CATEGORY_ID`, `TICKET_ACTIVE_CLIENTS_CATEGORY_ID`,
`TICKET_OUTDATED_CLIENTS_CATEGORY_ID`, `TICKET_FOLLOWUP_CATEGORY_ID`, `TICKET_CLOSED_CATEGORY_ID`).

A category left at `""` behaves exactly as before (any `TICKET_STAFF_ROLE_IDS` member can
manage it). Administrators, the ticket owner and the bot always keep access, and a move
hands access from the previous department to the destination one. The channel permission
overwrites are the enforcement point, so an unrelated department cannot even read the
ticket, and every button/modal re-checks the caller's department on the server side.

### Multiple servers (one ticket system per server)

To run the ticket system in more than one server, copy `ticket_guilds.example.json` to
`ticket_guilds.json` next to `config.py` and give every server its own block, keyed by its
server (guild) id:

```json
{
  "111111111111111111": {
    "name": "First server",
    "panel_channel_id": 0,
    "panel_title": "Need Hosting? Create a Ticket",
    "panel_description": "Create a ticket and our team will assist you.",
    "panel_button": "Create Ticket",
    "staff_role_ids": [],
    "cross_department_role_ids": [],
    "log_channel_id": 0,
    "closed_category_id": 0,
    "prefix": "ticket",
    "closed_prefix": "closed",
    "stale_days": 3,
    "max_open_per_user": 3,
    "categories": {
      "tickets": 0, "billing": 0, "support": 0, "partnership": 0,
      "recruitment": 0, "active": 0, "outdated": 0, "followup": 0
    },
    "department_roles": {
      "tickets": [], "billing": [], "support": [], "partnership": [],
      "recruitment": [], "active": [], "outdated": [], "followup": []
    }
  },
  "222222222222222222": { "...": "same shape, with that server's ids" }
}
```

* Each server has **its own** categories, department staff roles, panel / log / closed
  channels, ticket prefix, number sequence (`ticket-0001` in both servers), stale-ticket
  age and open-ticket limit.
* A category, channel or role id is only ever resolved inside the server the command ran
  in, so one server can never use another server's ids. A server that is **not** listed in
  `ticket_guilds.json` has no ticket system: `/ticket open` and the panel say so instead of
  falling back to someone else's setup.
* When the file exists it replaces the flat `TICKET_*` settings above. Without it those
  settings still apply to every server, so an existing single-server install needs no
  change at all.
* `/ticket setup` shows the resolved setup for the current server plus every missing or
  invalid category / role / channel id; the same problems are logged at startup.
* `/ticket panel` posts that server's panel to its `panel_channel_id` unless you pass `channel:`.
  The wording is per server: set `panel_title`, `panel_description` (use `\n` for line
  breaks) and `panel_button` in that server's block. To post a one-off version without
  touching the file, pass them to the command: `/ticket panel title:… description:… button:…`
  — it still only posts in the server the command runs in.
* Existing tickets are attached to their server on the first start (`guild_id`), numbered
  per server, and their history/notes/audit lines record that server too. The database is
  migrated in place (new `guild_id` / `guild_seq` columns); nothing is deleted or renumbered
  twice.

## Commands

**Statements** (all company-scoped: pick `company:` every time)

| Command | What it does |
|---|---|
| `/sale add` | Record a sale → posts a **Sales Statement** to that company's channel (Company, Date, Client Name/Contact, Server Name/UUID, Service/Plan, Amount Received, Payment Method, Transaction ID, Remarks, **Total Sales** for that company) |
| `/expense add` | Record an expense → posts an **Expense Statement** to that company's channel (Company, Date, Description, Category, Amount, Payment Method, Paid To, Transaction ID, Remarks, **Total Expenses** for that company) |
| `/revenue` | Record revenue the same way `/expense add` works: `company`, `category` (`Commission` / `Investment` / `General`), `amount`, `description`, `received_from`, plus optional `method`, `transaction_id`, `remarks`, `date`. Posts a **Revenue Statement** (Transaction ID `REV-0001`, Company, Date, Category, Amount, Received From, Payment Method, Transaction ID / Ref, Description, Remarks, Recorded By, that company's **Total Revenue**) to that company's channel only |
| `/expense-list [company] [include_voided]` | Recent Expense Statements with the `EXP-0001` id each one can be voided by |
| `/expense-void entry reason` | Void a wrong Expense Statement (`entry` is `EXP-0003` or `3`). It keeps its record and audit trail but leaves the expense totals, the reports and the P&L, its finance entry is voided too, and a void notice is posted to that company's channel |
| `/revenue-list [company] [include_voided]` | Recent Revenue Statements with the `REV-0001` id each one can be voided by |
| `/revenue-void entry reason` | Void a wrong Revenue Statement (`entry` is `REV-0003` or `3`). It stays in the database and audit log but drops out of every total, the reports and the P&L, and a void notice is posted to that company's channel |
| `/statement sales`, `/statement expenses` | Per-company period report (today / 7 days / 30 days / all time) with the period total |

**Invoices** (`/invoice ...`)

| Command | What it does |
|---|---|
| `create` | Quick invoice form for a **required company** (client, contact, amount, service/plan, due). Its **Mark as paid** button asks for method / transaction ID / server / UUID / remarks, then posts a Sales Statement for the invoice's company |
| `create-detailed` | Itemised PDF invoice for a **required company** (items form also shows the company, preselected from the ticket when it has one) |
| `list [status] [month]` | Quick and itemised invoices together |
| `view`, `pdf`, `pay`, `void`, `send-ticket`, `deliver` | Itemised invoice tools (payments ask for method + transaction ID) |

Itemised item line: `name | quantity | unit price | type=VPS; server=Hector-VPS-12; node=Ryzen; plan=4GB; id=HX-1122; link=https://...`

**Tickets** (`/ticket ...`): `open`, `panel`, `setup`, `info`, `list`, `claim`, `assign`, `note`, `priority`, `company`, `close`, `reopen`, `transcript`, `dashboard`.
All of them use the ticket setup of the server the command runs in (`ticket_guilds.json`, or the flat `TICKET_*` settings when that file is absent).
Panels have buttons: Claim, Create Invoice, Linked Ticket, Add Note, Transcript, Sales, Close/Reopen and Move.
The dashboard shows the category, subject, client, status, priority and history — not the ticket type, the
financial company or the sales-pipeline line (companies belong to the invoice / finance flows, `/ticket company`
still files a ticket under one, and the **Sales** button opens the deal).
**Move** opens one form asking for the **destination category** and a **reason** (the channel name is optional):
the same channel moves with its messages, history and owner, the reason is saved as a timestamped dashboard note
(including who moved it) and in the move history, the destination department gains access while the previous one
loses it, and the dashboard refreshes in place. There is one ticket-movement action — no separate "Complete Purchase".
`/ticket company` files a ticket under a company; invoices raised from that ticket preselect it, and
moving the ticket between Discord categories never changes it.

**Sales pipeline** (`/sales ...`): `pipeline`, `list`, `deal`, `ticket`, `add`, `stage`, `followup`, `note`, `won`, `lost`, `due`.
Purchase / enquiry tickets create deals automatically; invoices move them to Quoted, full payment marks them Won.

**Announcements**

| Command | What it does |
|---|---|
| `/embed` | Post a card (`## title`, message body, `-# footer`) in the chosen channel. `channel`, `title` and `footer` are command options; the message itself is typed in a form, where title and footer can still be edited. Staff only, never pings anyone |

**Giveaways** (`/giveaway ...`)

| Command | What it does |
|---|---|
| `create` | Post a giveaway card in a channel: prize, winners, duration in minutes, optional required role. The card shows a live entry count and a Discord countdown; the **Enter giveaway** button adds one entry per member |
| `end` | Draw the winners immediately instead of waiting |
| `reroll` | Pick a replacement winner after the giveaway ended (never repeats a previous winner) |

Giveaways, entries and winners live in `data/bot.db`, so entries and running giveaways survive a restart;
the draw happens automatically within ~30 seconds of the end time and pings only the winners.

**Finance** (`/finance ...`): `income`, `expense`, `category-add`, `transactions`, `void`, `audit`, `dashboard`, `pnl`.
`income`/`expense` require a company; `transactions`, `dashboard` and `pnl` accept an optional company
(omitted = all companies). Revenue recorded with `/revenue` belongs to that company's P&L for its own month:
Operating Revenue is split into the recorded income categories and the `Commission` / `General` revenue
statement lines. `Investment` is never revenue or profit, but it is not hidden either: the dashboard and the
P&L PDF give it its own line with its own **Total investment**, and both show a
**Total received (revenue + investment)** beside the revenue-only totals.

Wrong entries are cancelled, never deleted: `/expense-void <EXP id> <reason>` reverses an Expense
Statement (find the id with `/expense-list`), `/revenue-void` reverses a Revenue Statement and
`/finance void <transaction id> <reason>` reverses an income/expense entry (find the id with
`/finance transactions`). A voided entry keeps its record and audit-log line but is left out of the
dashboard, the P&L, the statement totals and the period statements — `/finance void` also cancels
the matching Sales / Expense statement entry, so the two books never disagree. To correct a
miscategorised entry, void it and record it again.

## Companies (multi-company finance)

Three companies are configured in `config.py` → `COMPANY_CONFIG` (keys `zarbunex`, `hectorhost`,
`matrixmc`). Every sale, expense, revenue entry, invoice, payment transaction and ticket stores the
company key, and each company's statements go to its own channel only — which may be in a different
server.

1. Create one statements channel per company (in whichever server you want) and, optionally, a
   separate invoices channel per company.
2. Invite the bot to **each** server with `bot` + `applications.commands` and the permissions from
   `bot.py` (`INVITE_PERMISSIONS`), and give it **View Channel** + **Send Messages** in those channels.
3. Set the ids as panel / environment variables (or edit `COMPANY_CONFIG`):

```
COMPANY_ZARBUNEX_STATEMENT_CHANNEL_ID=...
COMPANY_HECTORHOST_STATEMENT_CHANNEL_ID=...
COMPANY_MATRIXMC_STATEMENT_CHANNEL_ID=...
COMPANY_ZARBUNEX_INVOICE_CHANNEL_ID=...      # optional
COMPANY_HECTORHOST_INVOICE_CHANNEL_ID=...    # optional
COMPANY_MATRIXMC_INVOICE_CHANNEL_ID=...      # optional
COMPANY_ZARBUNEX_NAME=...                    # optional display-name override
```

A company with no channel is simply unavailable: commands reply with the exact variable to set, and
nothing is saved. There is deliberately **no default company** — every financial command asks for one.

Existing `data/bot.db` files are upgraded in place on start (new `company_id` columns and a `revenue`
table); old rows keep their values with `company_id` empty and still show under "all companies" reports.

## How they connect

* A payment on an itemised invoice → Sales Statement in the statements channel + ledger entry.
* A new itemised invoice → card in the invoice channel.
* `/sale add`, `/expense add` and quick-invoice payments → also recorded in the books, so `/finance dashboard` and the P&L include them.
* `/finance income` and `/finance expense` → also posted as Sales / Expense Statements.

So **Total Sales** and **Total Expenses** on statements cover everything.

## Files

```
bot.py  app.py  config.py  data.py  ui.py  requirements.txt
cogs/finance.py     /sale /expense /revenue /statement
cogs/invoices.py    quick invoice form
desk/               database, invoices, finance, tickets, sales, views, bridge, pdf_gen, utils
logo.png  NotoSans-*.ttf   invoice PDF branding (replace logo.png with yours)
tests/              python -m unittest discover -s tests -t .
```

Back up `data.json` and the `data/` folder (database + transcripts); never wipe them on reinstall.
The migration only adds columns/tables, so restoring an old `data/bot.db` into the new version is safe.
