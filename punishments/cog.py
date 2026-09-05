from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from global_blacklist.store import read_owner_discord_id

from .store import PunishmentRecord, PunishmentStore


def _read_discord_id(name: str) -> int:
    raw = os.getenv(name, '').strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


PUNISHMENT_GUILD_ID = _read_discord_id('PUNISHMENT_GUILD_ID')
PUNISHMENT_ANNOUNCE_CHANNEL_ID = _read_discord_id(
    'PUNISHMENT_ANNOUNCE_CHANNEL_ID'
)
BAN_COLOR = discord.Color.from_rgb(239, 75, 75)
REVOKE_COLOR = discord.Color.from_rgb(67, 181, 129)


def _user_label(user: discord.abc.User) -> str:
    name = getattr(user, 'global_name', None) or getattr(user, 'name', None) or str(user.id)
    return f'{name}\n({user.id})'


def _moderator_label(user: discord.abc.User) -> str:
    return user.mention


def _display_avatar_url(user: object) -> str | None:
    avatar = getattr(user, 'display_avatar', None)
    return getattr(avatar, 'url', None)


def _make_punishment_id(prefix: str = 'D') -> str:
    return f'{prefix}{secrets.token_hex(3).upper()}'


def _parse_user_id(raw: str) -> int | None:
    cleaned = raw.strip()
    if cleaned.startswith('<@') and cleaned.endswith('>'):
        cleaned = cleaned.removeprefix('<@').removesuffix('>').removeprefix('!')
    try:
        user_id = int(cleaned)
    except ValueError:
        return None
    return user_id if user_id > 0 else None


class PunishmentCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.owner_user_id = read_owner_discord_id()
        self.store = PunishmentStore(
            Path(__file__).resolve().parent / 'data' / 'punishments.json'
        )

    def _is_allowed_context(self, interaction: discord.Interaction) -> bool:
        return interaction.guild_id == PUNISHMENT_GUILD_ID

    async def _ensure_allowed(self, interaction: discord.Interaction) -> bool:
        if not self._is_allowed_context(interaction):
            await interaction.response.send_message(
                '这个命令只能在指定服务器使用。',
                ephemeral=True,
            )
            return False
        if interaction.user.id != self.owner_user_id:
            await interaction.response.send_message(
                '这个命令只允许开发者使用。',
                ephemeral=True,
            )
            return False
        return True

    async def _announcement_channel(
        self,
        guild: discord.Guild,
    ) -> discord.abc.Messageable | None:
        if not PUNISHMENT_ANNOUNCE_CHANNEL_ID:
            return None
        channel = guild.get_channel(PUNISHMENT_ANNOUNCE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(PUNISHMENT_ANNOUNCE_CHANNEL_ID)
            except discord.Forbidden:
                print(
                    '[WARN] Cannot access punishment announcement channel: '
                    f'guild_id={guild.id}, channel_id={PUNISHMENT_ANNOUNCE_CHANNEL_ID}'
                )
                return None
            except discord.HTTPException as exc:
                print(
                    '[WARN] Failed to fetch punishment announcement channel: '
                    f'guild_id={guild.id}, channel_id={PUNISHMENT_ANNOUNCE_CHANNEL_ID}, error={exc}'
                )
                return None
        return channel if hasattr(channel, 'send') else None

    def _ban_embed(
        self,
        *,
        target: discord.abc.User,
        moderator: discord.abc.User,
        reason: str,
        punishment_id: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title='⛔ 永久封禁',
            color=BAN_COLOR,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name='成员', value=_user_label(target), inline=True)
        embed.add_field(name='管理员', value=_moderator_label(moderator), inline=True)
        embed.add_field(name='原因', value=reason, inline=False)
        embed.add_field(name='处罚ID', value=punishment_id, inline=False)
        avatar_url = _display_avatar_url(target)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        return embed

    def _revoke_embed(
        self,
        *,
        target: discord.abc.User,
        moderator: discord.abc.User,
        reason: str,
        revoke_id: str,
        original_id: str | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title='✅ 撤销封锁',
            color=REVOKE_COLOR,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name='成员', value=_user_label(target), inline=True)
        embed.add_field(name='管理员', value=_moderator_label(moderator), inline=True)
        embed.add_field(name='原因', value=reason, inline=False)
        if original_id:
            embed.add_field(name='原处罚ID', value=original_id, inline=True)
        embed.add_field(name='撤销ID', value=revoke_id, inline=True)
        avatar_url = _display_avatar_url(target)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        return embed

    async def _publish_announcement(
        self,
        channel: discord.abc.Messageable,
        embed: discord.Embed,
    ) -> None:
        print(
            '[INFO] Sending punishment announcement: '
            f'channel_id={PUNISHMENT_ANNOUNCE_CHANNEL_ID}, title={embed.title}'
        )
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _preflight_announcement(
        self,
        interaction: discord.Interaction,
    ) -> discord.abc.Messageable | None:
        if interaction.guild is None:
            return None
        channel = await self._announcement_channel(interaction.guild)
        if channel is None:
            await interaction.followup.send(
                '处罚公示频道不可访问。请确认 Bot 在 '
                f'<#{PUNISHMENT_ANNOUNCE_CHANNEL_ID}> 有查看频道、发送消息、嵌入链接权限。',
                ephemeral=True,
            )
            return None
        if interaction.guild is not None and interaction.guild.me is not None:
            permissions_for = getattr(channel, 'permissions_for', None)
            if callable(permissions_for):
                perms = permissions_for(interaction.guild.me)
                if not (
                    getattr(perms, 'view_channel', False)
                    and getattr(perms, 'send_messages', False)
                    and getattr(perms, 'embed_links', False)
                ):
                    await interaction.followup.send(
                        '处罚公示频道权限不足。请给 Bot 在 '
                        f'<#{PUNISHMENT_ANNOUNCE_CHANNEL_ID}> 开启查看频道、发送消息、嵌入链接。',
                        ephemeral=True,
                    )
                    return None
        return channel

    @app_commands.command(name='封锁', description='永久封锁成员，并发送处罚公示')
    @app_commands.guilds(discord.Object(id=PUNISHMENT_GUILD_ID))
    @app_commands.describe(user_id='要封锁的用户 ID，支持直接粘贴 @提及', reason='处罚原因')
    async def ban_user(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str,
    ) -> None:
        if not await self._ensure_allowed(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message('当前上下文不是服务器。', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed_user_id = _parse_user_id(user_id)
        if parsed_user_id is None:
            await interaction.followup.send('用户 ID 必须是纯数字，或一个用户 @提及。', ephemeral=True)
            return

        announcement_channel = await self._preflight_announcement(interaction)
        if announcement_channel is None:
            return

        try:
            user = await self.bot.fetch_user(parsed_user_id)
        except discord.HTTPException:
            user = discord.Object(id=parsed_user_id)

        punishment_id = _make_punishment_id()
        audit_reason = (
            f'{punishment_id} by {interaction.user} ({interaction.user.id}): {reason}'
        )

        try:
            await interaction.guild.ban(
                user,
                reason=audit_reason,
                delete_message_seconds=0,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                '封锁失败：Bot 没有封禁成员权限，或目标成员权限高于 Bot。',
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f'封锁失败：{exc}', ephemeral=True)
            return

        record = PunishmentRecord(
            punishment_id=punishment_id,
            action='ban',
            user_id=parsed_user_id,
            user_label=_user_label(user),
            moderator_id=interaction.user.id,
            moderator_label=str(interaction.user),
            reason=reason,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.store.append(record)

        try:
            await self._publish_announcement(
                announcement_channel,
                self._ban_embed(
                    target=user,
                    moderator=interaction.user,
                    reason=reason,
                    punishment_id=punishment_id,
                )
            )
        except Exception as exc:
            await interaction.followup.send(
                f'已封锁，但处罚公示发送失败：{exc}\n处罚ID：`{punishment_id}`',
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f'已封锁 `{parsed_user_id}`，处罚ID：`{punishment_id}`',
            ephemeral=True,
        )

    @app_commands.command(name='恢复封锁', description='解除用户封锁，并发送撤销公示')
    @app_commands.guilds(discord.Object(id=PUNISHMENT_GUILD_ID))
    @app_commands.describe(user_id='要解除封锁的用户 ID', reason='撤销原因')
    async def unban_user(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str,
    ) -> None:
        if not await self._ensure_allowed(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message('当前上下文不是服务器。', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed_user_id = _parse_user_id(user_id)
        if parsed_user_id is None:
            await interaction.followup.send('用户 ID 必须是纯数字，或一个用户 @提及。', ephemeral=True)
            return

        announcement_channel = await self._preflight_announcement(interaction)
        if announcement_channel is None:
            return

        try:
            target = await self.bot.fetch_user(parsed_user_id)
        except discord.HTTPException:
            target = discord.Object(id=parsed_user_id)

        revoke_id = _make_punishment_id('R')
        audit_reason = (
            f'{revoke_id} by {interaction.user} ({interaction.user.id}): {reason}'
        )
        try:
            await interaction.guild.unban(target, reason=audit_reason)
        except discord.NotFound:
            await interaction.followup.send('解除失败：这个用户当前不在服务器封禁列表里。', ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send('解除失败：Bot 没有解除封禁权限。', ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f'解除失败：{exc}', ephemeral=True)
            return

        original = self.store.revoke_active_ban(
            parsed_user_id,
            moderator_id=interaction.user.id,
            moderator_label=str(interaction.user),
            reason=reason,
        )

        try:
            await self._publish_announcement(
                announcement_channel,
                self._revoke_embed(
                    target=target,
                    moderator=interaction.user,
                    reason=reason,
                    revoke_id=revoke_id,
                    original_id=original.punishment_id if original else None,
                )
            )
        except Exception as exc:
            await interaction.followup.send(
                f'已解除封锁，但撤销公示发送失败：{exc}\n撤销ID：`{revoke_id}`',
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f'已解除 `{parsed_user_id}` 的封锁，撤销ID：`{revoke_id}`',
            ephemeral=True,
        )
