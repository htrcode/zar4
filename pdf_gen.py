"""Standard business invoices and Profit & Loss statements (ReportLab, no system dependencies)."""
import datetime as dt
import io
import os
from functools import lru_cache
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, NextPageTemplate,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

import config
from desk.utils import amount_in_words, fmt

W, H = A4
M = 40
CW = W - 2 * M
BOTTOM = 70  # frame bottom (footer sits below)

# ---------- fonts (bundled Noto Sans so the rupee symbol always renders) ----------
_FONT_DIR = next(
    (d for d in (os.path.join(config.BASE_DIR, "fonts"), config.BASE_DIR)
     if os.path.exists(os.path.join(d, "NotoSans-Regular.ttf"))),
    os.path.join(config.BASE_DIR, "fonts"),
)
try:
    pdfmetrics.registerFont(TTFont("Body", os.path.join(_FONT_DIR, "NotoSans-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Body-Bold", os.path.join(_FONT_DIR, "NotoSans-Bold.ttf")))
    pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body", boldItalic="Body-Bold")
    F, FB, HAS_FONT = "Body", "Body-Bold", True
except Exception:
    F, FB, HAS_FONT = "Helvetica", "Helvetica-Bold", False


def money(cents: int, cur: str) -> str:
    s = fmt(cents, cur)
    return s if HAS_FONT else s.replace("₹", "Rs. ")


# ---------- colours ----------
def _hex(value, default):
    try:
        return colors.HexColor(value)
    except Exception:
        return colors.HexColor(default)


ACCENT = _hex(config.BRAND_COLOR, "#6D28D9")
INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6B7280")
HEAD_BG = colors.HexColor("#1F2937")
RULE = colors.HexColor("#E5E7EB")
PANEL = colors.HexColor("#F3F4F6")
STATUS = {"paid": ("PAID", "#15803D"), "partial": ("PARTIALLY PAID", "#B45309"), "unpaid": ("UNPAID", "#1D4ED8"),
          "overdue": ("OVERDUE", "#B91C1C"), "void": ("VOID", "#6B7280")}


def _st(name, **kw):
    base = dict(fontName=F, fontSize=9.5, leading=13, textColor=INK)
    base.update(kw)
    return ParagraphStyle(name, **base)


S_BODY = _st("body")
S_SMALL = _st("small", fontSize=8.5, leading=11.5, textColor=MUTED)
S_LABEL = _st("label", fontName=FB, fontSize=8, leading=10, textColor=MUTED)
S_TH = _st("th", fontName=FB, fontSize=8.5, leading=11, textColor=colors.white)
S_TH_R = _st("thr", fontName=FB, fontSize=8.5, leading=11, textColor=colors.white, alignment=2)
S_R = _st("r", alignment=2)
S_RB = _st("rb", fontName=FB, alignment=2)
S_ITEM = _st("item", fontName=FB, fontSize=10, leading=13)
S_SPEC = _st("spec", fontSize=8.5, leading=11.5, textColor=MUTED, leftIndent=9, bulletIndent=0)


def P(text, style=S_BODY, **kw):
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style, **kw)


def _date(value):
    try:
        d = dt.date.fromisoformat(value)
        return f"{d.day} {d.strftime('%b %Y')}"
    except Exception:
        return value


# ---------- logo ----------
@lru_cache(maxsize=1)
def _logo():
    """(ImageReader, aspect, png_bytes) with transparent margins trimmed, or None."""
    try:
        from PIL import Image as PILImage
        im = PILImage.open(config.LOGO_PATH).convert("RGBA")
        bbox = im.split()[3].point(lambda v: 255 if v > 12 else 0).getbbox()
        if bbox:
            im = im.crop(bbox)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        data = buf.getvalue()
        return ImageReader(io.BytesIO(data)), im.width / im.height, data
    except Exception:
        return None


def _logo_flowable(max_w=120, max_h=52):
    logo = _logo()
    if not logo:
        return None
    _, aspect, data = logo
    w, h = (max_h * aspect, max_h) if max_h * aspect <= max_w else (max_w, max_w / aspect)
    return Image(io.BytesIO(data), width=w, height=h, hAlign="LEFT")


# ---------- page furniture ----------
class NumberedCanvas(rl_canvas.Canvas):
    """Adds 'Page X of Y' to every page."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont(F, 8)
            self.setFillColor(MUTED)
            self.drawRightString(W - M, 30, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _footer(c, note):
    c.saveState()
    c.setStrokeColor(RULE); c.setLineWidth(0.8)
    c.line(M, 46, W - M, 46)
    c.setFont(F, 8); c.setFillColor(MUTED)
    contact = "  |  ".join(x for x in (config.BUSINESS_NAME, config.BUSINESS_EMAIL, config.BUSINESS_PHONE) if x)
    c.drawString(M, 30, contact)
    if note:
        c.drawString(M, 19, note)
    c.restoreState()


def _watermark(c, status):
    if status not in ("paid", "void", "overdue"):
        return
    label, color = STATUS[status]
    c.saveState()
    c.translate(W / 2, H / 2 - 20)
    c.rotate(35)
    c.setFillColor(colors.HexColor(color)); c.setFillAlpha(0.05)
    c.setFont(FB, 120)
    c.drawCentredString(0, 0, label)
    c.restoreState()


def _compact_header(c, right_text):
    c.saveState()
    logo = _logo()
    x = M
    if logo:
        img, aspect, _ = logo
        h = 28
        w = min(h * aspect, 70)
        c.drawImage(img, M, H - 52, w, w / aspect, mask="auto")
        x = M + w + 10
    c.setFillColor(INK); c.setFont(FB, 12)
    c.drawString(x, H - 42, config.BUSINESS_NAME)
    c.setFillColor(ACCENT); c.setFont(FB, 11)
    c.drawRightString(W - M, H - 42, right_text)
    c.setStrokeColor(ACCENT); c.setLineWidth(1.2)
    c.line(M, H - 62, W - M, H - 62)
    c.restoreState()


# ---------- building blocks ----------
def _table(rows, widths, cmds, **kw):
    t = Table(rows, colWidths=widths, **kw)
    t.setStyle(TableStyle(cmds))
    return t


NOPAD = [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
         ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]


def _company_block(inv):
    parts = []
    logo = _logo_flowable()
    if logo:
        parts += [logo, Spacer(1, 6)]
    parts.append(P(config.BUSINESS_NAME, _st("co", fontName=FB, fontSize=14, leading=18)))
    for line in (config.BUSINESS_ADDRESS, f"Phone: {config.BUSINESS_PHONE}" if config.BUSINESS_PHONE else "",
                 f"Email: {config.BUSINESS_EMAIL}" if config.BUSINESS_EMAIL else "", config.BUSINESS_TAX_ID):
        if line:
            parts.append(P(line, S_SMALL))
    return parts


def _meta_block(inv):
    label, color = STATUS[inv["status"]]
    if inv["status"] == "overdue" and inv["is_partial"]:
        label = "OVERDUE (PARTIAL)"
    terms_days = (dt.date.fromisoformat(inv["due_date"]) - dt.date.fromisoformat(inv["issue_date"])).days
    rows = [("Invoice No.", f"#{inv['number']}"),
            ("Company", config.company_name(inv.get("company_id"))),
            ("Invoice Date", _date(inv["issue_date"])),
            ("Due Date", _date(inv["due_date"])),
            ("Payment Terms", f"Net {terms_days} days" if terms_days > 0 else "Due on receipt")]
    data = [[P(a, S_SMALL), P(b, _st("mv", fontName=FB, alignment=2))] for a, b in rows]
    data.append([P("Status", S_SMALL), Paragraph(f'<font color="{color}"><b>{label}</b></font>', _st("ms", alignment=2))])
    meta = _table(data, [92, 138], [("LINEBELOW", (0, 0), (-1, -2), 0.5, RULE), ("TOPPADDING", (0, 0), (-1, -1), 4),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 0)], hAlign="RIGHT")
    title = "TAX INVOICE" if inv["tax_percent"] else "INVOICE"
    return [Paragraph(title, _st("ti", fontName=FB, fontSize=26, leading=30, textColor=ACCENT, alignment=2)),
            Spacer(1, 6), meta]


def _bill_and_due(inv):
    cur = inv["currency"]
    bill = [P("BILL TO", S_LABEL), Spacer(1, 3), P(inv["client_name"], _st("bn", fontName=FB, fontSize=11.5, leading=15))]
    if inv.get("client_contact"):
        bill.append(P(inv["client_contact"], S_SMALL))
    if inv["status"] == "paid":
        due = [P("AMOUNT DUE", S_LABEL), Spacer(1, 2), P(money(0, cur), _st("a", fontName=FB, fontSize=20, leading=24, alignment=2)),
               Paragraph('<font color="#15803D"><b>Paid in full</b></font>', _st("pf", fontSize=9, alignment=2))]
    elif inv["status"] == "void":
        due = [P("AMOUNT DUE", S_LABEL), Spacer(1, 2), P("-", _st("a", fontName=FB, fontSize=20, leading=24, alignment=2)),
               Paragraph('<font color="#6B7280"><b>Invoice voided</b></font>', _st("pf", fontSize=9, alignment=2))]
    else:
        due = [P("AMOUNT DUE", S_LABEL), Spacer(1, 2),
               P(money(inv["balance"], cur), _st("a", fontName=FB, fontSize=20, leading=24, alignment=2)),
               P(f"Due by {_date(inv['due_date'])}", _st("db", fontSize=9, alignment=2, textColor=MUTED))]
    due_t = _table([[due]], [200], [("BACKGROUND", (0, 0), (-1, -1), PANEL), ("LEFTPADDING", (0, 0), (-1, -1), 14),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 14), ("TOPPADDING", (0, 0), (-1, -1), 10),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10), ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT)])
    return _table([[bill, due_t]], [CW - 200, 200], NOPAD + [("VALIGN", (0, 0), (-1, -1), "TOP")])


def _items_table(inv):
    cur = inv["currency"]
    rows = [[P("#", S_TH), P("DESCRIPTION", S_TH), P("QTY", S_TH_R), P("RATE", S_TH_R), P("AMOUNT", S_TH_R)]]
    for n, it in enumerate(inv["items"], 1):
        desc = [P(it["description"], S_ITEM)]
        meta = " · ".join(str(x) for x in (
            it.get("product_type"),
            f"Plan: {it['plan']}" if it.get("plan") else "",
            f"Node: {it['node']}" if it.get("node") else "",
            f"Server ID: {it['server_id']}" if it.get("server_id") else "",
        ) if x)
        if meta:
            desc.append(P(meta, _st("im", fontSize=8.5, leading=11.5, textColor=ACCENT)))
        desc += [P(s, S_SPEC, bulletText="•") for s in (it.get("details") or "").splitlines() if s.strip()]
        rows.append([P(n, S_SMALL), desc, P(f"{it['quantity']:g}", S_R), P(money(it["unit_price"], cur), S_R),
                     P(money(it["line_total"], cur), S_RB)])
    cmds = [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
            ("TOPPADDING", (0, 1), (-1, -1), 9), ("BOTTOMPADDING", (0, 1), (-1, -1), 9),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 1), (-1, -1), 0.6, RULE), ("LINEBELOW", (0, -1), (-1, -1), 1, HEAD_BG)]
    return _table(rows, [28, 247, 50, 94, 96], cmds, repeatRows=1)


def _totals_and_words(inv):
    cur = inv["currency"]
    rows = [("Subtotal", money(inv["subtotal"], cur))]
    rows.append(("Discount", ("-" if inv["discount"] else "") + money(inv["discount"], cur)))
    if inv["tax_percent"]:
        rows.append((f"Tax ({inv['tax_percent']:g}%)", money(inv["tax"], cur)))
    rows.append(("Total", money(inv["total"], cur)))
    if inv["paid"]:
        rows.append(("Amount Paid", "-" + money(inv["paid"], cur)))
    rows.append(("Balance Due", money(inv["balance"], cur)))
    n = len(rows)
    data = []
    for i, (a, b) in enumerate(rows):
        strong = a in ("Total", "Balance Due")
        data.append([P(a, _st("tl", fontName=FB if strong else F)), P(b, _st("tv", fontName=FB if strong else F, alignment=2))])
    cmds = [("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, n - 1), (-1, n - 1), PANEL), ("LINEABOVE", (0, n - 1), (-1, n - 1), 1, HEAD_BG),
            ("TOPPADDING", (0, n - 1), (-1, n - 1), 8), ("BOTTOMPADDING", (0, n - 1), (-1, n - 1), 8)]
    totals = _table(data, [140, 110], cmds, hAlign="RIGHT")
    words = [P("AMOUNT IN WORDS", S_LABEL), Spacer(1, 3), P(amount_in_words(inv["total"], cur), _st("w", fontName=FB, fontSize=9.5, leading=13))]
    return _table([[words, totals]], [CW - 250, 250], NOPAD + [("VALIGN", (0, 0), (-1, -1), "TOP"),
                                                                  ("RIGHTPADDING", (0, 0), (0, 0), 24)])


def _payments_table(inv):
    cur = inv["currency"]
    rows = [[P("DATE", S_TH), P("METHOD", S_TH), P("NOTE", S_TH), P("AMOUNT", S_TH_R)]]
    for p in inv["payments"]:
        rows.append([P(_date(p["paid_at"])), P(p["method"] or "-"), P(p["note"] or "-"), P(money(p["amount"], cur), S_RB)])
    cmds = [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 1), (-1, -1), 0.6, RULE)]
    return _table(rows, [95, 105, CW - 95 - 105 - 100, 100], cmds, repeatRows=1)


def _info_columns(inv):
    left, right = [], []
    if config.PAYMENT_INSTRUCTIONS and inv["balance"] > 0 and not inv["voided"]:
        left += [P("PAYMENT INFORMATION", S_LABEL), Spacer(1, 3), P(config.PAYMENT_INSTRUCTIONS, S_SMALL), Spacer(1, 10)]
    if inv["notes"]:
        left += [P("NOTES", S_LABEL), Spacer(1, 3), P(inv["notes"], S_SMALL)]
    terms = []
    if config.REFUND_POLICY:
        terms.append("Refund policy: " + config.REFUND_POLICY)
    terms += [t.strip() for t in config.TERMS.split("|") if t.strip()]
    if terms:
        right += [P("TERMS & CONDITIONS", S_LABEL), Spacer(1, 3)]
        right += [P(f"{i}. {t}", S_SMALL) for i, t in enumerate(terms, 1)]
    return left, right


def _signature_block():
    parts = []
    if os.path.exists(config.SIGNATURE_PATH):
        try:
            parts.append(Image(config.SIGNATURE_PATH, width=110, height=36, hAlign="RIGHT", kind="proportional"))
        except Exception:
            parts.append(Spacer(1, 36))
    else:
        parts.append(Spacer(1, 36))
    parts += [HRFlowable(width=170, thickness=0.8, color=INK, hAlign="RIGHT", spaceBefore=2, spaceAfter=3),
              P("Authorized Signatory", _st("sg", fontName=FB, fontSize=9, alignment=2)),
              P(f"For {config.BUSINESS_NAME}", _st("sg2", fontSize=8.5, alignment=2, textColor=MUTED))]
    return parts


def _build(story, first_cb, later_cb, first_top, title):
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=M, rightMargin=M, topMargin=0, bottomMargin=0,
                          title=title, author=config.BUSINESS_NAME)
    pad = dict(leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="first", frames=[Frame(M, BOTTOM, CW, first_top - BOTTOM, id="f1", **pad)], onPage=first_cb),
        PageTemplate(id="later", frames=[Frame(M, BOTTOM, CW, H - 82 - BOTTOM, id="f2", **pad)], onPage=later_cb),
    ])
    doc.build([NextPageTemplate("later")] + story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


NOTE_INVOICE = "This is a computer-generated invoice and does not require a physical signature."


def invoice_pdf(inv: dict) -> bytes:
    def first(c, d):
        _watermark(c, inv["status"])
        _footer(c, NOTE_INVOICE)

    def later(c, d):
        _watermark(c, inv["status"])
        _compact_header(c, f"INVOICE #{inv['number']}")
        _footer(c, NOTE_INVOICE)

    header = _table([[_company_block(inv), _meta_block(inv)]], [CW - 235, 235],
                    NOPAD + [("VALIGN", (0, 0), (-1, -1), "TOP")])
    story = [header, Spacer(1, 12), HRFlowable(width="100%", thickness=1.6, color=ACCENT, spaceAfter=14),
             _bill_and_due(inv), Spacer(1, 18), _items_table(inv), Spacer(1, 12), KeepTogether([_totals_and_words(inv)])]
    if inv["payments"]:
        story += [Spacer(1, 18), KeepTogether([P("PAYMENTS RECEIVED", S_LABEL), Spacer(1, 4), _payments_table(inv)])]
    left, right = _info_columns(inv)
    if left or right:
        story += [Spacer(1, 20), _table([[left or "", right or ""]], [CW / 2 - 12, CW / 2 + 12],
                                       NOPAD + [("VALIGN", (0, 0), (-1, -1), "TOP"), ("RIGHTPADDING", (0, 0), (0, 0), 24)])]
    story += [Spacer(1, 18), KeepTogether(_signature_block())]
    if inv["voided"]:
        story += [Spacer(1, 10), Paragraph(
            f'<font color="#B91C1C"><b>THIS INVOICE HAS BEEN VOIDED.</b></font> {escape(inv["void_reason"] or "")}', S_BODY)]
    return _build(story, first, later, H - 40, f"Invoice {inv['number']}")


def pnl_pdf(report: dict) -> bytes:
    cur = config.CURRENCY
    note = "Generated automatically from recorded transactions. Voided entries are excluded."

    def page(c, d):
        _compact_header(c, "PROFIT & LOSS")
        _footer(c, note)

    def section(title, rows, total_label, total):
        data = [[P(title.upper(), S_TH), P("AMOUNT", S_TH_R)]]
        data += [[P(n), P(money(a, cur), S_R)] for n, a in rows] or [[P("No transactions", S_SMALL), ""]]
        data.append([P(total_label, _st("tl", fontName=FB)), P(money(total, cur), S_RB)])
        last = len(data) - 1
        return _table(data, [CW - 150, 150], [("BACKGROUND", (0, 0), (-1, 0), HEAD_BG), ("BACKGROUND", (0, last), (-1, last), PANEL),
                                              ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                                              ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                              ("LINEBELOW", (0, 1), (-1, -2), 0.6, RULE), ("LINEABOVE", (0, last), (-1, last), 1, HEAD_BG)],
                      repeatRows=1)

    net = report["net"]
    good = net >= 0
    net_t = _table([[P("NET PROFIT" if good else "NET LOSS", _st("n1", fontName=FB, fontSize=12, textColor=colors.white)),
                     P(money(net, cur), _st("n2", fontName=FB, fontSize=12, textColor=colors.white, alignment=2))]],
                   [CW - 150, 150], [("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#15803D" if good else "#B91C1C")),
                                     ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10)])
    # Revenue lines: recorded income categories *and* the Commission / General
    # revenue statements, so money recorded with /revenue shows up in the report.
    revenue_rows = [(name, amount) for name, amount in (report.get("income") or [])]
    revenue_rows += [(f"{name} (revenue statement)", amount)
                     for name, amount in (report.get("statement_rows") or []) if amount]
    # Investment recorded through /revenue gets its own section and its own total, so
    # capital is visible on the statement without ever being counted as revenue.
    investment = report.get("investment") or 0
    capital_rows = [(n, a) for n, a in (report.get("revenue_rows") or [])
                    if n == "Investment" and a] or [("Investment contributions", investment)]
    story = [Paragraph("Profit and Loss Statement", _st("t", fontName=FB, fontSize=20, leading=25, textColor=INK)),
             P(f"Period: {report['month']}   |   Currency: {cur}", S_SMALL), Spacer(1, 14),
             section("Revenue", revenue_rows, "Total revenue", report["revenue"]), Spacer(1, 12),
             section("Investment (tracked separately)", capital_rows, "Total investment", investment),
             Spacer(1, 12),
             section("Expenses", report["expenses"], "Total expenses", report["expense_total"]),
             Spacer(1, 12), net_t, Spacer(1, 12),
             section("Summary", [("Total revenue", report["revenue"]),
                                 ("Investment received (capital)", investment),
                                 ("Total expenses", report["expense_total"])],
                     "Total received (revenue + investment)", report["revenue"] + investment)]
    if report["revenue"]:
        story += [Spacer(1, 6), P(f"Net margin: {net / report['revenue'] * 100:.1f}%", S_SMALL)]
    story += [Spacer(1, 4), P("Investment contributions are capital: they are counted above and in "
                              f"Total received, but never in revenue or net profit.", S_SMALL)]
    return _build(story, page, page, H - 82, f"Profit and Loss {report['month']}")
