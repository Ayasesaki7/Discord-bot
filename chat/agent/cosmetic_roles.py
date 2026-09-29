"""Guild-scoped self-service cosmetic roles, separate from server management."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import discord

from .discord_tools import DiscordToolError

START_NAME = '--- 幻化区开始 ---'
END_NAME = '--- 幻化区结束 ---'
PRIVILEGE_NAME = '幻化权区'
DEFAULT_NORMAL_LIMIT = 2
DEFAULT_PRIVILEGED_LIMIT = 20
DEFAULT_AREA_LIMIT = 100
DISCORD_REQUEST_TIMEOUT = 45
logger = logging.getLogger(__name__)


def normalized_name(value: str) -> str:
    return ' '.join(value.split())


def boundaries(roles):
    starts = [role for role in roles if normalized_name(role.name) == START_NAME]
    ends = [role for role in roles if normalized_name(role.name) == END_NAME]
    if len(starts) != 1 or len(ends) != 1:
        raise DiscordToolError('幻化功能未启用：需要唯一的「---  幻化区开始 ---」和「---  幻化区结束 ---」身份组。')
    upper, lower = starts[0], ends[0]
    if upper.is_default() or lower.is_default() or not upper > lower:
        raise DiscordToolError('幻化区边界顺序无效：开始身份组必须位于结束身份组上方。')
    return upper, lower


def cosmetic_enabled(guild) -> bool:
    try:
        boundaries(getattr(guild, 'roles', ()))
        return True
    except DiscordToolError:
        return False


class CosmeticRoleStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cosmetic_roles (
                    guild_id TEXT NOT NULL, role_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL, public INTEGER NOT NULL,
                    state TEXT NOT NULL, request_key TEXT, created_at REAL NOT NULL,
                    PRIMARY KEY (guild_id, role_id), UNIQUE (guild_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS cosmetic_settings (
                    guild_id TEXT PRIMARY KEY, normal_limit INTEGER NOT NULL,
                    privileged_limit INTEGER NOT NULL, area_limit INTEGER NOT NULL,
                    privileged_role_ids TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cosmetic_creation_state (
                    guild_id TEXT NOT NULL, role_id TEXT NOT NULL,
                    start_role_id TEXT, end_role_id TEXT, expected_name TEXT, last_error TEXT,
                    PRIMARY KEY (guild_id, role_id)
                );
            ''')
        if os.name != 'nt':
            os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def records(self, guild_id: int) -> dict[int, dict]:
        with self.connect() as db:
            return {int(row['role_id']): dict(row) for row in db.execute(
                '''SELECT r.*, s.start_role_id, s.end_role_id, s.expected_name, s.last_error
                   FROM cosmetic_roles r LEFT JOIN cosmetic_creation_state s
                   ON r.guild_id = s.guild_id AND r.role_id = s.role_id WHERE r.guild_id = ?''', (str(guild_id),))}

    def put(self, guild_id: int, role_id: int, creator_id: int, public: bool,
            *, state='active', request_key=None, start_role_id=None, end_role_id=None, expected_name=None):
        with self.connect() as db:
            db.execute('''INSERT INTO cosmetic_roles
                       (guild_id, role_id, creator_id, public, state, request_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (str(guild_id), str(role_id), str(creator_id), int(public), state, request_key, time.time()))
            db.execute('''INSERT INTO cosmetic_creation_state
                       (guild_id, role_id, start_role_id, end_role_id, expected_name) VALUES (?, ?, ?, ?, ?)''',
                       (str(guild_id), str(role_id), str(start_role_id) if start_role_id else None,
                        str(end_role_id) if end_role_id else None, expected_name))

    def failure(self, guild_id: int, role_id: int, error: str):
        with self.connect() as db:
            db.execute('''INSERT INTO cosmetic_creation_state (guild_id, role_id, last_error) VALUES (?, ?, ?)
                       ON CONFLICT(guild_id, role_id) DO UPDATE SET last_error = excluded.last_error''',
                       (str(guild_id), str(role_id), error[:500]))

    def update(self, guild_id: int, role_id: int, *, public: bool, state='active'):
        with self.connect() as db:
            db.execute('UPDATE cosmetic_roles SET public = ?, state = ? WHERE guild_id = ? AND role_id = ?',
                       (int(public), state, str(guild_id), str(role_id)))
            db.execute('UPDATE cosmetic_creation_state SET last_error = NULL WHERE guild_id = ? AND role_id = ?',
                       (str(guild_id), str(role_id)))

    def remove(self, guild_id: int, role_id: int):
        with self.connect() as db:
            db.execute('DELETE FROM cosmetic_roles WHERE guild_id = ? AND role_id = ?', (str(guild_id), str(role_id)))
            db.execute('DELETE FROM cosmetic_creation_state WHERE guild_id = ? AND role_id = ?', (str(guild_id), str(role_id)))

    def settings(self, guild_id: int):
        with self.connect() as db:
            row = db.execute('SELECT * FROM cosmetic_settings WHERE guild_id = ?', (str(guild_id),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['privileged_role_ids'] = json.loads(result['privileged_role_ids'])
        return result

    def save_settings(self, guild_id: int, config: dict):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO cosmetic_settings VALUES (?, ?, ?, ?, ?)',
                       (str(guild_id), config['normal_limit'], config['privileged_limit'], config['area_limit'],
                        json.dumps(config['privileged_role_ids'])))


@dataclass
class CosmeticContext:
    guild: Any
    roles: dict
    channels: list
    upper: Any
    lower: Any
    requester: Any
    requester_permissions: discord.Permissions
    bot_top: Any
    bot_permissions: discord.Permissions
    settings: dict
    bot_member: Any = None

    def inside(self, role) -> bool:
        return self.lower < role < self.upper


_STYLE_FIELDS = {
    'name', 'role_color_style', 'color', 'secondary_color', 'tertiary_color',
    'unicode_emoji', 'clear_role_icon', 'source_type', 'source_url',
    'source_emoji_id', 'source_sticker_id', 'attachment_index',
    'channel_id', 'message_id', 'user_ref',
}
_ACTION_FIELDS = {
    'status': set(), 'list': {'query', 'offset', 'limit'}, 'mine': {'query', 'offset', 'limit'},
    'create': _STYLE_FIELDS | {'public', 'equip'},
    'edit': _STYLE_FIELDS | {'role_ref', 'public'},
    'delete': {'role_ref'}, 'equip': {'role_ref'}, 'unequip': {'role_ref'},
    'resume': {'role_ref', 'equip'},
    'adopt': {'role_ref', 'owner_ref', 'public'},
    'configure': {'normal_limit', 'privileged_limit', 'area_limit', 'privileged_role_ids'},
}


class CosmeticRoleHost:
    def __init__(self, host, *, store: CosmeticRoleStore | None = None):
        self.host = host
        self.bot = host.bot
        if store is None:
            store = getattr(self.bot, '_atri_cosmetic_role_store', None)
            if store is None:
                store = CosmeticRoleStore(Path(__file__).resolve().parents[2] / 'config/agent/cosmetic_roles.sqlite3')
                self.bot._atri_cosmetic_role_store = store
        self.store = store
        locks = getattr(self.bot, '_atri_cosmetic_role_locks', None)
        if locks is None:
            locks = {}
            self.bot._atri_cosmetic_role_locks = locks
        self.lock = locks.setdefault(host._guild().id, asyncio.Lock())
        if not hasattr(self.bot, '_atri_cosmetic_cooldowns'):
            self.bot._atri_cosmetic_cooldowns = {}
        self.cooldowns = self.bot._atri_cosmetic_cooldowns

    @staticmethod
    def _member_permissions(member, roles, guild):
        ids = {int(value) for value in member._roles} | {guild.id}
        value = 0
        owned = []
        for role in roles:
            if role.id in ids:
                value |= role.permissions.value
                owned.append(role)
        permissions = discord.Permissions(value)
        if member.id == guild.owner_id or permissions.administrator:
            permissions = discord.Permissions.all()
        return permissions, max(owned) if owned else None

    def _gateway_channels(self, guild):
        # Guild/channel updates are applied to this cache before events are
        # dispatched. Never trust a detached REST Guild or a disconnected cache.
        bot = self.bot
        ready = getattr(bot, 'is_ready', None)
        get_guild = getattr(bot, 'get_guild', None)
        socket = getattr(getattr(bot, 'ws', None), 'socket', None)
        if (callable(ready) and ready() and callable(get_guild) and get_guild(guild.id) is guild
                and getattr(getattr(bot, 'intents', None), 'guilds', False)
                and socket is not None and not socket.closed and not getattr(guild, 'unavailable', True)):
            return list(guild.channels)
        return None

    def _request_ready(self, operation):
        key = (self.host._guild().id, operation)
        remaining = self.cooldowns.get(key, 0) - time.monotonic()
        if remaining > 0:
            raise DiscordToolError(f'Discord {operation} 请求冷却中，请 {math.ceil(remaining)} 秒后再试；不要立即重复调用。')
        return key

    async def _request(self, operation, factory):
        key = self._request_ready(operation)
        try:
            return await asyncio.wait_for(factory(), timeout=DISCORD_REQUEST_TIMEOUT)
        except (discord.HTTPException, discord.RateLimited, TimeoutError, OSError) as exc:
            retry_after = max(30, float(getattr(exc, 'retry_after', 0)))
            self.cooldowns[key] = time.monotonic() + retry_after
            logger.warning('Cosmetic Discord request failed: guild=%s operation=%s error=%s status=%s code=%s retry_after=%.1f',
                           key[0], operation, type(exc).__name__, getattr(exc, 'status', None),
                           getattr(exc, 'code', None), retry_after)
            detail = ('限流' if isinstance(exc, discord.RateLimited) or getattr(exc, 'status', None) == 429
                      else '等待响应或限流队列超时' if isinstance(exc, TimeoutError)
                      else f'请求失败（{type(exc).__name__}，HTTP {getattr(exc, "status", "未知")}）')
            raise DiscordToolError(f'Discord {operation} {detail}，已停止操作；请 {math.ceil(retry_after)} 秒后重试，不要立即重复调用。这不等于边界或成员权限不合格。') from exc

    async def _channels(self, guild):
        cached = self._gateway_channels(guild)
        return cached if cached is not None else await self._request('channels', guild.fetch_channels)

    async def snapshot(self) -> CosmeticContext:
        guild = self.host._guild()
        roles = await self._request('roles', guild.fetch_roles)
        upper, lower = boundaries(roles)
        tasks = [asyncio.create_task(call) for call in (
            self._request('requester', lambda: guild.fetch_member(self.host.message.author.id)),
            self._request('bot_member', lambda: guild.fetch_member(self.bot.user.id)), self._channels(guild),
        )]
        try:
            requester, bot_member, channels = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if requester.id != self.host.message.author.id or bot_member.id != self.bot.user.id:
            raise DiscordToolError('幻化成员身份核验失败。')
        requester_permissions, _ = self._member_permissions(requester, roles, guild)
        bot_permissions, bot_top = self._member_permissions(bot_member, roles, guild)
        config = self.store.settings(guild.id)
        if config is None:
            privileged = [role for role in roles if role.name.strip() == PRIVILEGE_NAME]
            if len(privileged) != 1 or lower < privileged[0] < upper or privileged[0].is_default():
                privileged = []
            config = dict(normal_limit=DEFAULT_NORMAL_LIMIT, privileged_limit=DEFAULT_PRIVILEGED_LIMIT,
                          area_limit=DEFAULT_AREA_LIMIT, privileged_role_ids=[str(privileged[0].id)] if len(privileged) == 1 else [])
        return CosmeticContext(guild, {role.id: role for role in roles}, list(channels), upper, lower,
                               requester, requester_permissions, bot_top, bot_permissions, config, bot_member)

    def _after_placement(self, ctx, roles):
        upper, lower = boundaries(roles)
        if (upper.id, lower.id) != (ctx.upper.id, ctx.lower.id):
            raise DiscordToolError('放置期间幻化边界发生变化，保留未完成记录，未佩戴。')
        permissions, top = self._member_permissions(ctx.bot_member, roles, ctx.guild)
        cached = self._gateway_channels(ctx.guild)
        return replace(ctx, roles={role.id: role for role in roles}, upper=upper, lower=lower,
                       bot_top=top, bot_permissions=permissions, channels=cached if cached is not None else ctx.channels)

    def _manager(self, ctx) -> bool:
        return self.host._is_owner() or ctx.requester.id == ctx.guild.owner_id or ctx.requester_permissions.administrator

    def _require_manager(self, ctx):
        if not self._manager(ctx):
            raise DiscordToolError('幻化配置和旧身份组归属登记仅限开发者、当前服务器服主或管理员。')

    def _require_bot(self, ctx):
        if not ctx.bot_permissions.manage_roles or ctx.bot_top is None or not ctx.bot_top > ctx.upper:
            raise DiscordToolError('BOT 需要「管理身份组」权限，且 BOT 的最高身份组必须高于幻化区开始。')

    def _privileged_ids(self, ctx) -> set[int]:
        # A freely wearable cosmetic role must never become a quota entitlement.
        return {int(raw) for raw in ctx.settings['privileged_role_ids'] if int(raw) in ctx.roles
                and not ctx.inside(ctx.roles[int(raw)])
                and int(raw) not in {ctx.upper.id, ctx.lower.id, ctx.guild.id}}

    def _quota(self, ctx) -> int:
        privileged = bool(self._privileged_ids(ctx) & {int(value) for value in ctx.requester._roles})
        return ctx.settings['privileged_limit'] if privileged else ctx.settings['normal_limit']

    def _check_role(self, ctx, role):
        if not ctx.inside(role):
            raise DiscordToolError('只能操作开始与结束身份组之间的幻化身份组，边界本身也不能操作。')
        self._check_safe_role(ctx, role)

    def _check_safe_role(self, ctx, role):
        if role.managed or role.is_default() or role.permissions.value:
            raise DiscordToolError('该身份组带有权限或属于系统管理身份组，不能作为自由幻化身份组操作。')
        if role.id in {int(value) for value in ctx.settings['privileged_role_ids']}:
            raise DiscordToolError('特权额度身份组不能通过幻化工具领取或修改。')
        for channel in ctx.channels:
            for target, overwrite in channel.overwrites.items():
                if target.id == role.id and not overwrite.is_empty():
                    raise DiscordToolError('该身份组参与频道权限控制，幻化工具拒绝操作。')
        self._require_bot(ctx)

    def _resolve_role(self, ctx, raw):
        if not isinstance(raw, str) or not raw.strip():
            raise DiscordToolError('请提供明确的 role_ref（身份组引用、提及、ID 或完整名称）。')
        text = raw.strip()
        if text.startswith('<@&') and text.endswith('>'):
            text = text[3:-1]
        if text.startswith('ref:') or text.isdecimal():
            role = ctx.roles.get(self.host._coerce_id(text, 'role_ref'))
            if role is None:
                raise DiscordToolError('当前服务器没有该身份组。')
            return role
        matches = [role for role in ctx.roles.values() if role.name.casefold() == text.casefold()]
        if len(matches) != 1:
            raise DiscordToolError('身份组名称不明确或重名，请先列出幻化身份组并使用返回的 roleRef。')
        return matches[0]

    def _record(self, ctx, role, *, owner_only=False):
        record = self.store.records(ctx.guild.id).get(role.id)
        if record is None or record['state'] != 'active':
            raise DiscordToolError('该身份组尚未登记幻化归属；需要管理员明确登记，不能自行认领。')
        if owner_only and record['creator_id'] != str(ctx.requester.id):
            raise DiscordToolError('只能修改或删除自己创建／获登记归属的幻化身份组。')
        return record

    def _payload(self, ctx, role, record=None):
        result = self.host._role_payload(role)
        result.update(creatorId=record['creator_id'] if record else None,
                      public=bool(record['public']) if record else True,
                      legacy=record is None,
                      registered=bool(record and record['state'] == 'active'))
        result['state'] = record['state'] if record else 'legacy'
        try:
            self._check_role(ctx, role)
            result['blockedReason'] = None
        except DiscordToolError as exc:
            result['blockedReason'] = str(exc)
        owned = bool(record and record['creator_id'] == str(ctx.requester.id))
        active = record is None or record['state'] == 'active'
        result['canEquipSelf'] = active and not result['blockedReason'] and (result['public'] or owned)
        result['canEditOwn'] = bool(owned and active and not result['blockedReason'])
        return result

    def _validate_name(self, ctx, name, *, exclude_id=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise DiscordToolError('身份组名称需要 1–100 个字符，且不能含控制字符。')
        name = name.strip()
        if normalized_name(name) in {START_NAME, END_NAME, PRIVILEGE_NAME, '@everyone', '@here'}:
            raise DiscordToolError('不能使用幻化边界、特权身份组或保留名称。')
        if any(role.id != exclude_id and role.name.casefold() == name.casefold() for role in ctx.roles.values()):
            raise DiscordToolError('服务器已有同名身份组，请选择不同名称。')
        return name

    async def _style(self, ctx, args, *, creating=False, role=None):
        kwargs = self.host._role_colour_kwargs(args, creating=creating, role=role)
        icon, _source = await self.host._role_icon_kwargs(args, allow_clear=not creating)
        kwargs.update(icon)
        if creating or 'name' in args:
            kwargs['name'] = self._validate_name(ctx, args.get('name'), exclude_id=role.id if role else None)
        return kwargs

    async def execute(self, action: str, args: dict) -> dict:
        if action not in _ACTION_FIELDS:
            raise DiscordToolError('未知的幻化操作。')
        if not isinstance(args, dict) or set(args) - _ACTION_FIELDS[action]:
            raise DiscordToolError('幻化工具不接受权限、身份组位置、其他成员穿戴目标或其他未声明参数。')
        for field in ('public', 'equip', 'clear_role_icon'):
            if field in args and not isinstance(args[field], bool):
                raise DiscordToolError(f'{field} 必须为布尔值。')
        if 'user_ref' in args and args.get('source_type') != 'avatar':
            raise DiscordToolError('user_ref 仅能用于头像图标来源；幻化佩戴对象固定为请求者本人。')
        if action == 'delete':
            # Do not hold the guild lock while waiting for a human reaction.
            async with self.lock:
                ctx = await self.snapshot()
                role = self._resolve_role(ctx, args.get('role_ref'))
                self._check_role(ctx, role)
                self._record(ctx, role, owner_only=True)
                role_id = role.id
            await self.host._confirm_destructive_action('cosmetic_delete', {'role_id': str(role_id)})
            args = {'role_ref': str(role_id)}
        async with self.lock:
            ctx = await self.snapshot()
            if action in {'status', 'list', 'mine'}:
                records = self.store.records(ctx.guild.id)
                roles = [role for role in reversed(sorted(ctx.roles.values())) if ctx.inside(role)]
                if action == 'mine':
                    roles = [role for role in reversed(sorted(ctx.roles.values()))
                             if records.get(role.id, {}).get('creator_id') == str(ctx.requester.id)]
                data = {
                    'enabled': True, 'guildId': str(ctx.guild.id),
                    'startRoleId': str(ctx.upper.id), 'endRoleId': str(ctx.lower.id),
                    'myQuota': self._quota(ctx), 'myCreatedCount': sum(1 for rid, record in records.items() if rid in ctx.roles and record['creator_id'] == str(ctx.requester.id)),
                    'normalLimit': ctx.settings['normal_limit'], 'privilegedLimit': ctx.settings['privileged_limit'],
                    'areaLimit': ctx.settings['area_limit'], 'areaCount': sum(ctx.inside(role) for role in ctx.roles.values()),
                    'privilegedRoleIds': [str(value) for value in self._privileged_ids(ctx)],
                    'canConfigure': self._manager(ctx),
                    'myPendingRoleIds': [str(rid) for rid, record in records.items() if rid in ctx.roles
                                         and record['creator_id'] == str(ctx.requester.id) and record['state'] == 'pending'],
                }
                if action != 'status':
                    query = args.get('query', '')
                    if not isinstance(query, str) or len(query) > 100:
                        raise DiscordToolError('query 必须为不超过 100 个字符的名称检索文本。')
                    if query.strip():
                        roles = [role for role in roles if query.strip().casefold() in role.name.casefold()]
                    offset = self.host._bounded_int(args.get('offset'), default=0, minimum=0, maximum=1000)
                    limit = self.host._bounded_int(args.get('limit'), default=12, maximum=25)
                    data.update(roles=[], totalCount=len(roles), offset=offset, nextOffset=None, hasMore=False)
                    for role in roles[offset:offset + limit]:
                        data['roles'].append(self._payload(ctx, role, records.get(role.id)))
                        # Keep valid JSON below the shared host result cap.
                        if len(json.dumps(data, ensure_ascii=False, separators=(',', ':'))) > 11500:
                            data['roles'].pop()
                            break
                    next_offset = offset + len(data['roles'])
                    data['hasMore'] = next_offset < len(roles)
                    data['nextOffset'] = next_offset if data['hasMore'] else None
                return self.host._result('幻化区状态：区内无权限、无频道授权的旧身份组无需登记，所有人可给自己佩戴／取下；修改和删除仍只限已登记的归属者。列表可用 query 检索名称或用 nextOffset 翻页。', data)
            self._require_bot(ctx)
            if action == 'configure':
                return self._configure(ctx, args)
            if action == 'create':
                return await self._create(ctx, args)
            if action == 'resume':
                return await self._resume(ctx, self._resolve_role(ctx, args.get('role_ref')), args)
            role = self._resolve_role(ctx, args.get('role_ref'))
            self._check_role(ctx, role)
            if action == 'adopt':
                self._require_manager(ctx)
                owner = await self.host._resolve_member_ref(args.get('owner_ref'))
                owner = await asyncio.wait_for(ctx.guild.fetch_member(owner.id), timeout=12)
                records = self.store.records(ctx.guild.id)
                if role.id in records:
                    raise DiscordToolError('身份组已登记，不能覆盖现有归属。')
                # Member lookup may await REST; validate the region again.
                ctx = await self.snapshot()
                self._require_manager(ctx)
                role = ctx.roles.get(role.id)
                if role is None:
                    raise DiscordToolError('身份组已被删除。')
                self._check_role(ctx, role)
                self.store.put(ctx.guild.id, role.id, owner.id, bool(args.get('public', False)))
                return self.host._result('已登记现有幻化身份组归属；未改变原有权限或佩戴成员。', self._payload(ctx, role, self.store.records(ctx.guild.id)[role.id]))
            # Unregistered legacy roles are public for self-wearing only.
            # A pending/failed creation is NOT a legacy role or an ownership bypass.
            record = self.store.records(ctx.guild.id).get(role.id)
            if record is None and action in {'equip', 'unequip'}:
                record = {'public': True, 'creator_id': None}
            else:
                record = self._record(ctx, role, owner_only=action in {'edit', 'delete'})
            reason = f'ATRI cosmetic {action}; requester={ctx.requester.id}'
            if action == 'edit':
                kwargs = await self._style(ctx, args, role=role)
                ctx = await self.snapshot()
                role = ctx.roles.get(role.id)
                if role is None:
                    raise DiscordToolError('身份组已被删除。')
                self._check_role(ctx, role)
                record = self._record(ctx, role, owner_only=True)
                if 'name' in kwargs:
                    self._validate_name(ctx, kwargs['name'], exclude_id=role.id)
                if not kwargs and 'public' not in args:
                    raise DiscordToolError('请提供要修改的名称、颜色、图标或公开状态。')
                if kwargs:
                    role = await role.edit(reason=reason, **kwargs)
                public = args.get('public', bool(record['public']))
                self.store.update(ctx.guild.id, role.id, public=public)
                return self.host._result('已更新你的幻化身份组。公开状态只影响后续领取，不会自动移除已佩戴成员。', self._payload(ctx, role, self.store.records(ctx.guild.id)[role.id]))
            if action == 'delete':
                await role.delete(reason=reason)
                self.store.remove(ctx.guild.id, role.id)
                return self.host._result('已删除你的幻化身份组，个人名额已释放。', {'roleId': str(role.id)})
            if action == 'equip':
                if not record['public'] and record['creator_id'] != str(ctx.requester.id):
                    raise DiscordToolError('该幻化身份组未公开，只能由创建者自己佩戴。')
                await ctx.requester.add_roles(role, reason=reason, atomic=True)
            elif action == 'unequip':
                await ctx.requester.remove_roles(role, reason=reason, atomic=True)
            return self.host._result('已为你佩戴幻化身份组。' if action == 'equip' else '已取下你的幻化身份组。', {'roleId': str(role.id), 'userId': str(ctx.requester.id)})

    def _configure(self, ctx, args):
        self._require_manager(ctx)
        config = dict(ctx.settings)
        for key, maximum in (('normal_limit', 20), ('privileged_limit', 50), ('area_limit', 200)):
            if key in args:
                value = args[key]
                if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
                    raise DiscordToolError(f'{key} 需要 1–{maximum} 的整数。')
                config[key] = value
        if 'privileged_role_ids' in args:
            raw = args['privileged_role_ids']
            if not isinstance(raw, list) or len(raw) > 10:
                raise DiscordToolError('privileged_role_ids 必须为不超过 10 个身份组 ID 的数组。')
            ids = {self.host._coerce_id(value, 'role_id') for value in raw}
            if any(value not in ctx.roles or ctx.inside(ctx.roles[value]) or value in {ctx.upper.id, ctx.lower.id, ctx.guild.id} for value in ids):
                raise DiscordToolError('特权额度身份组必须属于当前服务器，且位于幻化区之外；不能是边界或 everyone。')
            config['privileged_role_ids'] = [str(value) for value in sorted(ids)]
        self.store.save_settings(ctx.guild.id, config)
        return self.host._result('已更新当前服务器的幻化设置；不会修改或删除已有身份组。', config)

    async def _create(self, ctx, args):
        key = hashlib.sha256(json.dumps([str(ctx.requester.id), str(self.host.message.id), args], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        records = self.store.records(ctx.guild.id)
        for role_id, record in records.items():
            if record['request_key'] == key and role_id in ctx.roles:
                role = ctx.roles[role_id]
                if record['state'] == 'pending':
                    return await self._resume(ctx, role, {'equip': args.get('equip', True)})
                self._check_role(ctx, role)
                self._record(ctx, role, owner_only=True)
                payload = self._payload(ctx, role, record)
                payload['equipped'] = role.id in ctx.requester._roles
                return self.host._result('此创建请求已完成，没有重复创建；若尚未佩戴，请对返回的身份组执行 equip。', payload)
        owned = [record for role_id, record in records.items() if role_id in ctx.roles and record['creator_id'] == str(ctx.requester.id)]
        pending = [record for record in owned if record['state'] == 'pending']
        if pending:
            name = str(args.get('name', '')).strip().casefold()
            matches = [record for record in pending if ctx.roles[int(record['role_id'])].name.casefold() == name]
            if len(matches) == 1:
                return await self._resume(ctx, ctx.roles[int(matches[0]['role_id'])], {'equip': args.get('equip', True)})
            ids = ', '.join(record['role_id'] for record in pending)
            raise DiscordToolError(f'你还有未完成的幻化身份组（{ids}）；请先用 mine 查看，再用 resume 和准确的 role_ref 继续放置，不要再次创建。')
        if len(owned) >= self._quota(ctx):
            raise DiscordToolError(f'你在当前服务器的幻化名额已满（{self._quota(ctx)} 个）。')
        if sum(ctx.inside(role) for role in ctx.roles.values()) >= ctx.settings['area_limit']:
            raise DiscordToolError('幻化区总量已达到配置上限，请联系管理员。')
        if owned and time.time() - max(record['created_at'] for record in owned) < 30:
            raise DiscordToolError('创建幻化身份组有 30 秒冷却，请稍后再创建下一组。')
        kwargs = await self._style(ctx, args, creating=True)
        ctx = await self.snapshot()
        self._require_bot(ctx)
        self._validate_name(ctx, kwargs['name'])
        if len(owned) >= self._quota(ctx) or sum(ctx.inside(role) for role in ctx.roles.values()) >= ctx.settings['area_limit']:
            raise DiscordToolError('额度或幻化区容量已改变，已停止创建。')
        public = bool(args.get('public', False))
        self._request_ready('place')  # Never create another role while placement is backing off.
        # POST /roles cannot set position. Creation at position 1 shifts the
        # lower marker up by one. PATCH follows immediately, with NO HTTP reads
        # in between. Its returned role list is the authoritative position check.
        destination = ctx.lower.position + 1
        created = await self._request('create', lambda: ctx.guild.create_role(
            permissions=discord.Permissions.none(), hoist=False, mentionable=False,
            reason=f'ATRI cosmetic create; owner={ctx.requester.id}; request={self.host.message.id}', **kwargs))
        self.store.put(ctx.guild.id, created.id, ctx.requester.id, public, state='pending', request_key=key,
                       start_role_id=ctx.upper.id, end_role_id=ctx.lower.id, expected_name=kwargs['name'])
        return await self._finish_pending(ctx, created, destination=destination, equip=args.get('equip', True))

    async def _resume(self, ctx, role, args):
        record = self.store.records(ctx.guild.id).get(role.id)
        if record is None:
            raise DiscordToolError('resume 只接受 BOT 留下的未完成创建记录，不能移动任意身份组。')
        owner = record['creator_id'] == str(ctx.requester.id)
        if not owner and not self._manager(ctx):
            raise DiscordToolError('只能继续自己未完成的幻化身份组。')
        equip = args.get('equip', True)
        if equip and not owner:
            raise DiscordToolError('管理员代为修复放置时必须指定 equip=false；不会替其他成员佩戴。')
        if record['state'] == 'active':
            self._check_role(ctx, role)
            return self.host._result('该身份组已完成放置；无需再次恢复，可使用 equip 自助佩戴。', self._payload(ctx, role, record))
        if record['state'] != 'pending':
            raise DiscordToolError('该创建记录不可恢复。')
        for field, current in (('start_role_id', ctx.upper.id), ('end_role_id', ctx.lower.id)):
            if record.get(field) and record[field] != str(current):
                raise DiscordToolError('原幻化边界已被替换，不能自动移动未完成身份组。')
        if record.get('expected_name') and role.name != record['expected_name']:
            raise DiscordToolError('未完成身份组已被外部改名，不能自动恢复；请管理员核对。')
        self._check_safe_role(ctx, role)
        if not ctx.inside(role) and not role < ctx.lower:
            raise DiscordToolError('未完成身份组被移到了上边界之外，不能自动恢复。')
        if not ctx.inside(role) and sum(ctx.inside(item) for item in ctx.roles.values()) >= ctx.settings['area_limit']:
            raise DiscordToolError('幻化区已满，暂不能恢复放置；没有重复创建。')
        return await self._finish_pending(ctx, role, destination=None if ctx.inside(role) else ctx.lower.position, equip=equip)

    async def _finish_pending(self, ctx, role, *, destination, equip):
        role_id = role.id
        record = self.store.records(ctx.guild.id)[role_id]
        try:
            # Observe gateway changes without another rate-limited GET.
            cached = self._gateway_channels(ctx.guild)
            if cached is not None:
                upper, lower = boundaries(ctx.guild.roles)
                if (upper.id, lower.id) != (ctx.upper.id, ctx.lower.id):
                    raise DiscordToolError('创建期间幻化边界发生变化，未佩戴。')
                ctx = replace(ctx, channels=cached)
                self._check_safe_role(ctx, role)
            if destination is not None:
                roles = await self._request('place', lambda: ctx.guild.edit_role_positions(
                    positions={role: destination}, reason=f'ATRI place pending cosmetic; role={role_id}; creator={record["creator_id"]}'))
                ctx = self._after_placement(ctx, roles)
            role = ctx.roles.get(role_id)
            if role is None:
                raise DiscordToolError('放置响应中没有该身份组，未佩戴。')
            self._check_role(ctx, role)
            if record.get('expected_name') and role.name != record['expected_name']:
                raise DiscordToolError('新身份组已被外部改名，保留未完成记录供核对。')
            self.store.update(ctx.guild.id, role.id, public=bool(record['public']))
        except BaseException as exc:
            self.store.failure(ctx.guild.id, role_id, str(exc) or type(exc).__name__)
            logger.warning('Cosmetic placement pending: guild=%s role=%s error=%s; resume existing role, do not recreate',
                           ctx.guild.id, role_id, type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DiscordToolError(f'身份组已创建，但放置／验证未完成（role_ref="{role_id}"），未自动佩戴；'
                                   f'稍后使用 resume 继续处理同一身份组，不要重复创建。原因：{exc}') from exc
        equipped = False
        equip_error = None
        if equip:
            try:
                await self._request('equip', lambda: ctx.requester.add_roles(role, reason='ATRI equip newly created personal cosmetic', atomic=True))
                equipped = True
            except DiscordToolError:
                equip_error = '身份组创建成功，但自动佩戴失败；可稍后对该身份组执行 equip，不要重复创建。'
        payload = self._payload(ctx, role, self.store.records(ctx.guild.id)[role.id])
        payload.update(equipped=equipped, equipError=equip_error)
        return self.host._result('幻化身份组已创建并放入指定边界之间。', payload)
