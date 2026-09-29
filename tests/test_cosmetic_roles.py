import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from chat.agent.cosmetic_roles import CosmeticRoleHost, CosmeticRoleStore, cosmetic_enabled
from chat.agent.discord_tools import DiscordToolHost, DiscordToolError


class Role:
    def __init__(self, guild, role_id, name, position, permissions=0):
        self.guild, self.id, self.name, self.position = guild, role_id, name, position
        self.permissions = discord.Permissions(permissions)
        self.managed = False
        self.colour = discord.Colour.default()
        self.secondary_colour = self.tertiary_colour = self.display_icon = None
        self.hoist = self.mentionable = False
        self.edit = AsyncMock(side_effect=self._edit)
        self.delete = AsyncMock(side_effect=self._delete)

    def __lt__(self, other):
        return (self.position, -self.id) < (other.position, -other.id)

    def __hash__(self):
        return hash(self.id)

    def is_default(self):
        return self.id == self.guild.id

    async def _edit(self, **kwargs):
        for key, value in kwargs.items():
            if key != 'reason':
                setattr(self, {'color': 'colour', 'secondary_color': 'secondary_colour', 'tertiary_color': 'tertiary_colour'}.get(key, key), value)
        return self

    async def _delete(self, **kwargs):
        self.guild.roles.remove(self)


class Member:
    def __init__(self, member_id, roles=()):
        self.id, self._roles = member_id, list(roles)
        self.name = self.display_name = f'member{member_id}'
        self.global_name = None
        self.add_roles = AsyncMock(side_effect=self._add)
        self.remove_roles = AsyncMock(side_effect=self._remove)

    async def _add(self, role, **kwargs):
        if role.id not in self._roles:
            self._roles.append(role.id)

    async def _remove(self, role, **kwargs):
        if role.id in self._roles:
            self._roles.remove(role.id)


class CosmeticRoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CosmeticRoleStore(Path(self.temp.name) / 'cosmetic.sqlite3')
        self.guild = SimpleNamespace(id=100, owner_id=900, roles=[], features=['ENHANCED_ROLE_COLORS'], chunked=True)
        self.guild.roles = [
            Role(self.guild, 100, '@everyone', 0),
            Role(self.guild, 102, '---  幻化区结束 ---', 1),
            Role(self.guild, 110, '旧幻化身份组', 2),
            Role(self.guild, 103, '---  幻化区开始 ---', 3),
            Role(self.guild, 104, '幻化权区', 4, discord.Permissions(manage_roles=True).value),
            Role(self.guild, 105, 'admin', 5, discord.Permissions(administrator=True).value),
            Role(self.guild, 106, 'bot', 6, discord.Permissions(manage_roles=True).value),
        ]
        self.requester, self.other, self.bot_member = Member(301), Member(302), Member(999, [106])
        self.guild.members = [self.requester, self.other, self.bot_member, Member(900)]
        self.guild.get_role = lambda value: next((role for role in self.guild.roles if role.id == value), None)
        self.guild.get_member = lambda value: next((member for member in self.guild.members if member.id == value), None)
        self.guild.fetch_member = AsyncMock(side_effect=lambda value: self.guild.get_member(value))
        self.guild.fetch_roles = AsyncMock(side_effect=lambda: list(self.guild.roles))
        self.channel = SimpleNamespace(id=200, overwrites={})
        self.guild.fetch_channels = AsyncMock(side_effect=lambda: [self.channel])
        self.guild.create_role = AsyncMock(side_effect=self.create_role)
        self.guild.edit_role_positions = AsyncMock(side_effect=self.move_roles)
        self.next_id = 2000
        self.bot = SimpleNamespace(user=SimpleNamespace(id=999), _atri_cosmetic_role_store=self.store)
        self.message = SimpleNamespace(id=500, author=self.requester, guild=self.guild, channel=self.channel, attachments=[])
        self.host = DiscordToolHost(bot=self.bot, message=self.message, owner_user_id=900, confirmation_handler=AsyncMock(return_value=True))
        self.host._role_icon_kwargs = AsyncMock(return_value=({}, None))
        self.service = CosmeticRoleHost(self.host, store=self.store)

    async def create_role(self, **kwargs):
        self.next_id += 1
        for role in self.guild.roles:
            if not role.is_default():
                role.position += 1
        created = Role(self.guild, self.next_id, kwargs['name'], 1, kwargs['permissions'].value)
        await created._edit(**{key: value for key, value in kwargs.items() if key != 'permissions'})
        self.guild.roles.append(created)
        return created

    async def move_roles(self, *, positions, **kwargs):
        ordered = sorted(self.guild.roles)
        for role, destination in positions.items():
            ordered.remove(role)
            ordered.insert(destination, role)
        for position, role in enumerate(ordered):
            role.position = position
        return ordered

    def register_legacy(self, owner=301, public=False):
        self.store.put(100, 110, owner, public)
        return self.guild.get_role(110)

    async def test_feature_requires_both_unique_correctly_ordered_markers(self):
        self.assertTrue(cosmetic_enabled(self.guild))
        self.guild.roles.remove(self.guild.get_role(102))
        self.assertFalse(cosmetic_enabled(self.guild))
        with self.assertRaisesRegex(DiscordToolError, '未启用'):
            await self.service.execute('create', {'name': 'no'})
        self.guild.create_role.assert_not_awaited()

    async def test_duplicate_marker_disables_mutations(self):
        self.guild.roles.append(Role(self.guild, 107, '--- 幻化区开始 ---', 4))
        with self.assertRaisesRegex(DiscordToolError, '唯一'):
            await self.service.execute('create', {'name': 'no'})
        self.guild.create_role.assert_not_awaited()

    async def test_reversed_markers_disable_mutations(self):
        self.guild.get_role(102).position = 10
        with self.assertRaisesRegex(DiscordToolError, '顺序'):
            await self.service.execute('create', {'name': 'no'})

    async def test_ordinary_member_creation_is_permissionless_and_inside_region(self):
        old_order = [role.id for role in sorted(self.guild.roles)]
        result = await self.host.execute('cosmetic_create', {'name': '紫云', 'role_color_style': 'gradient', 'color': 0x112233, 'secondary_color': 0xAABBCC})
        data = json.loads(result['content'])
        role = self.guild.get_role(int(data['id']))
        ctx = await self.service.snapshot()
        self.assertTrue(ctx.inside(role))
        self.assertEqual(role.permissions.value, 0)
        self.assertFalse(role.hoist)
        self.assertFalse(role.mentionable)
        self.assertEqual(data['creatorId'], '301')
        self.assertEqual(data['colorStyle'], 'gradient')
        self.assertFalse(data['public'])
        self.assertTrue(data['equipped'])
        self.assertEqual(old_order, [item.id for item in sorted(self.guild.roles) if item.id != role.id])
        self.assertEqual(self.requester.add_roles.await_args.args[0].id, role.id)

    async def test_same_creation_request_is_idempotent(self):
        first = await self.service.execute('create', {'name': '一次'})
        again = await self.service.execute('create', {'name': '一次'})
        self.assertEqual(json.loads(first['content'])['id'], json.loads(again['content'])['id'])
        self.guild.create_role.assert_awaited_once()

    async def test_per_member_quota_and_creation_cooldown(self):
        await self.service.execute('create', {'name': 'first'})
        with self.assertRaisesRegex(DiscordToolError, '30 秒'):
            await self.service.execute('create', {'name': 'second'})
        self.register_legacy()
        with self.assertRaisesRegex(DiscordToolError, '名额已满'):
            await self.service.execute('create', {'name': 'third'})

    async def test_privilege_quota_comes_from_this_guild_outside_region(self):
        self.requester._roles = [104]
        result = await self.service.execute('status', {})
        self.assertEqual(json.loads(result['content'])['myQuota'], 20)
        self.guild.get_role(104).position = 2
        result = await self.service.execute('status', {})
        self.assertEqual(json.loads(result['content'])['myQuota'], 2)

    async def test_duplicate_privilege_name_does_not_grant_quota(self):
        self.requester._roles = [104]
        self.guild.roles.append(Role(self.guild, 107, '幻化权区', 7))
        result = await self.service.execute('status', {})
        self.assertEqual(json.loads(result['content'])['myQuota'], 2)

    async def test_region_capacity_counts_unregistered_existing_roles(self):
        self.store.save_settings(100, dict(normal_limit=2, privileged_limit=20, area_limit=1, privileged_role_ids=['104']))
        with self.assertRaisesRegex(DiscordToolError, '总量'):
            await self.service.execute('create', {'name': 'no'})

    async def test_no_permission_position_or_other_member_parameters(self):
        for extra in ({'permission_names': ['administrator']}, {'position': 9}, {'target_id': '302'}, {'guild_id': '101'}, {'hoist': True}):
            with self.subTest(extra=extra), self.assertRaisesRegex(DiscordToolError, '未声明参数'):
                await self.service.execute('create', {'name': 'no', **extra})
        self.guild.create_role.assert_not_awaited()

    async def test_general_discord_management_is_still_admin_only(self):
        with self.assertRaisesRegex(DiscordToolError, 'Administrator'):
            await self.host.execute('create_role', {'name': 'not cosmetic'})
        self.guild.create_role.assert_not_awaited()

    async def test_bot_must_be_above_upper_boundary(self):
        self.guild.get_role(106).position = 2
        with self.assertRaisesRegex(DiscordToolError, '最高身份组'):
            await self.service.execute('create', {'name': 'no'})

    async def test_bot_needs_manage_roles_permission(self):
        self.guild.get_role(106).permissions = discord.Permissions.none()
        with self.assertRaisesRegex(DiscordToolError, '管理身份组'):
            await self.service.execute('create', {'name': 'no'})

    async def test_markers_and_outside_roles_are_never_operable(self):
        for role_id in (102, 103, 104, 105):
            with self.subTest(role_id=role_id), self.assertRaisesRegex(DiscordToolError, '之间'):
                await self.service.execute('equip', {'role_ref': str(role_id)})
        self.requester.add_roles.assert_not_awaited()

    async def test_permission_bearing_registered_role_is_blocked(self):
        role = self.register_legacy(public=True)
        role.permissions = discord.Permissions(manage_channels=True)
        with self.assertRaisesRegex(DiscordToolError, '带有权限'):
            await self.service.execute('equip', {'role_ref': str(role.id)})
        self.requester.add_roles.assert_not_awaited()

    async def test_channel_overwrite_role_cannot_be_worn_edited_or_deleted(self):
        role = self.register_legacy(public=True)
        self.channel.overwrites = {role: discord.PermissionOverwrite(view_channel=True)}
        for action in ('equip', 'delete', 'edit'):
            with self.subTest(action=action), self.assertRaisesRegex(DiscordToolError, '频道权限'):
                await self.service.execute(action, {'role_ref': str(role.id)})
        role.delete.assert_not_awaited()
        role.edit.assert_not_awaited()

    async def test_only_creator_can_edit_or_delete_even_if_requester_is_admin(self):
        role = self.register_legacy(owner=302, public=True)
        self.requester._roles.append(105)
        for action in ('edit', 'delete'):
            with self.subTest(action=action), self.assertRaisesRegex(DiscordToolError, '自己创建'):
                await self.service.execute(action, {'role_ref': str(role.id)})

    async def test_owner_can_change_colors_name_and_visibility(self):
        role = self.register_legacy()
        result = await self.service.execute('edit', {'role_ref': '旧幻化身份组', 'name': '改名', 'color': 0xABCDEF, 'public': True})
        self.assertEqual(json.loads(result['content'])['name'], '改名')
        self.assertTrue(self.store.records(100)[role.id]['public'])
        self.assertNotIn('permissions', role.edit.await_args.kwargs)
        self.assertNotIn('position', role.edit.await_args.kwargs)

    async def test_public_role_can_only_be_equipped_on_requester(self):
        role = self.register_legacy(owner=302, public=True)
        await self.service.execute('equip', {'role_ref': str(role.id)})
        self.requester.add_roles.assert_awaited_once_with(role, reason='ATRI cosmetic equip; requester=301', atomic=True)
        self.other.add_roles.assert_not_awaited()
        await self.service.execute('unequip', {'role_ref': str(role.id)})
        self.requester.remove_roles.assert_awaited_once()

    async def test_private_role_cannot_be_claimed_by_someone_else(self):
        role = self.register_legacy(owner=302)
        with self.assertRaisesRegex(DiscordToolError, '未公开'):
            await self.service.execute('equip', {'role_ref': str(role.id)})

    async def test_unregistered_roles_require_adoption_for_editing_not_wearing(self):
        with self.assertRaisesRegex(DiscordToolError, '尚未登记'):
            await self.service.execute('edit', {'role_ref': '110', 'name': 'no'})
        with self.assertRaisesRegex(DiscordToolError, '管理员'):
            await self.service.execute('adopt', {'role_ref': '110', 'owner_ref': '301'})
        self.requester._roles.append(105)
        result = await self.service.execute('adopt', {'role_ref': '110', 'owner_ref': '302', 'public': True})
        self.assertEqual(json.loads(result['content'])['creatorId'], '302')
        self.guild.get_role(110).edit.assert_not_awaited()
        with self.assertRaisesRegex(DiscordToolError, '不能覆盖'):
            await self.service.execute('adopt', {'role_ref': '110', 'owner_ref': '301'})

    async def test_legacy_role_is_public_for_self_wearing_without_registration(self):
        await self.service.execute('equip', {'role_ref': '110'})
        self.assertIn(110, self.requester._roles)
        self.other.add_roles.assert_not_awaited()
        await self.service.execute('unequip', {'role_ref': '110'})
        self.assertNotIn(110, self.requester._roles)
        self.assertEqual(self.store.records(100), {})
        data = json.loads((await self.service.execute('list', {}))['content'])
        role = data['roles'][0]
        self.assertTrue(role['public'] and role['legacy'] and role['canEquipSelf'])
        self.assertFalse(role['registered'] or role['canEditOwn'])
        self.assertEqual(data['myCreatedCount'], 0)

    async def test_legacy_self_wearing_does_not_grant_edit_or_delete(self):
        role = self.guild.get_role(110)
        for action in ('edit', 'delete'):
            with self.subTest(action=action), self.assertRaisesRegex(DiscordToolError, '尚未登记'):
                await self.service.execute(action, {'role_ref': '110'})
        role.edit.assert_not_awaited()
        role.delete.assert_not_awaited()
        self.host.confirmation_handler.assert_not_awaited()

    async def test_legacy_roles_with_permissions_or_overwrites_are_not_publicly_wearable(self):
        role = self.guild.get_role(110)
        role.permissions = discord.Permissions(manage_roles=True)
        with self.assertRaisesRegex(DiscordToolError, '带有权限'):
            await self.service.execute('equip', {'role_ref': '110'})
        role.permissions = discord.Permissions.none()
        self.channel.overwrites = {role: discord.PermissionOverwrite(view_channel=True)}
        with self.assertRaisesRegex(DiscordToolError, '频道权限'):
            await self.service.execute('equip', {'role_ref': '110'})
        self.requester.add_roles.assert_not_awaited()

    async def test_pending_creation_is_not_treated_as_public_legacy(self):
        self.store.put(100, 110, 302, False, state='pending')
        with self.assertRaisesRegex(DiscordToolError, '尚未登记'):
            await self.service.execute('equip', {'role_ref': '110'})
        self.requester.add_roles.assert_not_awaited()

    async def test_large_legacy_list_is_complete_json_with_pagination_and_search(self):
        self.guild.get_role(103).position = 100
        self.guild.get_role(104).position = 101
        self.guild.get_role(105).position = 102
        self.guild.get_role(106).position = 110
        for index in range(60):
            self.guild.roles.append(Role(self.guild, 3000 + index, f'很长的身份组名称{index}' + '甲' * 80, 3 + index))
        seen, offset = set(), 0
        while True:
            result = await self.service.execute('list', {'offset': offset, 'limit': 25})
            self.assertFalse(result['truncated'])
            self.assertLess(len(result['content']), 12000)
            data = json.loads(result['content'])
            self.assertEqual(data['totalCount'], 61)
            seen.update(role['id'] for role in data['roles'])
            if not data['hasMore']:
                break
            self.assertGreater(data['nextOffset'], offset)
            offset = data['nextOffset']
        self.assertEqual(len(seen), 61)
        result = await self.service.execute('list', {'query': '旧幻化'})
        data = json.loads(result['content'])
        self.assertEqual(data['totalCount'], 1)
        self.assertEqual(data['roles'][0]['id'], '110')

    async def test_delete_requires_same_requester_confirmation(self):
        role = self.register_legacy()
        self.host.confirmation_handler.return_value = False
        with self.assertRaisesRegex(DiscordToolError, 'not confirmed'):
            await self.service.execute('delete', {'role_ref': '110'})
        role.delete.assert_not_awaited()
        self.host.confirmation_handler.return_value = True
        await self.service.execute('delete', {'role_ref': '110'})
        role.delete.assert_awaited_once()
        self.assertNotIn(110, self.store.records(100))

    async def test_boundaries_are_rechecked_after_confirmation(self):
        role = self.register_legacy()
        async def move_out(*_args):
            role.position = 9
            return True
        self.host.confirmation_handler.side_effect = move_out
        with self.assertRaisesRegex(DiscordToolError, '之间'):
            await self.service.execute('delete', {'role_ref': '110'})
        role.delete.assert_not_awaited()

    async def test_permission_changes_during_icon_download_stop_edit(self):
        role = self.register_legacy()
        async def grant_permission(*_args, **_kwargs):
            role.permissions = discord.Permissions(manage_roles=True)
            return {}, None
        self.host._role_icon_kwargs.side_effect = grant_permission
        with self.assertRaisesRegex(DiscordToolError, '带有权限'):
            await self.service.execute('edit', {'role_ref': '110', 'name': 'new'})
        role.edit.assert_not_awaited()

    async def test_failed_placement_keeps_pending_without_rollback_requests(self):
        before = {role.id for role in self.guild.roles}
        self.guild.edit_role_positions.side_effect = RuntimeError('placement failed')
        with self.assertRaisesRegex(RuntimeError, 'placement failed'):
            await self.service.execute('create', {'name': 'failed'})
        created = ({role.id for role in self.guild.roles} - before).pop()
        self.assertEqual(self.store.records(100)[created]['state'], 'pending')
        self.guild.get_role(created).delete.assert_not_awaited()
        self.assertEqual(self.guild.fetch_channels.await_count, 2)
        self.requester.add_roles.assert_not_awaited()

    async def test_equip_failure_keeps_created_role_and_does_not_duplicate(self):
        self.requester.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'no')
        result = await self.service.execute('create', {'name': 'exists'})
        self.assertFalse(json.loads(result['content'])['equipped'])
        self.assertIn('不要重复创建', json.loads(result['content'])['equipError'])
        await self.service.execute('create', {'name': 'exists'})
        self.guild.create_role.assert_awaited_once()

    async def test_failed_placement_preserves_new_role_changed_by_administrator(self):
        async def change_new_role(*, positions, **kwargs):
            role = next(iter(positions))
            role.permissions = discord.Permissions(manage_roles=True)
            raise RuntimeError('concurrent administrative change')
        self.guild.edit_role_positions.side_effect = change_new_role
        with self.assertLogs('chat.agent.cosmetic_roles', level='WARNING'), self.assertRaises(RuntimeError):
            await self.service.execute('create', {'name': 'changed'})
        record = next(iter(self.store.records(100).values()))
        self.assertEqual(record['state'], 'pending')
        self.guild.get_role(int(record['role_id'])).delete.assert_not_awaited()
        self.requester.add_roles.assert_not_awaited()

    def enable_gateway(self):
        self.bot.is_ready = lambda: True
        self.bot.get_guild = lambda guild_id: self.guild if guild_id == self.guild.id else None
        self.bot.intents = SimpleNamespace(guilds=True)
        self.bot.ws = SimpleNamespace(socket=SimpleNamespace(closed=False))
        self.guild.unavailable = False
        self.guild.channels = [self.channel]

    async def test_gateway_creation_never_fetches_channels_and_places_immediately(self):
        self.enable_gateway()
        self.guild.fetch_channels.side_effect = AssertionError('must use live Gateway cache')
        events = []
        async def roles():
            events.append('read_roles')
            return list(self.guild.roles)
        async def create(**kwargs):
            events.append('create')
            return await self.create_role(**kwargs)
        async def move(**kwargs):
            events.append('place')
            return await self.move_roles(**kwargs)
        self.guild.fetch_roles.side_effect = roles
        self.guild.create_role.side_effect = create
        self.guild.edit_role_positions.side_effect = move
        old_lower_position = self.guild.get_role(102).position
        result = await self.service.execute('create', {'name': '立即入区'})
        role = self.guild.get_role(int(json.loads(result['content'])['id']))
        self.assertEqual(events[-2:], ['create', 'place'])
        self.assertEqual(self.guild.edit_role_positions.await_args.kwargs['positions'][role], old_lower_position + 1)
        self.assertTrue(self.guild.get_role(102) < role < self.guild.get_role(103))
        self.guild.fetch_channels.assert_not_awaited()
        self.assertEqual(self.guild.fetch_roles.await_count, 2)

    async def test_disconnected_gateway_does_not_authorize_from_stale_channels(self):
        self.enable_gateway()
        self.bot.ws.socket.closed = True
        await self.service.execute('status', {})
        self.guild.fetch_channels.assert_awaited_once()

    async def test_gateway_channel_overwrite_update_still_blocks_mutations(self):
        self.enable_gateway()
        role = self.register_legacy()
        async def change(*args, **kwargs):
            self.channel.overwrites = {role: discord.PermissionOverwrite(view_channel=True)}
            return {}, None
        self.host._role_icon_kwargs.side_effect = change
        with self.assertRaisesRegex(DiscordToolError, '频道权限'):
            await self.service.execute('edit', {'role_ref': '110', 'name': 'no'})
        role.edit.assert_not_awaited()
        self.guild.fetch_channels.assert_not_awaited()

    async def test_rate_limit_has_specific_error_and_shared_cooldown(self):
        self.guild.fetch_channels.side_effect = discord.RateLimited(50)
        with self.assertRaisesRegex(DiscordToolError, 'channels 限流'):
            await self.service.execute('create', {'name': 'no'})
        second = CosmeticRoleHost(self.host, store=self.store)
        with self.assertRaisesRegex(DiscordToolError, 'channels 请求冷却'):
            await second.execute('create', {'name': 'no'})
        self.guild.fetch_channels.assert_awaited_once()
        self.guild.create_role.assert_not_awaited()

    async def test_timeout_cancels_request_and_does_not_retry_immediately(self):
        cancelled = asyncio.Event()
        async def hanging():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.guild.fetch_roles.side_effect = hanging
        with patch('chat.agent.cosmetic_roles.DISCORD_REQUEST_TIMEOUT', 0.01):
            with self.assertRaisesRegex(DiscordToolError, 'roles.*超时'):
                await self.service.execute('status', {})
        self.assertTrue(cancelled.is_set())
        with self.assertRaisesRegex(DiscordToolError, '请求冷却'):
            await self.service.execute('status', {})
        self.guild.fetch_roles.assert_awaited_once()

    async def test_position_failure_retries_same_role_across_messages(self):
        self.guild.edit_role_positions.side_effect = discord.RateLimited(1)
        with self.assertRaisesRegex(DiscordToolError, 'resume'):
            await self.service.execute('create', {'name': '保留原组'})
        role_id = next(iter(self.store.records(100)))
        self.message.id += 1
        with self.assertRaisesRegex(DiscordToolError, '请求冷却'):
            await self.service.execute('create', {'name': '保留原组'})
        self.guild.create_role.assert_awaited_once()
        self.service.cooldowns.clear()
        self.guild.edit_role_positions.side_effect = self.move_roles
        result = await self.service.execute('create', {'name': '保留原组'})
        self.assertEqual(json.loads(result['content'])['id'], str(role_id))
        self.assertEqual(self.store.records(100)[role_id]['state'], 'active')
        self.guild.create_role.assert_awaited_once()

    async def test_pending_role_with_different_name_blocks_duplicate_creation(self):
        self.store.put(100, 110, 301, False, state='pending')
        with self.assertRaisesRegex(DiscordToolError, '未完成'):
            await self.service.execute('create', {'name': '另一种标题'})
        self.guild.create_role.assert_not_awaited()

    async def test_resume_only_accepts_pending_creator_or_manager_without_equip(self):
        with self.assertRaisesRegex(DiscordToolError, '未完成创建记录'):
            await self.service.execute('resume', {'role_ref': '110'})
        self.store.put(100, 110, 302, False, state='pending')
        with self.assertRaisesRegex(DiscordToolError, '自己未完成'):
            await self.service.execute('resume', {'role_ref': '110'})
        self.requester._roles.append(105)
        with self.assertRaisesRegex(DiscordToolError, 'equip=false'):
            await self.service.execute('resume', {'role_ref': '110'})
        await self.service.execute('resume', {'role_ref': '110', 'equip': False})
        self.assertEqual(self.store.records(100)[110]['creator_id'], '302')
        self.requester.add_roles.assert_not_awaited()
        self.other.add_roles.assert_not_awaited()

    async def test_resume_rejects_changed_permissions_name_or_boundary(self):
        self.store.put(100, 110, 301, False, state='pending', expected_name='原名', start_role_id=103, end_role_id=102)
        with self.assertRaisesRegex(DiscordToolError, '改名'):
            await self.service.execute('resume', {'role_ref': '110'})
        self.guild.get_role(110).name = '原名'
        self.guild.get_role(110).permissions = discord.Permissions(manage_roles=True)
        with self.assertRaisesRegex(DiscordToolError, '带有权限'):
            await self.service.execute('resume', {'role_ref': '110'})
        self.guild.get_role(110).permissions = discord.Permissions.none()
        self.guild.get_role(103).id = 108
        with self.assertRaisesRegex(DiscordToolError, '边界已被替换'):
            await self.service.execute('resume', {'role_ref': '110'})
        self.guild.edit_role_positions.assert_not_awaited()

    async def test_cancellation_after_create_preserves_recoverable_record(self):
        self.guild.edit_role_positions.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.execute('create', {'name': '被取消'})
        role_id = next(iter(self.store.records(100)))
        self.assertEqual(self.store.records(100)[role_id]['state'], 'pending')
        self.guild.get_role(role_id).delete.assert_not_awaited()
        self.guild.edit_role_positions.side_effect = self.move_roles
        await self.service.execute('resume', {'role_ref': str(role_id), 'equip': False})
        self.assertEqual(self.store.records(100)[role_id]['state'], 'active')
        self.guild.create_role.assert_awaited_once()

    async def test_unmoved_success_response_is_not_reported_as_success(self):
        self.guild.edit_role_positions.side_effect = lambda **kwargs: list(self.guild.roles)
        with self.assertRaisesRegex(DiscordToolError, '放置／验证未完成'):
            await self.service.execute('create', {'name': '位置未变'})
        role_id = next(iter(self.store.records(100)))
        self.assertEqual(self.store.records(100)[role_id]['state'], 'pending')
        self.requester.add_roles.assert_not_awaited()
        data = json.loads((await self.service.execute('mine', {}))['content'])
        self.assertEqual(data['myPendingRoleIds'], [str(role_id)])
        self.assertEqual(data['roles'][0]['state'], 'pending')

    async def test_metadata_extension_preserves_old_table_and_old_pending_rows(self):
        with self.store.connect() as db:
            self.assertEqual(len(db.execute('PRAGMA table_info(cosmetic_roles)').fetchall()), 7)
            db.execute('INSERT INTO cosmetic_roles VALUES (?, ?, ?, ?, ?, ?, ?)', ('100', '110', '301', 0, 'pending', 'old', time.time()))
        reloaded = CosmeticRoleStore(self.store.path)
        self.assertIsNone(reloaded.records(100)[110]['expected_name'])
        self.service.store = reloaded
        await self.service.execute('resume', {'role_ref': '110', 'equip': False})
        self.assertEqual(reloaded.records(100)[110]['state'], 'active')

    async def test_user_ref_cannot_be_misinterpreted_as_equip_target(self):
        with self.assertRaisesRegex(DiscordToolError, '头像'):
            await self.service.execute('create', {'name': 'no', 'user_ref': '302'})
        self.guild.create_role.assert_not_awaited()

    async def test_slash_configuration_and_adoption_use_same_authorization(self):
        from chat.cosmetic_commands import cosmetic_command
        interaction = SimpleNamespace(id=501, guild=self.guild, channel=self.channel, user=self.requester,
                                      response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        cog = SimpleNamespace(bot=self.bot, owner_user_id=900)
        await cosmetic_command(cog, interaction, 'configure', {'normal_limit': 9})
        self.assertIsNone(self.store.settings(100))
        self.assertIn('管理员', interaction.followup.send.call_args.args[0])
        self.requester._roles.append(105)
        await cosmetic_command(cog, interaction, 'status', {})
        embed = interaction.followup.send.call_args.kwargs['embed']
        self.assertIn('2 个', embed.description)
        await cosmetic_command(cog, interaction, 'adopt', {'role_ref': '110', 'owner_ref': '302'})
        embed = interaction.followup.send.call_args.kwargs['embed']
        self.assertIn('<@302>', embed.description)
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])

    async def test_reserved_names_and_existing_names_are_rejected(self):
        for name in ('---  幻化区开始 ---', '幻化权区', '@everyone', 'admin'):
            with self.subTest(name=name), self.assertRaises(DiscordToolError):
                await self.service.execute('create', {'name': name})
        self.guild.create_role.assert_not_awaited()

    async def test_configuration_is_admin_only_and_quota_role_must_be_outside(self):
        with self.assertRaisesRegex(DiscordToolError, '管理员'):
            await self.service.execute('configure', {'normal_limit': 3})
        self.requester._roles.append(105)
        with self.assertRaisesRegex(DiscordToolError, '之外'):
            await self.service.execute('configure', {'privileged_role_ids': ['110']})
        await self.service.execute('configure', {'normal_limit': 3, 'privileged_role_ids': ['104']})
        self.assertEqual(self.store.settings(100)['normal_limit'], 3)

    async def test_ownership_and_config_survive_reload_and_are_guild_scoped(self):
        self.register_legacy()
        self.store.save_settings(100, dict(normal_limit=3, privileged_limit=20, area_limit=100, privileged_role_ids=['104']))
        reloaded = CosmeticRoleStore(self.store.path)
        self.assertEqual(reloaded.records(100)[110]['creator_id'], '301')
        self.assertEqual(reloaded.records(101), {})
        self.assertIsNone(reloaded.settings(101))

    async def test_concurrent_creations_cannot_exceed_quota(self):
        self.register_legacy()
        with self.store.connect() as db:
            db.execute('UPDATE cosmetic_roles SET created_at = ?', (time.time() - 60,))
        message = SimpleNamespace(**vars(self.message))
        message.id = 501
        other_host = DiscordToolHost(bot=self.bot, message=message, owner_user_id=900)
        other_host._role_icon_kwargs = AsyncMock(return_value=({}, None))
        other_service = CosmeticRoleHost(other_host, store=self.store)
        results = await asyncio.gather(self.service.execute('create', {'name': 'A'}), other_service.execute('create', {'name': 'B'}), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(len(self.store.records(100)), 2)

    async def test_fresh_role_ref_roundtrip_does_not_require_name_or_id_guessing(self):
        self.register_legacy(public=True)
        result = await self.service.execute('list', {})
        ref = json.loads(result['content'])['roles'][0]['roleRef']
        await self.service.execute('equip', {'role_ref': ref})
        self.assertIn(110, self.requester._roles)

    async def test_network_failure_during_boundary_check_does_not_mutate(self):
        self.guild.fetch_roles.side_effect = OSError('offline')
        with self.assertRaisesRegex(DiscordToolError, '停止操作'):
            await self.service.execute('create', {'name': 'no'})
        self.guild.create_role.assert_not_awaited()
