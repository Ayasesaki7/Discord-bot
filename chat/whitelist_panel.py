from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import discord

if TYPE_CHECKING:
    from .cog import AtriChat

WHITELIST_ENV_KEY = 'ATRI_CHAT_GUILD_WHITELIST_IDS'
WHITELIST_PAGE_SIZE = 25
WHITELIST_PANEL_TIMEOUT_SECONDS = 900
WHITELIST_ID_PATTERN = re.compile(r'\d+')


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


def read_whitelist_guild_ids_from_env() -> set[int]:
    raw = os.getenv(WHITELIST_ENV_KEY, '').strip()
    if not raw:
        return set()

    guild_ids: set[int] = set()
    for token in WHITELIST_ID_PATTERN.findall(raw):
        try:
            guild_id = int(token)
        except ValueError:
            continue
        if guild_id > 0:
            guild_ids.add(guild_id)
    return guild_ids


def persist_whitelist_guild_ids(cog: 'AtriChat', guild_ids: set[int]) -> None:
    normalized_ids = sorted({int(guild_id) for guild_id in guild_ids if int(guild_id) > 0})
    serialized = ','.join(str(guild_id) for guild_id in normalized_ids)

    cog.whitelisted_guild_ids = set(normalized_ids)
    os.environ[WHITELIST_ENV_KEY] = serialized
    _write_env_updates({WHITELIST_ENV_KEY: serialized})
    print(
        '[INFO] Chat guild whitelist updated: '
        f'count={len(normalized_ids)}, ids={serialized or "(empty)"}'
    )


def parse_whitelist_guild_ids(raw: str) -> set[int]:
    parsed: set[int] = set()
    for token in WHITELIST_ID_PATTERN.findall(raw):
        try:
            guild_id = int(token)
        except ValueError:
            continue
        if guild_id > 0:
            parsed.add(guild_id)
    return parsed


class WhitelistManualIdModal(discord.ui.Modal):
    def __init__(
        self,
        panel: 'ChatWhitelistView',
        *,
        action: Literal['add', 'remove'],
    ):
        self.panel = panel
        self.action = action
        title = '手动添加服务器白名单 ID' if action == 'add' else '手动移除服务器白名单 ID'
        super().__init__(title=title, timeout=300)
        self.guild_ids_input = discord.ui.TextInput(
            label='服务器 ID',
            style=discord.TextStyle.paragraph,
            placeholder='支持多个，按空格、换行、逗号分隔',
            required=True,
            max_length=1000,
        )
        self.add_item(self.guild_ids_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.cog.owner_user_id:
            await interaction.response.send_message(
                '这个面板只允许开发者使用。',
                ephemeral=True,
            )
            return

        guild_ids = parse_whitelist_guild_ids(self.guild_ids_input.value)
        if not guild_ids:
            await interaction.response.send_message(
                '没有识别到有效的服务器 ID。',
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        if self.action == 'add':
            added_count = self.panel.apply_added_ids(guild_ids)
            skipped_count = len(guild_ids) - added_count
            await self.panel.prime_guild_profiles(list(guild_ids))
            if skipped_count > 0:
                self.panel.status_message = (
                    f'手动添加完成：新增 {added_count} 个，{skipped_count} 个原本已在白名单里。'
                )
            else:
                self.panel.status_message = f'已手动加入 {added_count} 个服务器白名单 ID。'
        else:
            removed_count = self.panel.apply_removed_ids(guild_ids)
            missing_count = len(guild_ids) - removed_count
            if missing_count > 0:
                self.panel.status_message = (
                    f'手动移除完成：移出 {removed_count} 个，{missing_count} 个原本不在白名单里。'
                )
            else:
                self.panel.status_message = f'已手动移出 {removed_count} 个服务器白名单 ID。'

        self.panel.error_message = None
        await self.panel.refresh_panel_message()


class WhitelistGuildAddSelect(discord.ui.Select):
    def __init__(self, panel: 'ChatWhitelistView'):
        self.panel = panel
        options = panel.current_candidate_options
        placeholder = (
            '从 bot 当前已加入的服务器中选择并加入白名单'
            if options
            else '当前没有可加入白名单的服务器'
        )

        if not options:
            options = [
                discord.SelectOption(
                    label='当前没有可加入白名单的服务器',
                    value='__empty__',
                )
            ]

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=min(len(options), WHITELIST_PAGE_SIZE),
            options=options,
            disabled=not panel.current_candidate_ids,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_ids = {int(value) for value in self.values if value.isdigit()}
        if not selected_ids:
            await interaction.response.defer()
            return

        added_count = self.panel.apply_added_ids(selected_ids)
        skipped_count = len(selected_ids) - added_count
        await self.panel.prime_guild_profiles(list(selected_ids))
        self.panel.error_message = None
        if skipped_count > 0:
            self.panel.status_message = (
                f'新增 {added_count} 个白名单服务器，{skipped_count} 个本来就在白名单里。'
            )
        else:
            self.panel.status_message = f'已新增 {added_count} 个白名单服务器。'

        self.panel.bind_message(interaction.message)
        await interaction.response.edit_message(
            embed=self.panel.build_embed(),
            view=self.panel.rebuild_for_response(),
        )


class WhitelistGuildRemoveSelect(discord.ui.Select):
    def __init__(self, panel: 'ChatWhitelistView'):
        self.panel = panel
        options = panel.current_whitelist_options
        placeholder = '勾选要移出白名单的服务器' if options else '当前白名单为空'

        if not options:
            options = [
                discord.SelectOption(
                    label='当前白名单为空',
                    value='__empty__',
                )
            ]

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=min(len(options), WHITELIST_PAGE_SIZE),
            options=options,
            disabled=not panel.current_whitelist_ids,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_ids = {int(value) for value in self.values if value.isdigit()}
        if not selected_ids:
            await interaction.response.defer()
            return

        removed_count = self.panel.apply_removed_ids(selected_ids)
        self.panel.error_message = None
        self.panel.status_message = f'已移出 {removed_count} 个白名单服务器。'
        self.panel.bind_message(interaction.message)
        await interaction.response.edit_message(
            embed=self.panel.build_embed(),
            view=self.panel.rebuild_for_response(),
        )


class ChatWhitelistView(discord.ui.View):
    def __init__(self, cog: 'AtriChat', guild: discord.Guild | None):
        super().__init__(timeout=WHITELIST_PANEL_TIMEOUT_SECONDS)
        self.cog = cog
        self.guild = guild
        self.candidate_page = 0
        self.whitelist_page = 0
        self.status_message = '服务器白名单只影响聊天回复，不影响每日运势和其他功能。'
        self.error_message: str | None = None
        self.panel_message: discord.InteractionMessage | None = None
        self.closed = False
        self.guild_name_cache: dict[int, str] = dict(getattr(cog, 'whitelist_guild_names', {}))
        self.guild_icon_cache: dict[int, str] = dict(getattr(cog, 'whitelist_guild_icons', {}))
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
    def all_known_guild_ids(self) -> list[int]:
        ids = {guild.id for guild in self.cog.bot.guilds}
        ids.update(self.cog.whitelisted_guild_ids)
        return sorted(ids)

    @property
    def sorted_whitelist_ids(self) -> list[int]:
        return sorted(self.cog.whitelisted_guild_ids)

    @property
    def candidate_guild_ids(self) -> list[int]:
        return [
            guild_id
            for guild_id in self.all_known_guild_ids
            if guild_id not in self.cog.whitelisted_guild_ids
        ]

    @property
    def total_candidate_pages(self) -> int:
        if not self.candidate_guild_ids:
            return 1
        return ((len(self.candidate_guild_ids) - 1) // WHITELIST_PAGE_SIZE) + 1

    @property
    def total_whitelist_pages(self) -> int:
        if not self.sorted_whitelist_ids:
            return 1
        return ((len(self.sorted_whitelist_ids) - 1) // WHITELIST_PAGE_SIZE) + 1

    @property
    def current_candidate_ids(self) -> list[int]:
        self.candidate_page = max(0, min(self.candidate_page, self.total_candidate_pages - 1))
        start = self.candidate_page * WHITELIST_PAGE_SIZE
        end = start + WHITELIST_PAGE_SIZE
        return self.candidate_guild_ids[start:end]

    @property
    def current_whitelist_ids(self) -> list[int]:
        self.whitelist_page = max(0, min(self.whitelist_page, self.total_whitelist_pages - 1))
        start = self.whitelist_page * WHITELIST_PAGE_SIZE
        end = start + WHITELIST_PAGE_SIZE
        return self.sorted_whitelist_ids[start:end]

    @property
    def current_candidate_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for guild_id in self.current_candidate_ids:
            label = self.format_guild_label(guild_id, include_id=False)
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(guild_id),
                    description=f'ID: {guild_id}'[:100],
                )
            )
        return options

    @property
    def current_whitelist_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for guild_id in self.current_whitelist_ids:
            label = self.format_guild_label(guild_id, include_id=False)
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(guild_id),
                    description=f'ID: {guild_id}'[:100],
                )
            )
        return options

    def bind_message(self, message: discord.InteractionMessage | None) -> None:
        if message is not None:
            self.panel_message = message

    def remember_guild(self, guild: discord.Guild | discord.PartialInviteGuild) -> None:
        name = _compact_text(getattr(guild, 'name', '未知服务器'), 100)
        icon = getattr(guild, 'icon', None)
        icon_url = str(icon.url) if icon is not None else ''
        self.guild_name_cache[guild.id] = name
        self.guild_icon_cache[guild.id] = icon_url
        self.cog.whitelist_guild_names[guild.id] = name
        self.cog.whitelist_guild_icons[guild.id] = icon_url

    async def prime_guild_profiles(self, guild_ids: list[int] | None = None) -> None:
        target_ids = guild_ids or self.current_whitelist_ids or self.current_candidate_ids
        for guild_id in target_ids:
            if guild_id in self.guild_name_cache and guild_id in self.guild_icon_cache:
                continue

            guild = self.cog.bot.get_guild(guild_id)
            if guild is not None:
                self.remember_guild(guild)
                continue

            try:
                fetched_guild = await self.cog.bot.fetch_guild(guild_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
            self.remember_guild(fetched_guild)

    def format_guild_label(self, guild_id: int, *, include_id: bool = True) -> str:
        base = self.guild_name_cache.get(guild_id)
        if base is None:
            guild = self.cog.bot.get_guild(guild_id)
            if guild is not None:
                self.remember_guild(guild)
                base = self.guild_name_cache.get(guild_id)

        if base is None:
            base = '未知服务器'

        if include_id:
            base = f'{base} [{guild_id}]'
        return _compact_text(base, 100)

    def get_icon_preview(self) -> tuple[str | None, str | None]:
        for guild_id in self.current_whitelist_ids:
            icon_url = self.guild_icon_cache.get(guild_id)
            if icon_url:
                return icon_url, self.format_guild_label(guild_id, include_id=False)
        return None, None

    def build_candidate_summary(self) -> str:
        current_ids = self.current_candidate_ids
        if not current_ids:
            return '当前 bot 已加入的服务器都已经在白名单里。'

        preview_ids = current_ids[:10]
        lines = [
            f'{index}. {self.format_guild_label(guild_id)}'
            for index, guild_id in enumerate(
                preview_ids,
                start=self.candidate_page * WHITELIST_PAGE_SIZE + 1,
            )
        ]
        remaining = len(current_ids) - len(preview_ids)
        if remaining > 0:
            lines.append(f'... 当前页还有 {remaining} 项，可通过上方下拉框勾选加入。')
        return '\n'.join(lines)

    def build_whitelist_summary(self) -> str:
        current_ids = self.current_whitelist_ids
        if not current_ids:
            return '当前白名单为空。空白名单时，聊天功能会在所有服务器停止 AI 回复。'

        preview_ids = current_ids[:10]
        lines = [
            f'{index}. {self.format_guild_label(guild_id)}'
            for index, guild_id in enumerate(
                preview_ids,
                start=self.whitelist_page * WHITELIST_PAGE_SIZE + 1,
            )
        ]
        remaining = len(current_ids) - len(preview_ids)
        if remaining > 0:
            lines.append(f'... 当前页还有 {remaining} 项，可通过下方下拉框勾选移除。')
        return '\n'.join(lines)

    def build_embed(self) -> discord.Embed:
        total_count = len(self.sorted_whitelist_ids)
        color = discord.Color.green() if total_count > 0 else discord.Color.orange()
        if self.error_message:
            color = discord.Color.red()

        if self.closed:
            description = '这个服务器白名单面板已经关闭。重新执行命令可以再打开。'
        else:
            description = (
                '仅开发者可用。这里的白名单只控制聊天功能是否允许调用 AI；'
                '每日运势、音乐和其他功能都不会被这份名单拦截。'
            )

        embed = discord.Embed(
            title='聊天服务器白名单面板',
            description=description,
            color=color,
        )
        embed.add_field(
            name='白名单状态',
            value=(
                f'总服务器数: `{total_count}`\n'
                f'当前服务器聊天已放行: `{"是" if self.guild and self.guild.id in self.cog.whitelisted_guild_ids else "否"}`\n'
                f'空白名单行为: `聊天功能全部停用`'
            ),
            inline=False,
        )
        embed.add_field(
            name='可加入的服务器',
            value=(
                f'候选页码: `{self.candidate_page + 1}/{self.total_candidate_pages}`\n'
                f'{self.build_candidate_summary()}'
            ),
            inline=False,
        )
        embed.add_field(
            name='当前白名单',
            value=(
                f'白名单页码: `{self.whitelist_page + 1}/{self.total_whitelist_pages}`\n'
                f'{self.build_whitelist_summary()}'
            ),
            inline=False,
        )
        embed.add_field(
            name='操作说明',
            value=(
                '1. 上方下拉框用于从 bot 已加入的服务器里加入白名单。\n'
                '2. 下方下拉框用于从当前白名单里移除服务器。\n'
                '3. “添加当前服务器 / 移除当前服务器”适合热切换当前群。\n'
                '4. 如果目标服务器暂时不在列表里，可以用手动 ID。'
            ),
            inline=False,
        )
        icon_url, icon_label = self.get_icon_preview()
        if icon_url:
            embed.set_thumbnail(url=icon_url)
            embed.add_field(
                name='图标预览',
                value=f'当前显示的是 `{icon_label}` 的服务器图标。',
                inline=False,
            )
        status_lines = [f'最近操作: {self.status_message}']
        if self.error_message:
            status_lines.append(f'最近错误: {self.error_message}')
        embed.add_field(name='状态', value='\n'.join(status_lines), inline=False)
        return embed

    def _rebuild_items(self) -> None:
        self.clear_items()
        self.add_item(WhitelistGuildAddSelect(self))
        self.add_item(WhitelistGuildRemoveSelect(self))

        previous_candidate_button = discord.ui.Button(
            label='候选上一页',
            style=discord.ButtonStyle.secondary,
            disabled=self.candidate_page <= 0,
            row=2,
        )
        previous_candidate_button.callback = self.previous_candidate_page
        self.add_item(previous_candidate_button)

        next_candidate_button = discord.ui.Button(
            label='候选下一页',
            style=discord.ButtonStyle.secondary,
            disabled=self.candidate_page >= self.total_candidate_pages - 1,
            row=2,
        )
        next_candidate_button.callback = self.next_candidate_page
        self.add_item(next_candidate_button)

        previous_whitelist_button = discord.ui.Button(
            label='白名单上一页',
            style=discord.ButtonStyle.secondary,
            disabled=self.whitelist_page <= 0,
            row=2,
        )
        previous_whitelist_button.callback = self.previous_whitelist_page
        self.add_item(previous_whitelist_button)

        next_whitelist_button = discord.ui.Button(
            label='白名单下一页',
            style=discord.ButtonStyle.secondary,
            disabled=self.whitelist_page >= self.total_whitelist_pages - 1,
            row=2,
        )
        next_whitelist_button.callback = self.next_whitelist_page
        self.add_item(next_whitelist_button)

        add_current_button = discord.ui.Button(
            label='添加当前服务器',
            style=discord.ButtonStyle.primary,
            disabled=self.guild is None,
            row=3,
        )
        add_current_button.callback = self.add_current_guild
        self.add_item(add_current_button)

        remove_current_button = discord.ui.Button(
            label='移除当前服务器',
            style=discord.ButtonStyle.secondary,
            disabled=self.guild is None,
            row=3,
        )
        remove_current_button.callback = self.remove_current_guild
        self.add_item(remove_current_button)

        manual_add_button = discord.ui.Button(
            label='手动添加 ID',
            style=discord.ButtonStyle.primary,
            row=4,
        )
        manual_add_button.callback = self.open_manual_add_modal
        self.add_item(manual_add_button)

        manual_remove_button = discord.ui.Button(
            label='手动移除 ID',
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        manual_remove_button.callback = self.open_manual_remove_modal
        self.add_item(manual_remove_button)

        close_button = discord.ui.Button(
            label='关闭面板',
            style=discord.ButtonStyle.danger,
            row=4,
        )
        close_button.callback = self.close_panel
        self.add_item(close_button)

    def rebuild_for_response(self) -> 'ChatWhitelistView':
        self._rebuild_items()
        return self

    def apply_added_ids(self, guild_ids: set[int]) -> int:
        before = set(self.cog.whitelisted_guild_ids)
        updated = before | set(guild_ids)
        persist_whitelist_guild_ids(self.cog, updated)
        self.candidate_page = min(self.candidate_page, self.total_candidate_pages - 1)
        self.whitelist_page = min(self.whitelist_page, self.total_whitelist_pages - 1)
        return len(updated - before)

    def apply_removed_ids(self, guild_ids: set[int]) -> int:
        before = set(self.cog.whitelisted_guild_ids)
        updated = before - set(guild_ids)
        persist_whitelist_guild_ids(self.cog, updated)
        self.candidate_page = min(self.candidate_page, self.total_candidate_pages - 1)
        self.whitelist_page = min(self.whitelist_page, self.total_whitelist_pages - 1)
        return len(before - updated)

    async def refresh_panel_message(self) -> None:
        if self.panel_message is None:
            return

        await self.prime_guild_profiles()
        self._rebuild_items()
        await self.panel_message.edit(
            embed=self.build_embed(),
            view=None if self.closed else self,
        )

    async def previous_candidate_page(self, interaction: discord.Interaction) -> None:
        self.candidate_page = max(0, self.candidate_page - 1)
        self.error_message = None
        self.status_message = f'已切换到候选服务器第 {self.candidate_page + 1} 页。'
        self.bind_message(interaction.message)
        await self.prime_guild_profiles()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self.rebuild_for_response(),
        )

    async def next_candidate_page(self, interaction: discord.Interaction) -> None:
        self.candidate_page = min(self.total_candidate_pages - 1, self.candidate_page + 1)
        self.error_message = None
        self.status_message = f'已切换到候选服务器第 {self.candidate_page + 1} 页。'
        self.bind_message(interaction.message)
        await self.prime_guild_profiles()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self.rebuild_for_response(),
        )

    async def previous_whitelist_page(self, interaction: discord.Interaction) -> None:
        self.whitelist_page = max(0, self.whitelist_page - 1)
        self.error_message = None
        self.status_message = f'已切换到白名单第 {self.whitelist_page + 1} 页。'
        self.bind_message(interaction.message)
        await self.prime_guild_profiles()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self.rebuild_for_response(),
        )

    async def next_whitelist_page(self, interaction: discord.Interaction) -> None:
        self.whitelist_page = min(self.total_whitelist_pages - 1, self.whitelist_page + 1)
        self.error_message = None
        self.status_message = f'已切换到白名单第 {self.whitelist_page + 1} 页。'
        self.bind_message(interaction.message)
        await self.prime_guild_profiles()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self.rebuild_for_response(),
        )

    async def add_current_guild(self, interaction: discord.Interaction) -> None:
        if self.guild is None:
            await interaction.response.send_message(
                '当前上下文不在服务器里，不能直接添加当前服务器。',
                ephemeral=True,
            )
            return

        self.remember_guild(self.guild)
        added_count = self.apply_added_ids({self.guild.id})
        self.error_message = None
        if added_count:
            self.status_message = f'已将当前服务器 `{self.guild.name}` 加入聊天白名单。'
        else:
            self.status_message = f'当前服务器 `{self.guild.name}` 本来就在聊天白名单里。'
        self.bind_message(interaction.message)
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self.rebuild_for_response(),
        )

    async def remove_current_guild(self, interaction: discord.Interaction) -> None:
        if self.guild is None:
            await interaction.response.send_message(
                '当前上下文不在服务器里，不能直接移除当前服务器。',
                ephemeral=True,
            )
            return

        removed_count = self.apply_removed_ids({self.guild.id})
        self.error_message = None
        if removed_count:
            self.status_message = f'已将当前服务器 `{self.guild.name}` 移出聊天白名单。'
        else:
            self.status_message = f'当前服务器 `{self.guild.name}` 原本就不在聊天白名单里。'
        self.bind_message(interaction.message)
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self.rebuild_for_response(),
        )

    async def open_manual_add_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(WhitelistManualIdModal(self, action='add'))

    async def open_manual_remove_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(WhitelistManualIdModal(self, action='remove'))

    async def close_panel(self, interaction: discord.Interaction) -> None:
        self.closed = True
        self.stop()
        self.bind_message(interaction.message)
        await interaction.response.edit_message(embed=self.build_embed(), view=None)
