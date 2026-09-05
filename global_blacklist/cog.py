from __future__ import annotations

from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .store import (
    GLOBAL_BLACKLIST_ENV_KEY,
    is_global_blacklisted,
    persist_global_blacklist_ids,
    read_global_blacklist_ids,
    read_owner_discord_id,
    serialize_user_ids,
)


class GlobalBlacklistCog(commands.Cog):
    global_blacklist = app_commands.Group(
        name='全局黑名单',
        description='管理会被 Bot 完全静默忽略的用户',
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.project_root = Path(__file__).resolve().parents[1]
        self.owner_user_id = read_owner_discord_id()
        self._refresh_bot_cache()

    def _refresh_bot_cache(self) -> None:
        self.bot.global_blacklist_ids = read_global_blacklist_ids()

    def _persist(self, user_ids: set[int]) -> None:
        normalized = {int(user_id) for user_id in user_ids if int(user_id) > 0}
        persist_global_blacklist_ids(self.project_root, normalized)
        self.bot.global_blacklist_ids = normalized

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await interaction.response.send_message('这个命令只允许开发者使用。', ephemeral=True)
        return False

    @global_blacklist.command(name='添加', description='把用户加入全局静默黑名单')
    @app_commands.describe(user='要拉黑的用户')
    async def add_user(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await self._ensure_owner(interaction):
            return
        if user.id == self.owner_user_id:
            await interaction.response.send_message('不能把开发者加入全局黑名单。', ephemeral=True)
            return

        user_ids = set(getattr(self.bot, 'global_blacklist_ids', set()))
        if user.id in user_ids:
            await interaction.response.send_message(
                f'{user.mention} 已经在全局黑名单里。',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        user_ids.add(user.id)
        self._persist(user_ids)
        await interaction.response.send_message(
            f'已加入全局黑名单：{user.mention} (`{user.id}`)',
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @global_blacklist.command(name='移除', description='把用户移出全局静默黑名单')
    @app_commands.describe(user='要解除拉黑的用户')
    async def remove_user(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await self._ensure_owner(interaction):
            return

        user_ids = set(getattr(self.bot, 'global_blacklist_ids', set()))
        if user.id not in user_ids:
            await interaction.response.send_message(
                f'{user.mention} 不在全局黑名单里。',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        user_ids.remove(user.id)
        self._persist(user_ids)
        await interaction.response.send_message(
            f'已移出全局黑名单：{user.mention} (`{user.id}`)',
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @global_blacklist.command(name='列表', description='查看当前全局静默黑名单')
    async def list_users(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return

        user_ids = sorted(getattr(self.bot, 'global_blacklist_ids', set()))
        if not user_ids:
            await interaction.response.send_message('全局黑名单为空。', ephemeral=True)
            return

        lines = [f'- `{user_id}`' for user_id in user_ids[:80]]
        if len(user_ids) > 80:
            lines.append(f'- ... 还有 {len(user_ids) - 80} 个')

        await interaction.response.send_message(
            f'当前全局黑名单 ({len(user_ids)}):\n' + '\n'.join(lines),
            ephemeral=True,
        )

    @global_blacklist.command(name='重载', description='从 .env 重新读取全局静默黑名单')
    async def reload_users(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return

        self._refresh_bot_cache()
        serialized = serialize_user_ids(getattr(self.bot, 'global_blacklist_ids', set()))
        await interaction.response.send_message(
            f'已从 `{GLOBAL_BLACKLIST_ENV_KEY}` 重载 {len(self.bot.global_blacklist_ids)} 个用户。'
            + (f'\n`{serialized}`' if serialized else ''),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if is_global_blacklisted(self.bot, getattr(message.author, 'id', None)):
            return
