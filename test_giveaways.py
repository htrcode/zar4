"""Tests for the giveaway storage, drawing rules and card."""
import json
import os
import shutil
import tempfile
import unittest

from cogs.giveaways import _pick, giveaway_card
from desk.database import Database, giveaway_number


def _card_text(view) -> str:
    return "\n".join(c["content"] for c in view.to_components()[0]["components"] if c["type"] == 10)


def _giveaway(**overrides) -> dict:
    base = {"id": 7, "prize": "1 GB VPS", "winners": 2, "host_id": 5, "required_role_id": None,
            "ends_at": "2026-10-27T12:00:00+00:00", "status": "open", "message_id": 99,
            "channel_id": 42, "ended_at": None}
    base.update(overrides)
    return base


class GiveawayDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-giveaway-")
        self.db = Database(os.path.join(self.dir, "giveaway.db"))
        await self.db.connect()
        self.gid = await self.db.create_giveaway(
            1, guild_id=10, channel_id=42, prize="1 GB VPS", winners=2,
            required_role_id=None, ends_at="2026-10-27T12:00:00+00:00",
        )

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_single_entry_per_member(self):
        self.assertTrue(await self.db.enter_giveaway(self.gid, 100))
        self.assertFalse(await self.db.enter_giveaway(self.gid, 100))   # duplicate blocked
        self.assertTrue(await self.db.enter_giveaway(self.gid, 200))
        self.assertEqual(await self.db.giveaway_entry_count(self.gid), 2)
        self.assertEqual(sorted(await self.db.giveaway_entries(self.gid)), [100, 200])

    async def test_finish_happens_once_and_records_winners(self):
        for user_id in (100, 200, 300):
            await self.db.enter_giveaway(self.gid, user_id)
        self.assertTrue(await self.db.finish_giveaway(1, self.gid, [100, 200]))
        self.assertFalse(await self.db.finish_giveaway(1, self.gid, [300]))  # no double draw
        giveaway = await self.db.get_giveaway(self.gid)
        self.assertEqual(giveaway["status"], "ended")
        self.assertIsNotNone(giveaway["ended_at"])
        self.assertEqual([w["user_id"] for w in await self.db.giveaway_winners(self.gid)], [100, 200])
        self.assertEqual(await self.db.open_giveaways(), [])

    async def test_reroll_adds_a_winner_without_dropping_the_first(self):
        await self.db.enter_giveaway(self.gid, 100)
        await self.db.finish_giveaway(1, self.gid, [100])
        await self.db.add_giveaway_winner(1, self.gid, 200)
        self.assertEqual([w["user_id"] for w in await self.db.giveaway_winners(self.gid)], [100, 200])

    async def test_message_id_is_stored(self):
        await self.db.set_giveaway_message(self.gid, 1234)
        self.assertEqual((await self.db.get_giveaway(self.gid))["message_id"], 1234)
        self.assertEqual(giveaway_number(self.gid), "GA-0001")


class GiveawayDrawingTests(unittest.TestCase):
    def test_pick_respects_count_and_never_repeats(self):
        picks = _pick(list(range(10)), 3)
        self.assertEqual(len(picks), 3)
        self.assertEqual(len(set(picks)), 3)

    def test_pick_excludes_previous_winners(self):
        self.assertEqual(_pick([1], 1, exclude=[1]), [])
        self.assertEqual(_pick([1, 2], 5, exclude=[1]), [2])

    def test_pick_without_entries(self):
        self.assertEqual(_pick([], 2), [])


class GiveawayCardTests(unittest.TestCase):
    def test_open_card_shows_timer_entries_and_button(self):
        view = giveaway_card(_giveaway(required_role_id=9), 14)
        text = _card_text(view)
        self.assertIn("🎉 Giveaway · 1 GB VPS", text)
        self.assertIn("**Entries:** 14", text)
        self.assertIn("**Winner", text)
        self.assertIn("<@&9>", text)
        self.assertIn("<t:", text)          # Discord countdown timestamp
        self.assertIn("giveaway:enter:7", json.dumps(view.to_components()))

    def test_ended_card_lists_winners_and_has_no_button(self):
        view = giveaway_card(_giveaway(status="ended", ended_at="2026-10-27T12:05:00+00:00"), 3, [11, 12])
        text = _card_text(view)
        self.assertIn("**Won By:** <@11>, <@12>", text)
        self.assertIn("GA-0007 · ended", text)
        self.assertNotIn("giveaway:enter", json.dumps(view.to_components()))

    def test_ended_card_without_entries(self):
        view = giveaway_card(_giveaway(status="ended"), 0, [])
        self.assertIn("No valid entries", _card_text(view))


if __name__ == "__main__":
    unittest.main()
