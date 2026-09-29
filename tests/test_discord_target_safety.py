from __future__ import annotations

import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from chat.agent.discord_tools import DiscordToolError
from tests import test_discord_tools as fixtures


class DiscordTargetSafetyTests(unittest.IsolatedAsyncioTestCase):
    def make_host(self, **kwargs):
        return fixtures.DiscordToolHostTests().make_host(owner=True, **kwargs)

    def targets(self, host):
        member = SimpleNamespace(
            id=222222222222222222, name="unique_user", display_name="小明", global_name=None,
            bot=False, joined_at=None, roles=[], display_avatar=fixtures.FakeAsset(), guild_avatar=None,
            add_roles=AsyncMock(), kick=AsyncMock(),
        )
        role = SimpleNamespace(id=333333333333333333, name="测试身份组", position=1, delete=AsyncMock())
        host.message.guild.members = [member]
        host.message.guild.roles = [role]
        host.message.guild.get_member = lambda value: member if value == member.id else None
        host.message.guild.get_role = lambda value: role if value == role.id else None
        host._require_editable_role = lambda _role: None
        return member, role

    async def test_query_refs_round_trip_without_copying_snowflakes(self):
        host = self.make_host()
        member, role = self.targets(host)
        result = await host.execute("members", {"query": "unique"})
        user_ref = json.loads(result["content"])[0]["userRef"]
        role_ref = host._role_payload(role)["roleRef"]
        await host.execute("add_role", {"user_ref": user_ref, "role_ref": role_ref})
        member.add_roles.assert_awaited_once()
        self.assertEqual(member.add_roles.await_args.args[0].id, role.id)

    async def test_channel_ref_resolves_exact_current_guild_channel(self):
        host = self.make_host()
        result = await host.execute("context", {})
        ref = json.loads(result["content"])["channel"]["channelRef"]
        await host.execute("send_message", {"channel_id": ref, "content": "hello"})
        host.message.channel.send.assert_awaited_once()

    async def test_wrong_type_reference_cannot_be_used_as_a_member(self):
        host = self.make_host()
        member, role = self.targets(host)
        with self.assertRaisesRegex(DiscordToolError, "wrong-type"):
            await host.execute("add_role", {"user_ref": host._target_ref("role", role.id), "role_ref": str(role.id)})
        member.add_roles.assert_not_awaited()

    async def test_wrong_type_reference_cannot_set_permission_target(self):
        host = self.make_host()
        member, _ = self.targets(host)
        with self.assertRaisesRegex(DiscordToolError, "wrong-type"):
            await host.execute("set_channel_permissions", {
                "channel_id": "200", "target_type": "role",
                "target_id": host._target_ref("member", member.id), "permission_values": {"view_channel": True},
            })

    async def test_old_turn_and_cross_guild_refs_are_rejected(self):
        first = self.make_host()
        old_ref = first._target_ref("channel", 200)
        for guild_id in (100, 101):
            with self.subTest(guild_id=guild_id):
                host = self.make_host()
                host.message.guild.id = guild_id
                with self.assertRaisesRegex(DiscordToolError, "stale"):
                    await host.execute("send_message", {"channel_id": old_ref, "content": "hello"})
                host.message.channel.send.assert_not_awaited()

    async def test_conflicting_member_identifiers_are_rejected(self):
        host = self.make_host()
        member, role = self.targets(host)
        with self.assertRaisesRegex(DiscordToolError, "different members"):
            await host.execute("add_role", {"user_ref": member.name, "user_id": "999", "role_ref": role.name})
        member.add_roles.assert_not_awaited()

    async def test_conflicting_role_identifiers_are_rejected(self):
        host = self.make_host()
        member, role = self.targets(host)
        with self.assertRaisesRegex(DiscordToolError, "different roles"):
            await host.execute("add_role", {"user_ref": member.name, "role_ref": role.name, "role_id": "999"})
        member.add_roles.assert_not_awaited()

    async def test_unique_partial_member_is_not_an_action_target(self):
        host = self.make_host()
        member, role = self.targets(host)
        with self.assertRaisesRegex(DiscordToolError, "exactly match"):
            await host.execute("add_role", {"user_ref": "unique", "role_ref": role.name})
        member.add_roles.assert_not_awaited()

    async def test_unique_partial_role_is_not_an_action_target(self):
        host = self.make_host()
        member, _ = self.targets(host)
        with self.assertRaisesRegex(DiscordToolError, "exactly match"):
            await host.execute("add_role", {"user_ref": member.name, "role_ref": "测试"})
        member.add_roles.assert_not_awaited()

    async def test_incomplete_member_cache_does_not_prove_name_uniqueness(self):
        host = self.make_host()
        member, role = self.targets(host)
        host.message.guild.chunked = False
        with self.assertRaisesRegex(DiscordToolError, "cache is incomplete"):
            await host.execute("add_role", {"user_ref": member.name, "role_ref": role.name})
        await host.execute("add_role", {"user_ref": f"<@{member.id}>", "role_ref": f"<@&{role.id}>"})
        member.add_roles.assert_awaited_once()

    async def test_duplicate_role_names_require_disambiguation(self):
        host = self.make_host()
        member, role = self.targets(host)
        host.message.guild.roles.append(SimpleNamespace(id=321, name=role.name))
        with self.assertRaisesRegex(DiscordToolError, "multiple"):
            await host.execute("add_role", {"user_ref": member.name, "role_ref": role.name})

    async def test_destructive_name_is_frozen_before_confirmation(self):
        host = self.make_host()
        member, role = self.targets(host)
        replacement = SimpleNamespace(id=321, name="different")
        host.message.guild.roles.append(replacement)

        async def rename_while_waiting(action, args):
            self.assertEqual(args["role_ref"], str(role.id))
            role.name, replacement.name = "renamed", role.name
            return True

        host.confirmation_handler = AsyncMock(side_effect=rename_while_waiting)
        await host.execute("delete_role", {"role_ref": role.name})
        role.delete.assert_awaited_once()

    async def test_invalid_ids_fail_before_confirmation_or_mutation(self):
        for raw in (222222222222222222, float(222222222222222222), True, "+123", "１２３", "1e18", "123_456", "00123", [], {}, "18446744073709551616"):
            with self.subTest(raw=raw):
                host = self.make_host(reaction_confirmed=True)
                with self.assertRaises(DiscordToolError):
                    await host.execute("delete_channel", {"channel_id": raw})
                host.confirmation_handler.assert_not_awaited()

    async def test_unban_does_not_stringify_unsafe_integer_first(self):
        host = self.make_host(reaction_confirmed=True)
        host.message.guild.unban = AsyncMock()
        for args in ({"user_id": 222222222222222222}, {"user_ref": 222222222222222222}):
            with self.subTest(args=args), self.assertRaisesRegex(DiscordToolError, "unsafe JSON integer"):
                await host.execute("unban_member", args)
        host.message.guild.unban.assert_not_awaited()
        host.confirmation_handler.assert_not_awaited()

    async def test_unban_accepts_exact_quoted_id_for_non_member(self):
        host = self.make_host(reaction_confirmed=True)
        host.message.guild.unban = AsyncMock()
        await host.execute("unban_member", {"user_ref": "222222222222222222"})
        self.assertEqual(host.message.guild.unban.await_args.args[0].id, 222222222222222222)

    async def test_cross_guild_argument_is_never_silently_ignored(self):
        host = self.make_host()
        with self.assertRaisesRegex(DiscordToolError, "cross-guild"):
            await host.execute("send_message", {"guild_id": "101", "content": "no"})
        host.message.channel.send.assert_not_awaited()

    async def test_wrong_channel_range_anchor_stops_before_confirmation(self):
        host = self.make_host(reaction_confirmed=True)
        host.message.channel.fetch_message = AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"))
        host.message.channel.purge = AsyncMock()
        with self.assertRaisesRegex(DiscordToolError, "nothing deleted"):
            await host.execute("delete_messages", {"after_message_id": "999"})
        host.message.channel.purge.assert_not_awaited()
        host.confirmation_handler.assert_not_awaited()


class AuditLogAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def make_host(self, *, developer=False, admin=False, view=False, guild_owner=False):
        host = fixtures.DiscordToolHostTests().make_host(owner=developer, administrator=admin, guild_owner=guild_owner)
        # The fixture's developer also owns the guild by default; separate them.
        host.message.guild.owner_id = host.message.author.id if guild_owner else 900
        requester = SimpleNamespace(
            id=host.message.author.id, guild=host.message.guild,
            guild_permissions=discord.Permissions(administrator=admin, view_audit_log=view),
        )
        host.message.guild.fetch_member = AsyncMock(return_value=requester)
        host.message.guild.me.guild_permissions = discord.Permissions(view_audit_log=True)

        async def entries(**kwargs):
            created = discord.utils.utcnow() - timedelta(days=1)
            yield SimpleNamespace(id=discord.utils.time_snowflake(created), action="role_update", user=SimpleNamespace(id=20),
                                  target=SimpleNamespace(id=300), reason="sensitive audit detail",
                                  created_at=created)

        host.message.guild.audit_logs = MagicMock(side_effect=entries)
        return host

    async def test_ordinary_member_denied_before_audit_api(self):
        host = self.make_host()
        with self.assertRaisesRegex(DiscordToolError, "拒绝读取审核日志"):
            await host.execute("audit_log", {})
        host.message.guild.audit_logs.assert_not_called()

    async def test_developer_without_guild_permission_is_also_denied(self):
        host = self.make_host(developer=True)
        with self.assertRaisesRegex(DiscordToolError, "view_audit_log"):
            await host.execute("audit_log", {})
        host.message.guild.audit_logs.assert_not_called()

    async def test_view_audit_log_admin_and_guild_owner_are_allowed(self):
        for permissions in ({"view": True}, {"admin": True}, {"guild_owner": True}):
            with self.subTest(permissions=permissions):
                host = self.make_host(**permissions)
                result = await host.execute("audit_log", {})
                self.assertEqual(json.loads(result["content"])["entries"][0]["targetId"], "300")
                self.assertEqual(host.message.guild.fetch_member.await_count, 2)
                host.message.guild.fetch_member.assert_awaited_with(host.message.author.id)

    async def test_stale_message_admin_does_not_override_live_revocation(self):
        host = self.make_host()
        host.message.author.guild_permissions = discord.Permissions(administrator=True)
        with self.assertRaisesRegex(DiscordToolError, "view_audit_log"):
            await host.execute("audit_log", {})
        host.message.guild.audit_logs.assert_not_called()

    async def test_permission_is_rechecked_on_every_call(self):
        host = self.make_host(view=True)
        await host.execute("audit_log", {})
        host.message.guild.fetch_member.return_value.guild_permissions = discord.Permissions.none()
        with self.assertRaisesRegex(DiscordToolError, "view_audit_log"):
            await host.execute("audit_log", {})
        self.assertEqual(host.message.guild.audit_logs.call_count, 1)

    async def test_other_guild_admin_does_not_grant_access_here(self):
        host = self.make_host(admin=True)
        host.message.guild.fetch_member.return_value.guild = SimpleNamespace(id=999)
        with self.assertRaisesRegex(DiscordToolError, "view_audit_log"):
            await host.execute("audit_log", {})
        host.message.guild.audit_logs.assert_not_called()

    async def test_permission_lookup_failure_fails_closed(self):
        host = self.make_host(admin=True)
        host.message.guild.fetch_member.side_effect = OSError("network unavailable")
        with self.assertRaisesRegex(DiscordToolError, "无法核实"):
            await host.execute("audit_log", {})
        host.message.guild.audit_logs.assert_not_called()

    async def test_bot_permission_is_separate_from_requester_permission(self):
        host = self.make_host(view=True)
        host.message.guild.me.guild_permissions = discord.Permissions.none()
        with self.assertRaisesRegex(DiscordToolError, "BOT 缺少"):
            await host.execute("audit_log", {})
        host.message.guild.audit_logs.assert_not_called()
