"""Tests for the `/embed` announcement card."""
import types
import unittest

import ui
from cogs.embed import EmbedModal, announcement_card


def _card_text(view) -> str:
    return "\n".join(c["content"] for c in view.to_components()[0]["components"] if c["type"] == 10)


class EmbedCardTests(unittest.TestCase):
    def test_card_contains_title_body_and_footer(self):
        text = _card_text(announcement_card("Server maintenance", "**Restart** at 10 PM IST.", "— Staff"))
        self.assertIn("## Server maintenance", text)
        self.assertIn("**Restart** at 10 PM IST.", text)
        self.assertIn("-# — Staff", text)

    def test_footer_is_optional(self):
        text = _card_text(announcement_card("News", "Body only"))
        self.assertIn("## News", text)
        self.assertNotIn("-#", text)

    def test_form_prefills_title_and_footer(self):
        channel = types.SimpleNamespace(name="announcements")
        modal = EmbedModal(channel, title="Launch", footer="Zarbunex PVT LTD")
        self.assertEqual(modal.heading.default, "Launch")
        self.assertEqual(modal.footer_text.default, "Zarbunex PVT LTD")
        self.assertEqual(modal.title, "Post to #announcements")
        self.assertTrue(modal.message_body.required)


if __name__ == "__main__":
    unittest.main()
