from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands


ROLE_CLAIM_COLOR = discord.Color.from_rgb(255, 126, 153)
ROLE_SELECT_LIMIT = 25
ROLE_CLAIM_PANEL_CUSTOM_ID = 'role_claim:claim'
ROLE_REMOVE_PANEL_CUSTOM_ID = 'role_claim:remove'


@dataclass(frozen=True)
class RoleClaimOption:
    key: str
    label: str
    role_id: int
    description: str


@dataclass(frozen=True)
class RoleClaimGuildConfig:
    guild_id: int
    manager_role_id: int
    roles: tuple[RoleClaimOption, ...]


EMPTY_ROLE_CLAIM_CONFIG = RoleClaimGuildConfig(
    guild_id=0,
    manager_role_id=0,
    roles=(),
)


def _read_positive_id(name: str) -> int:
    raw = os.getenv(name, '').strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _read_role_claim_configs() -> dict[int, RoleClaimGuildConfig]:
    guild_id = _read_positive_id('ROLE_CLAIM_GUILD_ID')
    manager_role_id = _read_positive_id('ROLE_CLAIM_MANAGER_ROLE_ID')
    raw_options = os.getenv('ROLE_CLAIM_OPTIONS_JSON', '').strip()
    if not guild_id or not manager_role_id or not raw_options:
        return {}

    try:
        payload = json.loads(raw_options)
    except json.JSONDecodeError as exc:
        print(f'[WARN] ROLE_CLAIM_OPTIONS_JSON is invalid JSON: {exc}')
        return {}
    if not isinstance(payload, list):
        print('[WARN] ROLE_CLAIM_OPTIONS_JSON must be a JSON list')
        return {}

    options: list[RoleClaimOption] = []
    for item in payload[:ROLE_SELECT_LIMIT]:
        if not isinstance(item, dict):
            continue
        key = str(item.get('key', '')).strip()
        label = str(item.get('label', '')).strip()
        description = str(item.get('description', '')).strip()
        try:
            role_id = int(item.get('role_id', 0))
        except (TypeError, ValueError):
            continue
        if not key or not label or role_id <= 0:
            continue
        options.append(
            RoleClaimOption(
                key=key[:100],
                label=label[:100],
                role_id=role_id,
                description=(description or label)[:100],
            )
        )

    if not options:
        return {}
    config = RoleClaimGuildConfig(
        guild_id=guild_id,
        manager_role_id=manager_role_id,
        roles=tuple(options),
    )
    return {guild_id: config}


ROLE_CLAIM_GUILD_CONFIGS = _read_role_claim_configs()


def _compact_text(text: str, limit: int = 100) -> str:
    compact = ' '.join(text.split())
    if len(compact) > limit:
        compact = compact[: limit - 3] + '...'
    return compact


def _member_has_role(member: discord.Member, role_id: int) -> bool:
    return any(role.id == role_id for role in member.roles)


def _role_mention(role_id: int) -> str:
    return f'<@&{role_id}>'


class RoleClaimSelect(discord.ui.Select):
    def __init__(
        self,
        cog: 'RoleClaimCog',
        *,
        action: Literal['claim', 'remove'],
        config: RoleClaimGuildConfig,
    ) -> None:
        self.cog = cog
        self.action = action

        placeholder = '选择要领取的身份组' if action == 'claim' else '选择要从自己身上移除的身份组'
        custom_id = ROLE_CLAIM_PANEL_CUSTOM_ID if action == 'claim' else ROLE_REMOVE_PANEL_CUSTOM_ID
        options = [
            discord.SelectOption(
                label=option.label[:100],
                value=option.key,
                description=option.description[:100],
            )
            for option in config.roles[:ROLE_SELECT_LIMIT]
        ]
        if not options:
            options = [
                discord.SelectOption(
                    label='暂无可领取身份组',
                    value='__empty__',
                    description='请稍后再试',
                )
            ]

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id,
            disabled=not config.roles,
            row=0 if action == 'claim' else 1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_role_select(interaction, action=self.action, values=self.values)


class RoleClaimPanelView(discord.ui.View):
    def __init__(
        self,
        cog: 'RoleClaimCog',
        config: RoleClaimGuildConfig | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        active_config = config or cog.default_config
        self.add_item(RoleClaimSelect(cog, action='claim', config=active_config))
        self.add_item(RoleClaimSelect(cog, action='remove', config=active_config))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not getattr(
            interaction.client,
            'is_globally_blacklisted',
            lambda _user_id: False,
        )(interaction.user.id)


class RoleClaimCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def default_config(self) -> RoleClaimGuildConfig:
        return next(
            iter(ROLE_CLAIM_GUILD_CONFIGS.values()),
            EMPTY_ROLE_CLAIM_CONFIG,
        )

    def get_config(self, guild_id: int | None) -> RoleClaimGuildConfig | None:
        if guild_id is None:
            return None
        return ROLE_CLAIM_GUILD_CONFIGS.get(guild_id)

    def get_role_option(
        self,
        config: RoleClaimGuildConfig,
        key: str,
    ) -> RoleClaimOption | None:
        for option in config.roles:
            if option.key == key:
                return option
        return None

    async def _send_private_result(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            await interaction.followup.send(content, ephemeral=True)
        except discord.HTTPException as exc:
            print(
                '[WARN] Failed to send role claim interaction result: '
                f'user_id={getattr(interaction.user, "id", None)}, error={exc}'
            )

    async def _defer_private(self, interaction: discord.Interaction) -> bool:
        if interaction.response.is_done():
            return True
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            return True
        except discord.HTTPException as exc:
            print(
                '[WARN] Failed to defer role claim interaction: '
                f'user_id={getattr(interaction.user, "id", None)}, error={exc}'
            )
            return False

    async def _resolve_member(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
    ) -> discord.Member | None:
        if isinstance(interaction.user, discord.Member):
            return interaction.user

        try:
            return await guild.fetch_member(interaction.user.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            print(
                '[WARN] Failed to resolve role claim member: '
                f'guild_id={guild.id}, user_id={interaction.user.id}, error={exc}'
            )
            return None

    def _resolve_bot_member(self, guild: discord.Guild) -> discord.Member | None:
        if guild.me is not None:
            return guild.me
        if self.bot.user is None:
            return None
        return guild.get_member(self.bot.user.id)

    def _can_manage_role(self, guild: discord.Guild, role: discord.Role) -> tuple[bool, str | None]:
        bot_member = self._resolve_bot_member(guild)
        if bot_member is None:
            return False, '我还没拿到自己在这个服务器里的成员信息，请稍后再试。'
        if role.managed:
            return False, f'{role.mention} 是集成/系统托管身份组，不能手动发放或移除。'
        if not bot_member.guild_permissions.manage_roles:
            return False, '我没有“管理身份组”权限，暂时不能发放或移除身份组。'
        if role >= bot_member.top_role:
            return False, f'{role.mention} 的位置不在我的最高身份组下面，我动不到它。'
        return True, None

    def _build_panel_embed(self, config: RoleClaimGuildConfig, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title='身份组领取',
            description='选择下方菜单领取或移除自己的身份组。操作结果只会你自己看到。',
            color=ROLE_CLAIM_COLOR,
        )
        role_lines = []
        for index, option in enumerate(config.roles, start=1):
            role = guild.get_role(option.role_id)
            role_text = role.mention if role is not None else _role_mention(option.role_id)
            role_lines.append(f'{index}. **{option.label}** - {role_text}')

        embed.add_field(
            name='可领取身份组',
            value='\n'.join(role_lines) if role_lines else '当前还没有配置可领取身份组。',
            inline=False,
        )
        embed.set_footer(text='面板长期有效；如果后续身份组列表更新，可以重新发布一份新面板。')
        return embed

    async def _validate_publish_request(
        self,
        interaction: discord.Interaction,
    ) -> tuple[RoleClaimGuildConfig | None, discord.Member | None]:
        config = self.get_config(interaction.guild_id)
        if interaction.guild is None or config is None:
            await interaction.response.send_message(
                '这个身份组领取面板目前只允许在指定服务器使用。',
                ephemeral=True,
            )
            return None, None

        member = await self._resolve_member(interaction, interaction.guild)
        if member is None:
            await interaction.response.send_message(
                '暂时没能确认你的服务器成员信息，请稍后再试。',
                ephemeral=True,
            )
            return None, None

        if not _member_has_role(member, config.manager_role_id):
            await interaction.response.send_message(
                f'只有拥有 {_role_mention(config.manager_role_id)} 的成员才能发布领取面板。',
                ephemeral=True,
            )
            return None, None

        return config, member

    @app_commands.command(
        name='身份组领取面板',
        description='发布一个长期有效的身份组领取面板',
    )
    async def publish_role_claim_panel(self, interaction: discord.Interaction) -> None:
        config, _member = await self._validate_publish_request(interaction)
        if config is None or interaction.guild is None:
            return

        channel = interaction.channel
        if channel is None or not hasattr(channel, 'send'):
            await interaction.response.send_message(
                '当前上下文里没有可发送面板的公屏频道。',
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        view = RoleClaimPanelView(self, config)
        embed = self._build_panel_embed(config, interaction.guild)
        try:
            message = await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(f'面板发送失败：{exc}', ephemeral=True)
            return

        await interaction.followup.send(
            f'身份组领取面板已发送到公屏：{message.jump_url}',
            ephemeral=True,
        )

    async def handle_role_select(
        self,
        interaction: discord.Interaction,
        *,
        action: Literal['claim', 'remove'],
        values: list[str],
    ) -> None:
        deferred = await self._defer_private(interaction)
        if not deferred:
            return

        try:
            await self._handle_role_select(interaction, action=action, values=values)
        except Exception as exc:
            print(
                '[ERROR] Unexpected role claim interaction failure: '
                f'guild_id={interaction.guild_id}, '
                f'user_id={getattr(interaction.user, "id", None)}, '
                f'action={action}, error={exc}'
            )
            await self._send_private_result(
                interaction,
                f'操作时出了一点问题，请稍后再试：{exc}',
            )

    async def _handle_role_select(
        self,
        interaction: discord.Interaction,
        *,
        action: Literal['claim', 'remove'],
        values: list[str],
    ) -> None:
        config = self.get_config(interaction.guild_id)
        if interaction.guild is None or config is None:
            await self._send_private_result(
                interaction,
                '这个身份组领取面板目前只允许在指定服务器使用。',
            )
            return

        if not values or values[0] == '__empty__':
            await self._send_private_result(interaction, '当前没有可操作的身份组。')
            return

        option = self.get_role_option(config, values[0])
        if option is None:
            await self._send_private_result(
                interaction,
                '这个面板的身份组配置已经更新，请让管理员重新发布一份面板。',
            )
            return

        member = await self._resolve_member(interaction, interaction.guild)
        if member is None:
            await self._send_private_result(
                interaction,
                '暂时没能确认你的服务器成员信息，请稍后再试。',
            )
            return

        role = interaction.guild.get_role(option.role_id)
        if role is None:
            await self._send_private_result(
                interaction,
                f'没有在服务器里找到“{_compact_text(option.label)}”对应的身份组，请联系管理员检查配置。',
            )
            return

        can_manage, reason = self._can_manage_role(interaction.guild, role)
        if not can_manage:
            await self._send_private_result(interaction, reason or '我暂时不能操作这个身份组。')
            return

        has_role = _member_has_role(member, role.id)
        if action == 'claim':
            if has_role:
                await self._send_private_result(
                    interaction,
                    f'你已经拥有 **{option.label}** 身份组了。',
                )
                return
            try:
                await member.add_roles(
                    role,
                    reason=f'Role claim panel: {interaction.user} claimed {option.label}',
                )
            except discord.Forbidden:
                await self._send_private_result(interaction, '我没有权限给你发放这个身份组。')
                return
            except discord.HTTPException as exc:
                await self._send_private_result(interaction, f'发放失败：{exc}')
                return

            await self._send_private_result(
                interaction,
                f'已为你领取 **{option.label}** 身份组。',
            )
            return

        if not has_role:
            await self._send_private_result(
                interaction,
                f'你现在没有 **{option.label}** 身份组，不需要移除。',
            )
            return

        try:
            await member.remove_roles(
                role,
                reason=f'Role claim panel: {interaction.user} removed {option.label}',
            )
        except discord.Forbidden:
            await self._send_private_result(interaction, '我没有权限从你身上移除这个身份组。')
            return
        except discord.HTTPException as exc:
            await self._send_private_result(interaction, f'移除失败：{exc}')
            return

        await self._send_private_result(
            interaction,
            f'已从你身上移除 **{option.label}** 身份组。',
        )
