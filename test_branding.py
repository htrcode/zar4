"""Per-company invoice logo and signature (/branding)."""
import os as _os
import tempfile as _tempfile

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_os.environ.setdefault("INVOICE_BOT_DATA", _os.path.join(_tempfile.mkdtemp(prefix="zarbot-ledger-"), "data.json"))

import io
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import desk.utils as utils
from cogs.branding import Branding
from desk import pdf_gen
from desk.database import Database
from desk.utils import branding_file, clear_branding, parse_items, save_branding


def png(color, size=(300, 120), mode="RGBA"):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def logo_pixel(company):
    data = pdf_gen._logo(company)[2]
    return Image.open(io.BytesIO(data)).convert("RGBA").getpixel((5, 5))


def text_of(view):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == 10:
                out.append(node["content"])
            for child in node.get("components", []) or []:
                walk(child)
    for top in view.to_components():
        walk(top)
    return "\n".join(out)


class FakeAttachment:
    def __init__(self, data, size=None):
        self.data, self.size = data, size if size is not None else len(data)

    async def read(self):
        return self.data


class FakeInteraction:
    def __init__(self):
        self.sent, self.files, self._done = [], [], False
        self.user = types.SimpleNamespace(id=1)
        self.response = types.SimpleNamespace(defer=self._defer, is_done=lambda: self._done,
                                              send_message=self._send)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self, **kw):
        self._done = True

    async def _send(self, *a, **kw):
        self._done = True
        self.sent.append(kw["view"])
        if kw.get("file"):
            self.files.append(kw["file"])

    @property
    def text(self):
        return "\n".join(text_of(v) for v in self.sent)


def choice(value):
    return types.SimpleNamespace(value=value, name=value)


class BrandingBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-brand-")
        patcher = mock.patch.object(utils, "BRANDING_DIR", Path(self.dir) / "branding")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class StorageTests(BrandingBase):
    async def test_upload_is_stored_per_company_and_falls_back_to_the_default(self):
        self.assertIsNone(branding_file("zarbunex", "logo"))
        default = logo_pixel("zarbunex")
        save_branding("zarbunex", "logo", png((255, 0, 0, 255)))
        self.assertEqual(logo_pixel("zarbunex"), (255, 0, 0, 255))
        self.assertEqual(logo_pixel("hectorhost"), default)           # other companies keep the default
        self.assertEqual(logo_pixel(None), default)                   # invoices with no company too
        self.assertEqual(logo_pixel("ZarBunex"), (255, 0, 0, 255))    # key or name both resolve

    async def test_replacing_an_upload_is_picked_up_without_a_restart(self):
        save_branding("hectorhost", "logo", png((0, 255, 0, 255)))
        self.assertEqual(logo_pixel("hectorhost"), (0, 255, 0, 255))
        os.utime(branding_file("hectorhost", "logo"), ns=(1, 1))      # force a different timestamp
        save_branding("hectorhost", "logo", png((0, 0, 255, 255)))
        self.assertEqual(logo_pixel("hectorhost"), (0, 0, 255, 255))

    async def test_signature_is_used_in_the_invoice_footer(self):
        path = save_branding("matrixmc", "signature", png((10, 10, 10, 255), (200, 80)))
        block = pdf_gen._signature_block("matrixmc")
        self.assertEqual(block[0].filename, str(path))
        other = pdf_gen._signature_block("zarbunex")
        self.assertNotEqual(getattr(other[0], "filename", None), str(path))

    async def test_reset_returns_to_the_default(self):
        save_branding("zarbunex", "logo", png((255, 0, 0, 255)))
        save_branding("zarbunex", "signature", png((0, 0, 0, 255)))
        self.assertTrue(clear_branding("zarbunex", "logo"))
        self.assertFalse(clear_branding("zarbunex", "logo"))
        self.assertIsNone(branding_file("zarbunex", "logo"))
        self.assertIsNotNone(branding_file("zarbunex", "signature"))  # the other image is untouched

    async def test_bad_input_is_refused_and_nothing_is_written(self):
        for data in (b"not an image", b"", b"<svg></svg>"):
            with self.assertRaises(ValueError):
                save_branding("zarbunex", "logo", data)
        with self.assertRaises(ValueError):
            save_branding("nope", "logo", png((1, 2, 3, 255)))
        with self.assertRaises(ValueError):
            save_branding("zarbunex", "stamp", png((1, 2, 3, 255)))
        self.assertFalse((Path(self.dir) / "branding").exists())

    async def test_big_images_are_scaled_down_and_jpegs_are_accepted(self):
        buf = io.BytesIO()
        Image.new("RGB", (3000, 2000), (9, 9, 9)).save(buf, "JPEG")
        path = save_branding("zarbunex", "logo", buf.getvalue())
        self.assertLessEqual(max(Image.open(path).size), 1000)
        self.assertEqual(Image.open(path).format, "PNG")


class InvoicePdfTests(BrandingBase):
    async def test_invoice_pdf_builds_with_company_images_and_without(self):
        db = Database(os.path.join(self.dir, "t.db"))
        await db.connect()
        self.addAsyncCleanup(db.close)
        ids = []
        for company in ("zarbunex", "hectorhost"):
            ids.append(await db.create_invoice(
                1, client_discord_id=2, client_name="Cat", items=parse_items("VPS | 1 | 500 | type=VPS"),
                due_days=7, tax_percent=0, discount_cents=0, category="Hosting Revenue", notes="",
                ticket_channel_id=None, company_id=company))
        save_branding("zarbunex", "logo", png((255, 0, 0, 255)))
        save_branding("zarbunex", "signature", png((0, 0, 0, 255), (200, 80)))
        for iid in ids:
            data = pdf_gen.invoice_pdf(await db.get_invoice(iid))
            self.assertTrue(data.startswith(b"%PDF"))
        report_pdf = pdf_gen.pnl_pdf({"month": "2026-11", "company": "zarbunex", "net": 0, "revenue": 0,
                                      "income": [], "expenses": [], "expense_total": 0, "investment": 0,
                                      "statement_rows": [], "revenue_rows": []})
        self.assertTrue(report_pdf.startswith(b"%PDF"))


class CommandTests(BrandingBase):
    def setUp(self):
        self.cog = Branding(types.SimpleNamespace())

    async def run_set(self, data, company="zarbunex", kind="logo", size=None):
        interaction = FakeInteraction()
        await Branding.set_image.callback(self.cog, interaction, choice(company), choice(kind),
                                          FakeAttachment(data, size))
        return interaction

    async def test_set_saves_confirms_and_shows_the_image(self):
        done = await self.run_set(png((255, 0, 0, 255)))
        self.assertIn("Logo updated", done.text)
        self.assertEqual(len(done.files), 1)
        self.assertEqual(logo_pixel("zarbunex"), (255, 0, 0, 255))
        sig = await self.run_set(png((0, 0, 0, 255)), kind="signature")
        self.assertIn("Signature updated", sig.text)
        self.assertIsNotNone(branding_file("zarbunex", "signature"))

    async def test_set_rejects_non_images_and_oversized_files(self):
        self.assertIn("could not read", (await self.run_set(b"hello")).text)
        self.assertIn("too big", (await self.run_set(png((1, 1, 1, 255)), size=6 * 1024 * 1024)).text)
        self.assertIsNone(branding_file("zarbunex", "logo"))

    async def test_reset_and_show(self):
        await self.run_set(png((255, 0, 0, 255)))
        shown = FakeInteraction()
        await Branding.show_images.callback(self.cog, shown)
        self.assertIn("logo custom", shown.text)
        for expected in ("✅ Logo reset", "Nothing to reset"):
            done = FakeInteraction()
            await Branding.reset_image.callback(self.cog, done, choice("zarbunex"), choice("logo"))
            self.assertIn(expected, done.text)

    async def test_staff_only(self):
        interaction = FakeInteraction()
        self.assertFalse(await self.cog.interaction_check(interaction))
        self.assertIn("staff", interaction.text)


if __name__ == "__main__":
    unittest.main()
