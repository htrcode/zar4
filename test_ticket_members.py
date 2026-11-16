"""/ticket add and /ticket remove: add people to a ticket, and keep them through close / reopen / move."""
import os as _os
import tempfile as _tempfile

# Keep the JSON ledger away from the real one (must be set before `config` is imported).
_os.environ.setdefault("INVOICE_BOT_DATA", _os.path.join(_tempfile.mkdtemp(prefix="zarbot-ledger-"), "data.json"))

import shutil  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
import warnings  # noqa: E402
from unittest import mock  # noqa: E402

import discord  # noqa: E402

from desk.database import Database  # noqa: E402
from desk.tickets import Tickets  # noqa: E402
from tests.test_departments import _Category, _Channel, _Departments, _Guild, _Member, _Role  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)


def http_error(cls, status=500):
    return cls(types.SimpleNamespace(status=status, reason="x"), "boom")


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


class Chan(_Channel):
    """A ticket channel that records permission changes and messages."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.perm_calls, self.sent, self.perm_error, self.send_error = [], [], None, None

    async def set_permissions(self, target, *, overwrite=None, reason=None):
        if self.perm_error:
            raise self.perm_error
        self.perm_calls.append((target, overwrite, reason))

    async def send(self, **kw):
        if self.send_error:
            raise self.send_error
        self.sent.append(kw)


class FakeInteraction:
    def __init__(self, channel, user, guild):
        self.channel, self.channel_id, self.user, self.guild = channel, channel.id, user, guild
        self.sent, self._done = [], False
        self.response = types.SimpleNamespace(defer=self._defer, is_done=lambda: self._done, send_message=self._send)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self, **kw):
        self._done = True

    async def _send(self, *a, **kw):
        self._done = True
        self.sent.append(kw["view"])

    @property
    def text(self):
        return "\n".join(text_of(v) for v in self.sent)


class Base(_Departments, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-members-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = Database(_os.path.join(self.dir, "t.db"))
        await self.db.connect()
        self.addAsyncCleanup(self.db.close)
        self.billing_cat, self.support_cat = _Category(111, "Billing"), _Category(222, "Technical Support")
        self.channel = Chan(category=self.billing_cat)
        self.guild = _Guild([self.billing_role, self.support_role, self.manager_role],
                            [self.billing_cat, self.support_cat])
        self.owner, self.helper, self.friend = _Member(7), _Member(21), _Member(22)
        self.staff = _Member(9, administrator=True)
        for member in (self.owner, self.helper, self.friend):
            self.guild.members[member.id] = member
        self.service = Tickets.__new__(Tickets)
        self.service.bot = types.SimpleNamespace(
            db=self.db, get_channel=lambda cid: self.channel if cid == self.channel.id else None,
            fetch_channel=mock.AsyncMock(side_effect=http_error(discord.NotFound, 404)))
        self.service._moving, self.service._moved = set(), {}
        self.service.request_refresh = lambda ticket_id: None
        self.service._log = mock.AsyncMock()
        self.service.save_transcript = mock.AsyncMock(return_value=None)
        self.ticket_id = await self.db.create_ticket(
            1, subject="Need a VPS", kind="support", priority="normal", client_discord_id=7,
            client_name="Bibi", channel_id=self.channel.id)

    async def ticket(self):
        return await self.db.get_ticket(self.ticket_id)

    async def add(self, member=None):
        await self.service.add_member(self.guild, await self.ticket(), member or self.helper, self.staff)

    async def members(self):
        return await self.db.ticket_members(self.ticket_id)

    def overwrite_for(self, overwrites, user_id):
        return next(ow for target, ow in overwrites.items() if getattr(target, "id", None) == user_id
                    and not isinstance(target, _Role))


# ---------------------------------------------------------------------------
class AddMemberTests(Base):
    async def test_the_member_gets_access_is_remembered_and_told(self):
        await self.add()
        target, overwrite, reason = self.channel.perm_calls[0]
        self.assertIs(target, self.helper)
        for allowed in ("view_channel", "send_messages", "attach_files", "read_message_history"):
            self.assertTrue(getattr(overwrite, allowed), allowed)
        self.assertIn("ticket-0001", reason)
        self.assertEqual(await self.members(), [21])
        notice = self.channel.sent[0]
        self.assertIn("<@21>", text_of(notice["view"]))
        self.assertEqual(notice["allowed_mentions"].users, [self.helper])    # they are actually pinged
        self.assertIn("ticket.member_add", [a["action"] for a in await self.db.recent_audit(10)])

    async def test_several_people_can_be_added(self):
        await self.add(self.helper)
        await self.add(self.friend)
        self.assertEqual(sorted(await self.members()), [21, 22])

    async def test_refusals_change_nothing(self):
        bot_user = _Member(30)
        bot_user.bot = True
        cases = [(bot_user, "Bots"), (self.owner, "already the owner")]
        for member, expected in cases:
            with self.assertRaisesRegex(ValueError, expected):
                await self.add(member)
        await self.add()
        with self.assertRaisesRegex(ValueError, "already added"):
            await self.add()
        await self.db.set_ticket_status(1, self.ticket_id, "closed", "done")
        with self.assertRaisesRegex(ValueError, "Reopen it first"):
            await self.add(self.friend)
        self.assertEqual(await self.members(), [21])
        self.assertEqual(len(self.channel.perm_calls), 1)

    async def test_a_deleted_channel_is_reported(self):
        self.service.bot.get_channel = lambda cid: None
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            await self.add()
        self.assertEqual(await self.members(), [])

    async def test_missing_permission_is_explained_and_nothing_is_saved(self):
        self.channel.perm_error = http_error(discord.Forbidden, 403)
        with self.assertRaisesRegex(ValueError, "Manage Roles"):
            await self.add()
        self.channel.perm_error = http_error(discord.HTTPException, 500)
        with self.assertRaisesRegex(ValueError, "Nothing was changed"):
            await self.add()
        self.assertEqual(await self.members(), [])

    async def test_if_saving_fails_the_access_is_taken_back(self):
        with mock.patch.object(self.db, "add_ticket_member", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(ValueError, "nothing was changed"):
                await self.add()
        self.assertEqual([call[1] for call in self.channel.perm_calls], [self.channel.perm_calls[0][1], None])
        self.assertEqual(await self.members(), [])

    async def test_a_failing_notice_does_not_undo_the_add(self):
        self.channel.send_error = http_error(discord.HTTPException, 500)
        await self.add()
        self.assertEqual(await self.members(), [21])


class RemoveMemberTests(Base):
    async def remove(self, member=None):
        await self.service.remove_member(self.guild, await self.ticket(), member or self.helper, self.staff)

    async def test_access_is_taken_back_and_forgotten(self):
        await self.add()
        await self.remove()
        target, overwrite, _ = self.channel.perm_calls[-1]
        self.assertEqual((target, overwrite), (self.helper, None))            # the overwrite is deleted
        self.assertEqual(await self.members(), [])
        self.assertIn("ticket.member_remove", [a["action"] for a in await self.db.recent_audit(10)])
        self.assertIn("Member removed", text_of(self.channel.sent[-1]["view"]))

    async def test_only_people_added_with_the_command_can_be_removed(self):
        with self.assertRaisesRegex(ValueError, "not added with `/ticket add`"):
            await self.remove(self.friend)
        self.assertEqual(self.channel.perm_calls, [])          # staff and the owner are never touched

    async def test_a_refused_removal_keeps_the_record(self):
        await self.add()
        self.channel.perm_error = http_error(discord.Forbidden, 403)
        with self.assertRaisesRegex(ValueError, "Manage Roles"):
            await self.remove()
        self.assertEqual(await self.members(), [21])

    async def test_a_permission_that_is_already_gone_is_fine(self):
        await self.add()
        self.channel.perm_error = http_error(discord.NotFound, 404)
        await self.remove()
        self.assertEqual(await self.members(), [])

    async def test_removing_from_a_closed_ticket_works_quietly(self):
        await self.add()
        await self.db.set_ticket_status(1, self.ticket_id, "closed", "done")
        sent = len(self.channel.sent)
        await self.remove()
        self.assertEqual(await self.members(), [])
        self.assertEqual(len(self.channel.sent), sent)

    async def test_a_deleted_channel_still_lets_the_record_go(self):
        await self.add()
        self.service.bot.get_channel = lambda cid: None
        await self.remove()
        self.assertEqual(await self.members(), [])


# ---------------------------------------------------------------------------
class PersistenceTests(Base):
    """Close, reopen and move rebuild every permission, so added people must be re-applied each time."""

    def test_overwrites_include_added_people_with_the_owners_rights(self):
        opened = Tickets._overwrites(self.guild, 7, department="billing", member_ids=[21, 22])
        for user_id in (21, 22):
            ow = self.overwrite_for(opened, user_id)
            self.assertTrue(ow.view_channel and ow.send_messages and ow.read_message_history)
        closed = Tickets._overwrites(self.guild, 7, closed=True, department="billing", member_ids=[21])
        ow = self.overwrite_for(closed, 21)
        self.assertFalse(ow.view_channel or ow.send_messages)                  # same as the owner of a closed ticket
        self.assertTrue(ow.read_message_history)

    def test_the_owner_listed_as_a_member_is_not_duplicated_and_unknown_users_work(self):
        ow = Tickets._overwrites(self.guild, 7, member_ids=[7, 999999])
        self.assertEqual(sum(1 for t in ow if getattr(t, "id", None) == 7), 1)
        self.assertTrue(self.overwrite_for(ow, 999999).view_channel)           # someone not cached: discord.Object

    def test_without_members_nothing_changes(self):
        self.assertEqual(Tickets._overwrites(self.guild, 7, department="billing").keys(),
                         Tickets._overwrites(self.guild, 7, department="billing", member_ids=()).keys())

    async def test_added_people_keep_access_through_close_reopen_and_move(self):
        await self.add()
        actor = _Member(9, [self.support_role], manage_channels=True)
        await self.service.close_ticket(self.guild, self.ticket_id, self.staff, "done")
        closed = self.overwrite_for(self.channel.edits[-1]["overwrites"], 21)
        self.assertFalse(closed.view_channel)                                  # locked like the owner
        self.assertTrue(closed.read_message_history)
        await self.service.reopen_ticket(self.guild, self.ticket_id, self.staff)
        self.assertTrue(self.overwrite_for(self.channel.edits[-1]["overwrites"], 21).view_channel)   # back
        await self.service.move_ticket(self.guild, self.ticket_id, actor, "support", "Needs tech", "support-0001")
        moved = self.channel.edits[-1]["overwrites"]
        self.assertTrue(self.overwrite_for(moved, 21).view_channel)            # still in after a handover
        self.assertTrue(self.overwrite_for(moved, 21).send_messages)
        self.assertEqual(await self.members(), [21])

    async def test_a_removed_person_is_not_brought_back_by_a_later_rebuild(self):
        await self.add()
        await self.service.remove_member(self.guild, await self.ticket(), self.helper, self.staff)
        await self.service.close_ticket(self.guild, self.ticket_id, self.staff, "done")
        self.assertNotIn(21, [getattr(t, "id", None) for t in self.channel.edits[-1]["overwrites"]])

    async def test_database_membership_rules(self):
        self.assertTrue(await self.db.add_ticket_member(9, self.ticket_id, 21))
        self.assertFalse(await self.db.add_ticket_member(9, self.ticket_id, 21))
        self.assertTrue(await self.db.add_ticket_member(9, self.ticket_id, 22))
        self.assertEqual(sorted(await self.members()), [21, 22])
        self.assertTrue(await self.db.remove_ticket_member(9, self.ticket_id, 21))
        self.assertFalse(await self.db.remove_ticket_member(9, self.ticket_id, 21))
        self.assertEqual(await self.members(), [22])


class CommandTests(Base):
    def interaction(self, user=None):
        return FakeInteraction(self.channel, user or self.staff, self.guild)

    async def test_add_and_remove_from_inside_the_ticket_channel(self):
        done = self.interaction()
        await Tickets.add_cmd.callback(self.service, done, self.helper, None)
        self.assertIn("added to **ticket-0001**", done.text)
        self.assertEqual(await self.members(), [21])
        gone = self.interaction()
        await Tickets.remove_cmd.callback(self.service, gone, self.helper, None)
        self.assertIn("removed from **ticket-0001**", gone.text)
        self.assertEqual(await self.members(), [])

    async def test_a_ticket_number_can_be_given_from_another_channel(self):
        elsewhere = FakeInteraction(Chan(channel_id=777), self.staff, self.guild)
        await Tickets.add_cmd.callback(self.service, elsewhere, self.friend, self.ticket_id)
        self.assertIn("added", elsewhere.text)
        self.assertEqual(await self.members(), [22])

    async def test_errors_are_shown_to_the_staff_member(self):
        for member, ticket, expected in ((self.owner, None, "already the owner"),
                                         (self.helper, 999, "Ticket not found")):
            done = self.interaction()
            await Tickets.add_cmd.callback(self.service, done, member, ticket)
            self.assertIn("❌", done.text)
            self.assertIn(expected, done.text)
        nobody = self.interaction()
        await Tickets.remove_cmd.callback(self.service, nobody, self.helper, None)
        self.assertIn("not added with", nobody.text)

    async def test_staff_of_another_department_cannot_add_anyone(self):
        outsider = _Member(5, [_Role(404)])
        done = self.interaction(outsider)
        await Tickets.add_cmd.callback(self.service, done, self.helper, None)
        self.assertIn("another department", done.text)
        self.assertEqual(await self.members(), [])
        self.assertEqual(self.channel.perm_calls, [])

    def test_the_commands_exist_with_the_expected_options(self):
        add, remove = Tickets.add_cmd, Tickets.remove_cmd
        self.assertEqual((add.name, remove.name), ("add", "remove"))
        for command in (add, remove):
            self.assertEqual([(p.name, p.required) for p in command.parameters],
                             [("member", True), ("ticket", False)])


if __name__ == "__main__":
    unittest.main()
