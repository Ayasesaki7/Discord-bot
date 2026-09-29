from __future__ import annotations

import json
from types import SimpleNamespace

import discord

from .agent.discord_tools import DiscordToolHost, DiscordToolError


async def cosmetic_command(cog, interaction: discord.Interaction, action: str, args: dict):
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send('幻化功能只能在服务器内使用。', ephemeral=True)
        return
    message = SimpleNamespace(id=interaction.id, guild=interaction.guild, channel=interaction.channel,
                              author=interaction.user, content='', attachments=[], reference=None)
    host = DiscordToolHost(bot=cog.bot, message=message, owner_user_id=cog.owner_user_id)
    try:
        if action == 'status':
            from .agent.cosmetic_roles import CosmeticRoleHost
            service = CosmeticRoleHost(host)
            service._require_manager(await service.snapshot())
        result = await host.execute('cosmetic_' + action, args)
        data = json.loads(result['content'])
        if action in {'status', 'configure'}:
            ordinary = data.get('normal_limit', data.get('normalLimit'))
            privileged = data.get('privileged_limit', data.get('privilegedLimit'))
            total = data.get('area_limit', data.get('areaLimit'))
            ids = data.get('privileged_role_ids', data.get('privilegedRoleIds', []))
            text = '\n'.join([
                f'普通成员额度：{ordinary} 个／人', f'特权成员额度：{privileged} 个／人',
                f'幻化区总量上限：{total} 个',
                '特权身份组：' + ('、'.join(f'<@&{value}>' for value in ids) or '未识别到唯一且位于区外的「幻化权区」'),
                '', '仅在两个边界之间操作；身份组须无额外权限、无频道授权。',
                '普通成员可直接 @ BOT 创建／调整自己的幻化身份组；创建默认仅自己可用并自动佩戴。',
                '旧身份组无需登记，所有人都能给自己佩戴／取下；不会开放改名、改色或删除。',
                '/幻化归属 仅用于管理员有意将旧身份组交给指定成员管理，不是佩戴旧组的前置条件。',
                '修改本命令的参数即可保存设置；创建冷却为 30 秒。',
            ])
        else:
            text = result['summary'] + f'\n身份组：<@&{data["id"]}>\n归属：<@{data["creatorId"]}>\n公开领取：' + ('是' if data['public'] else '否')
        embed = discord.Embed(title='幻化身份组管理', description=text, colour=discord.Colour.from_rgb(174, 149, 231))
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except (DiscordToolError, discord.HTTPException, TimeoutError) as exc:
        await interaction.followup.send(f'未执行：{str(exc)[:1500]}', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
