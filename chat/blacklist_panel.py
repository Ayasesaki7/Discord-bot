from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import discord

if TYPE_CHECKING:
    from .cog import AtriChat

BLACKLIST_ENV_KEY = 'ATRI_CHAT_BLACKLIST_IDS'
BLACKLIST_PAGE_SIZE = 25
BLACKLIST_PANEL_TIMEOUT_SECONDS = 900
BLACKLIST_ID_PATTERN = re.compile(r'\d+')


def _compact_text(text: str, limit: int = 400) -> str:
    compact = ' '.join(text.split())
    if len(compact) > limit:
        compact = compact[: limit - 3] + '...'
    return compact


def _env_file_path() -> Path:
    return Path(__file__).resolve().parent.parent / '.env'


def _read_env_text(path: Path) -> tuple[str, str]:
    if not path.exists():
        return '', os.linesep

    raw = path.read_text(encoding='utf-8-sig')
    newline = '\r\n' if '\r\n' in raw else '\n'
    return raw, newline


def _write_env_updates(updates: dict[str, str]) -> None:
    path = _env_file_path()
    raw, newline = _read_env_text(path)
    lines = raw.splitlines()
    updated_lines: list[str] = []
    seen_keys: set[str] = set()

    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith('#') or '=' not in line:
            updated_lines.append(line)
            continue

        key, _separator, _value = line.partition('=')
        env_key = key.strip()
        if env_key in updates:
            updated_lines.append(f'{env_key}={updates[env_key]}')
            seen_keys.add(env_key)
            continue

        updated_lines.append(line)

    for env_key, env_value in updates.items():
        if env_key in seen_keys:
            continue
        updated_lines.append(f'{env_key}={env_value}')

    content = newline.join(updated_lines)
    if updated_lines:
        content += newline
    path.write_text(content, encoding='utf-8')


def read_blacklist_ids_from_env() -> set[int]:
    raw = os.getenv(BLACKLIST_ENV_KEY, '').strip()
    if not raw:
        return set()

    user_ids: set[int] = set()
    for token in BLACKLIST_ID_PATTERN.findall(raw):
        try:
            user_id = int(token)
        except ValueError:
            continue
        if user_id > 0:
            user_ids.add(user_id)
    return user_ids


def persist_blacklist_ids(cog: 'AtriChat', user_ids: set[int]) -> None:
    normalized_ids = sorted({int(user_id) for user_id in user_ids if int(user_id) > 0})
    serialized = ','.join(str(user_id) for user_id in normalized_ids)

    cog.blacklisted_user_ids = set(normalized_ids)
    os.environ[BLACKLIST_ENV_KEY] = serialized
    _write_env_updates({BLACKLIST_ENV_KEY: serialized})
    print(
        '[INFO] Chat blacklist updated: '
        f'count={len(normalized_ids)}, ids={serialized or "(empty)"}'
    )


def parse_blacklist_ids(raw: str) -> set[int]:
    parsed: set[int] = set()
    for token in BLACKLIST_ID_PATTERN.findall(raw):
        try:
            user_id = int(token)
        except ValueError:
            continue
        if user_id > 0:
            parsed.add(user_id)
    return parsed


class BlacklistManualIdModal(discord.ui.Modal):
    def __init__(
        self,
        panel: 'ChatBlacklistView',
        *,
        action: Literal['add', 'remove'],
    ):
        self.panel = panel
        self.action = action
        title = '手动添加黑名单 ID' if action == 'add' else '手动移除黑名单 ID'
        super().__init__(title=title, timeout=300)
        self.user_ids_input = discord.ui.TextInput(
            label='Discord ID 或 @提及',
            style=discord.TextStyle.paragraph,
            placeholder='支持多个，按空格、换行、逗号分隔，也可以直接粘贴 @用户',
            required=True,
            max_length=1000,
        )
        self.add_item(self.user_ids_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.cog.owner_user_id:
            await interaction.response.send_message(
                '这个面板只允许开发者使用。',
                ephemeral=True,
            )
            return

        user_ids = parse_blacklist_ids(self.user_ids_input.value)
        if not user_ids:
            await interaction.response.send_message(
                '没有识别到有效的 Discord ID。',
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        if self.action == 'add':
            added_count = self.panel.apply_added_ids(user_ids)
            skipped_count = len(user_ids) - added_count
            await self.panel.prime_user_profiles(list(user_ids))
            if skipped_count > 0:
                self.panel.status_message = (
                    f'手动添加完成：新增 {added_count} 个，{skipped_count} 个原本已在黑名单里。'
                )
            else:
                self.panel.status_message = f'已手动加入 {added_count} 个黑名单 ID。'
        else:
            removed_count = self.panel.apply_removed_ids(user_ids)
            missing_count = len(user_ids) - removed_count
            if missing_count > 0:
                self.panel.status_message = (
                    f'手动移除完成：移出 {removed_count} 个，{missing_count} 个原本不在黑名单里。'
                )
            else:
                self.panel.status_message = f'已手动移出 {removed_count} 个黑名单 ID。'

        self.panel.error_message = None
        await self.panel.refresh_panel_message()


class BlacklistUserAddSelect(discord.ui.UserSelect):
    def __init__(self, panel: 'ChatBlacklistView'):
        self.panel = panel
        disabled = panel.guild is None
        placeholder = (
            '当前不在服务器上下文，请改用手动添加 ID'
            if disabled
            else '从当前服务器选择要加入黑名单的成员'
        )
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=25,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        for item in self.values:
            self.panel.remember_user(item)

        selected_ids = {item.id for item in self.values}
        if not selected_ids:
            await interaction.response.defer()
            return

        added_count = self.panel.apply_added_ids(selected_ids)
        skipped_count = len(selected_ids) - added_count
        self.panel.error_message = None
        if skipped_count > 0:
            self.panel.status_message = (
                f'新增 {added_count} 个黑名单成员，{skipped_count} 个本来就在名单里。'
            )
        else:
            self.panel.status_message = f'已新增 {added_count} 个黑名单成员。'

        self.panel.bind_message(interaction.message)
        await interaction.response.edit_message(
            embed=self.panel.build_embed(),
            view=self.panel.rebuild_for_response(),
        )


class BlacklistRemoveSelect(discord.ui.Select):
    def __init__(self, panel: 'ChatBlacklistView'):
        self.panel = panel
        options = panel.current_page_options
        placeholder = '勾选要移出黑名单的成员' if options else '当前黑名单为空'

        if not options:
            options = [
                discord.SelectOption(
                    label='当前黑名单为空',
                    value='__empty__',
                )
            ]

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=min(len(options), BLACKLIST_PAGE_SIZE),
            options=options,
            disabled=not panel.current_page_ids,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_ids = {int(value) for value in self.values if value.isdigit()}
        if not selected_ids:
            await interaction.response.defer()
            return

        removed_count = self.panel.apply_removed_ids(selected_ids)
        self.panel.error_message = None
        self.panel.status_message = f'已移出 {removed_count} 个黑名单成员。'
        self.panel.bind_message(interaction.message)
        await interaction.response.edit_message(
            embed=self.panel.build_embed(),
            view=self.panel.rebuild_for_response(),
        )


class ChatBlacklistView(discord.ui.View):
    def __init__(self, cog: 'AtriChat', guild: discord.Guild | None):
        super().__init__(timeout=BLACKLIST_PANEL_TIMEOUT_SECONDS)
        self.cog = cog
        self.guild = guild
        self.page = 0
        self.status_message = '黑名单只拦截聊天提及，不影响音乐和其他功能。'
        self.error_message: str | None = None
        self.panel_message: discord.InteractionMessage | None = None
        self.closed = False
        self.user_label_cache: dict[int, str] = dict(getattr(cog, 'blacklist_user_labels', {}))
        self.user_avatar_cache: dict[int, str] = dict(getattr(cog, 'blacklist_user_avatars', {}))
        self._rebuild_items()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.cog.owner_user_id:
            return True

        await interaction.response.send_message(
            '这个面板只允许开发者使用。',
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        if self.panel_message is None or self.closed:
            return

        self.closed = True
        try:
            await self.panel_message.edit(embed=self.build_embed(), view=None)
        except discord.HTTPException:
            pass

    @property
    def sorted_blacklist_ids(self) -> list[int]:
        return sorted(self.cog.blacklisted_user_ids)

    @property
    def total_pages(self) -> int:
        if not self.sorted_blacklist_ids:
            return 1
        return ((len(self.sorted_blacklist_ids) - 1) // BLACKLIST_PAGE_SIZE) + 1

    @property
    def current_page_ids(self) -> list[int]:
        self.page = max(0, min(self.page, self.total_pages - 1))
        start = self.page * BLACKLIST_PAGE_SIZE
        end = start + BLACKLIST_PAGE_SIZE
        return self.sorted_blacklist_ids[start:end]

    @property
    def current_page_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for user_id in self.current_page_ids:
            label = self.format_user_label(user_id, include_id=False)
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(user_id),
                    description=f'ID: {user_id}'[:100],
                )
            )
        return options

    def bind_message(self, message: discord.InteractionMessage | None) -> None:
        if message is not None:
            self.panel_message = message

    def _build_base_label(self, user: discord.abc.User) -> str:
        display_name = getattr(user, 'display_name', user.name)
        if user.name != display_name:
            return _compact_text(f'{display_name} ({user.name})', 100)
        return _compact_text(display_name, 100)

    def remember_user(self, user: discord.abc.User) -> None:
        label = self._build_base_label(user)
        avatar_url = str(user.display_avatar.url) if user.display_avatar else ''
        self.user_label_cache[user.id] = label
        self.user_avatar_cache[user.id] = avatar_url
        self.cog.blacklist_user_labels[user.id] = label
        self.cog.blacklist_user_avatars[user.id] = avatar_url

    async def prime_user_profiles(self, user_ids: list[int] | None = None) -> None:
        target_ids = user_ids or self.current_page_ids
        for user_id in target_ids:
            if user_id in self.user_label_cache and user_id in self.user_avatar_cache:
                continue

            member = self.guild.get_member(user_id) if self.guild is not None else None
            if member is not None:
                self.remember_user(member)
                continue

            cached_user = self.cog.bot.get_user(user_id)
            if cached_user is not None:
                self.remember_user(cached_user)
                continue

            try:
                fetched_user = await self.cog.bot.fetch_user(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
            self.remember_user(fetched_user)

    def format_user_label(self, user_id: int, *, include_id: bool = True) -> str:
        base = self.user_label_cache.get(user_id)
        if base is None:
            member = self.guild.get_member(user_id) if self.guild is not None else None
            if member is not None:
                self.remember_user(member)
                base = self.user_label_cache.get(user_id)
            else:
                user = self.cog.bot.get_user(user_id)
                if user is not None:
                    self.remember_user(user)
                    base = self.user_label_cache.get(user_id)

        if base is None:
            base = '未知用户'

        if include_id:
            base = f'{base} [{user_id}]'
        return _compact_text(base, 100)

    def get_avatar_preview(self) -> tuple[str | None, str | None]:
        for user_id in self.current_page_ids:
            avatar_url = self.user_avatar_cache.get(user_id)
            if avatar_url:
                return avatar_url, self.format_user_label(user_id, include_id=False)
        return None, None

    def build_current_page_summary(self) -> str:
        current_ids = self.current_page_ids
        if not current_ids:
            return '当前黑名单为空。'

        preview_ids = current_ids[:10]
        lines = [
            f'{index}. {self.format_user_label(user_id)}'
            for index, user_id in enumerate(
                preview_ids,
                start=self.page * BLACKLIST_PAGE_SIZE + 1,
            )
        ]
        remaining = len(current_ids) - len(preview_ids)
        if remaining > 0:
            lines.append(f'... 当前页还有 {remaining} 项，可通过下拉框勾选移除。')
        return '\n'.join(lines)

    def build_embed(self) -> discord.Embed:
        total_count = len(self.sorted_blacklist_ids)
        color = discord.Color.red() if total_count > 0 else discord.Color.green()
        if self.error_message:
            color = discord.Color.orange()

        description = (
            '仅开发者可用。被拉黑用户依然能使用其他功能，'
            '只是聊天 @bot 时不会得到回应，也不会向上游发请求。'
        )
        if self.closed:
            description = '这个黑名单面板已经关闭。重新执行命令可以再打开。'

        embed = discord.Embed(
            title='聊天黑名单管理面板',
            description=description,
            color=color,
        )
        embed.add_field(
            name='黑名单概况',
            value=(
                f'总人数: `{total_count}`\n'
                f'当前页: `{self.page + 1}/{self.total_pages}`\n'
                f'作用范围: `只拦截聊天提及`'
            ),
            inline=False,
        )
        embed.add_field(
            name='当前页名单',
            value=self.build_current_page_summary(),
            inline=False,
        )
        embed.add_field(
            name='操作说明',
            value=(
                '1. 上方成员选择器用于把当前服务器成员加入黑名单。\n'
                '2. 下方多选框用于勾选并移出黑名单。\n'
                '3. 不在当前服务器里的用户，可以用“手动添加 ID”或“手动移除 ID”。'
            ),
            inline=False,
        )
        avatar_url, avatar_label = self.get_avatar_preview()
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
            embed.add_field(
                name='头像预览',
                value=f'当前显示的是 `{avatar_label}` 的头像。',
                inline=False,
            )
        status_lines = [f'最近操作: {self.status_message}']
        if self.error_message:
            status_lines.append(f'最近错误: {self.error_message}')
        embed.add_field(name='状态', value='\n'.join(status_lines), inline=False)
        return embed

    def _rebuild_items(self) -> None:
        self.clear_items()
        self.add_item(BlacklistUserAddSelect(self))
        self.add_item(BlacklistRemoveSelect(self))

        previous_button = discord.ui.Button(
            label='上一页',
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
            row=2,
        )
        previous_button.callback = self.previous_page
        self.add_item(previous_button)

        next_button = discord.ui.Button(
            label='下一页',
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages - 1,
            row=2,
        )
        next_button.callback = self.next_page
        self.add_item(next_button)

        manual_add_button = discord.ui.Button(
            label='手动添加 ID',
            style=discord.ButtonStyle.primary,
            row=3,
        )
        manual_add_button.callback = self.open_manual_add_modal
        self.add_item(manual_add_button)

        manual_remove_button = discord.ui.Button(
            label='手动移除 ID',
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        manual_remove_button.callback = self.open_manual_remove_modal
        self.add_item(manual_remove_button)

        close_button = discord.ui.Button(
            label='关闭面板',
            style=discord.ButtonStyle.danger,
            row=3,
        )
        close_button.callback = self.close_panel
        self.add_item(close_button)

    def rebuild_for_response(self) -> 'ChatBlacklistView':
        self._rebuild_items()
        return self

    def apply_added_ids(self, user_ids: set[int]) -> int:
        before = set(self.cog.blacklisted_user_ids)
        updated = before | set(user_ids)
        persist_blacklist_ids(self.cog, updated)
        self.page = min(self.page, self.total_pages - 1)
        return len(updated - before)

    def apply_removed_ids(self, user_ids: set[int]) -> int:
        before = set(self.cog.blacklisted_user_ids)
        updated = before - set(user_ids)
        persist_blacklist_ids(self.cog, updated)
        self.page = min(self.page, self.total_pages - 1)
        return len(before - updated)

    async def refresh_panel_message(self) -> None:
        if self.panel_message is None:
            return

        await self.prime_user_profiles()
        self._rebuild_items()
        await self.panel_message.edit(
            embed=self.build_embed(),
            view=None if self.closed else self,
        )

    async def previous_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self.error_message = None
        self.status_message = f'已切换到第 {self.page + 1} 页黑名单。'
        self.bind_message(interaction.message)
        await self.prime_user_profiles()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self.rebuild_for_response(),
        )

    async def next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self.error_message = None
        self.status_message = f'已切换到第 {self.page + 1} 页黑名单。'
        self.bind_message(interaction.message)
        await self.prime_user_profiles()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self.rebuild_for_response(),
        )

    async def open_manual_add_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BlacklistManualIdModal(self, action='add'))

    async def open_manual_remove_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BlacklistManualIdModal(self, action='remove'))

    async def close_panel(self, interaction: discord.Interaction) -> None:
        self.closed = True
        self.stop()
        self.bind_message(interaction.message)
        await interaction.response.edit_message(embed=self.build_embed(), view=None)