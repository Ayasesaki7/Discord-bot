import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from chat.agent.discord_tools import DiscordToolError, DiscordToolHost
from tests import test_discord_tools as fixtures


class MemberDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def make_host(self):
        host = fixtures.DiscordToolHostTests().make_host(owner=True)
        host.message.guild.chunked = False
        host.message.guild.members = []
        host._fetch_live_members = AsyncMock(return_value=[])
        return host

    def member(self, **kwargs):
        fields = dict(id=111111111111111111, name="example_user", display_name="示例成员", global_name="示例成员",
                      bot=False, joined_at=None, roles=[], display_avatar=fixtures.FakeAsset(), guild_avatar=None,
                      add_roles=AsyncMock())
        fields.update(kwargs)
        return SimpleNamespace(**fields)

    async def test_direct_member_name_uses_live_lookup_on_first_call(self):
        host = self.make_host()
        host._fetch_live_members.return_value = [self.member()]
        result = await host.execute("member", {"user_ref": "示例成员"})
        self.assertEqual(json.loads(result["content"])["id"], "111111111111111111")
        host._fetch_live_members.assert_awaited_once_with("示例成员")

    async def test_discovery_and_returned_ref_do_not_depend_on_gateway_cache(self):
        host = self.make_host()
        member = self.member()
        host._fetch_live_members.return_value = [member]
        result = await host.execute("members", {"query": "ci0721"})
        ref = json.loads(result["content"])[0]["userRef"]
        self.assertIsNone(host.message.guild.get_member(member.id))
        resolved = await host.execute("member", {"user_ref": ref})
        self.assertEqual(json.loads(resolved["content"])["id"], str(member.id))
        host._fetch_live_members.assert_awaited_once()

    async def test_private_channel_does_not_restrict_guild_member_lookup(self):
        host = self.make_host()
        member = self.member()
        host.message.channel.permissions_for = lambda _member: discord.Permissions.none()
        host._fetch_live_members.return_value = [member]
        result = await host.execute("member", {"user_ref": member.name})
        self.assertEqual(json.loads(result["content"])["name"], member.name)

    async def test_live_member_can_be_managed_without_an_extra_lookup(self):
        host = self.make_host()
        member = self.member()
        role = SimpleNamespace(id=501, name="role")
        host._fetch_live_members.return_value = [member]
        host.message.guild.roles = [role]
        host.message.guild.get_role = lambda value: role if value == role.id else None
        host._require_editable_role = lambda _role: None
        await host.execute("add_role", {"user_ref": member.name, "role_ref": role.name})
        member.add_roles.assert_awaited_once()
        host._fetch_live_members.assert_awaited_once()

    async def test_username_global_name_and_nickname_are_exactly_resolved(self):
        member = self.member(display_name="私密昵称")
        for name in (member.name, member.global_name, member.display_name):
            with self.subTest(name=name):
                host = self.make_host()
                host._fetch_live_members.return_value = [member]
                result = await host.execute("member", {"user_ref": name})
                self.assertEqual(json.loads(result["content"])["id"], str(member.id))

    async def test_parallel_and_repeated_names_use_one_request_per_turn(self):
        host = self.make_host()
        host._fetch_live_members.return_value = [self.member()]
        await asyncio.gather(
            host.execute("members", {"query": "示例成员"}),
            host.execute("member", {"user_ref": "示例成员"}),
            host.execute("members", {"query": "@示例成员"}),
        )
        host._fetch_live_members.assert_awaited_once_with("示例成员")

    async def test_empty_query_marks_partial_cache_instead_of_claiming_empty_guild(self):
        host = self.make_host()
        result = await host.execute("members", {})
        self.assertTrue(result["truncated"])
        self.assertIn("not the full guild member list", result["summary"])
        host._fetch_live_members.assert_not_awaited()

    async def test_zero_live_results_explain_prefix_matching(self):
        host = self.make_host()
        result = await host.execute("members", {"query": "夏夏"})
        self.assertEqual(json.loads(result["content"]), [])
        self.assertIn("zero results do not prove", result["summary"])
        self.assertIn("REST", result["summary"])

    async def test_lookup_failure_is_not_an_empty_success_or_retried(self):
        host = self.make_host()
        host._fetch_live_members.side_effect = asyncio.TimeoutError()
        for _ in range(2):
            with self.assertRaisesRegex(DiscordToolError, "不能据此判断成员不存在"):
                await host.execute("members", {"query": "示例成员"})
        host._fetch_live_members.assert_awaited_once()

    async def test_duplicate_live_display_names_remain_ambiguous(self):
        host = self.make_host()
        host._fetch_live_members.return_value = [self.member(), self.member(id=111, name="other")]
        with self.assertRaisesRegex(DiscordToolError, "multiple"):
            await host.execute("member", {"user_ref": "示例成员"})

    async def test_live_prefix_candidate_does_not_become_a_mutation_target(self):
        host = self.make_host()
        host._fetch_live_members.return_value = [self.member()]
        with self.assertRaisesRegex(DiscordToolError, "exactly match"):
            await host.execute("member", {"user_ref": "ci0721"})

    async def test_api_cap_cannot_prove_uniqueness(self):
        host = self.make_host()
        host._fetch_live_members.return_value = [self.member()] + [self.member(id=1000 + n, name=f"other{n}", display_name=f"other{n}", global_name=None) for n in range(99)]
        with self.assertRaisesRegex(DiscordToolError, "reached its limit"):
            await host.execute("member", {"user_ref": "示例成员"})

    async def test_fresh_response_overrides_stale_cached_names(self):
        host = self.make_host()
        host.message.guild.members = [self.member(id=777)]
        host._fetch_live_members.return_value = [self.member()]
        result = await host.execute("member", {"user_ref": "示例成员"})
        self.assertEqual(json.loads(result["content"])["id"], "111111111111111111")

    async def test_complete_cache_supports_global_display_name_search(self):
        host = self.make_host()
        host.message.guild.chunked = True
        host.message.guild.members = [self.member(display_name="nick")]
        result = await host.execute("members", {"query": "示例成员"})
        self.assertEqual(len(json.loads(result["content"])), 1)
        host._fetch_live_members.assert_not_awaited()

    async def test_mention_query_uses_exact_member_id(self):
        host = self.make_host()
        host.message.guild.fetch_member = AsyncMock(return_value=self.member())
        result = await host.execute("members", {"query": "<@111111111111111111>"})
        self.assertEqual(len(json.loads(result["content"])), 1)
        host.message.guild.fetch_member.assert_awaited_once_with(111111111111111111)
        host._fetch_live_members.assert_not_awaited()

    async def test_real_sdk_rest_route_and_member_payload_without_privileged_intents(self):
        bot = discord.Client(intents=discord.Intents.none())
        try:
            guild = discord.Guild(state=bot._connection, data={
                "id": "444444444444444444", "name": "guild", "owner_id": "10", "member_count": 123,
                "roles": [{"id": "444444444444444444", "name": "@everyone", "permissions": "0", "position": 0, "color": 0}],
            })
            bot.http.request = AsyncMock(return_value=[{
                "user": {"id": "111111111111111111", "username": "example_user", "global_name": "示例成员", "discriminator": "0", "avatar": None},
                "roles": [], "flags": 0, "joined_at": "2026-09-01T00:00:00+00:00", "deaf": False, "mute": False,
            }])
            message = SimpleNamespace(guild=guild, author=SimpleNamespace(id=10), channel=SimpleNamespace(id=200))
            host = DiscordToolHost(bot=bot, message=message, owner_user_id=10)
            result = await host.execute("member", {"user_ref": "示例成员"})
            payload = json.loads(result["content"])
            self.assertEqual(payload["id"], "111111111111111111")
            self.assertEqual(payload["roles"], [])
            self.assertFalse(bot.intents.members)
            self.assertIsNone(guild.get_member(111111111111111111))
            route = bot.http.request.await_args.args[0]
            self.assertEqual(route.method, "GET")
            self.assertEqual(route.url, "https://discord.com/api/v10/guilds/444444444444444444/members/search")
            self.assertEqual(bot.http.request.await_args.kwargs["params"], {"query": "示例成员", "limit": 100})
        finally:
            await bot.close()
