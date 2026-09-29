"""Discord guild settings. Every submission rechecks the caller's live guild role."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3

import discord

from .guild_settings import PolicyError


async def require_manager(bot, interaction, guild_id):
    if interaction.guild is None or interaction.guild.id != guild_id:
        raise PolicyError('只能管理打开面板时的服务器。')
    chat = bot.get_cog('AtriChat')
    if chat is None:
        raise PolicyError('聊天模块暂不可用。')
    if interaction.user.id == chat.owner_user_id:
        return
    blocked = getattr(bot, 'is_globally_blacklisted', None)
    if callable(blocked) and blocked(interaction.user.id):
        raise PolicyError('当前用户不能使用管理面板。')
    member = await interaction.guild.fetch_member(interaction.user.id)
    if member.bot or (member.id != interaction.guild.owner_id and not member.guild_permissions.administrator):
        raise PolicyError('仅限开发者、当前服务器服主或当前服务器 admin。')


async def reject(interaction, error):
    message = str(error) if isinstance(error, PolicyError) else '操作失败，配置未确认保存；请刷新后再试。'
    method = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await method(message[:1500], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


STATUS_NAMES = dict(never='尚未整理', busy='另一个整理任务进行中', unavailable='服务器或频道暂不可用',
                    disabled='功能已关闭或没有读取权限', cooldown='尚在整理间隔内', empty='暂无已存记忆',
                    local_only='本地整理完成', reviewed='语义核查完成', budget_wait='本地完成，语义预算/冷却待恢复',
                    model_failed='本地完成，模型核查失败（原记忆保留）', changed='数据已变化，丢弃过时结果')


def report_text(report):
    text = STATUS_NAMES.get(report.get('status'), '未知状态')
    if report.get('last_run'):
        text += f'\n最近运行：<t:{int(report["last_run"])}:R>'
    if 'records' in report:
        text += (f'\n检查 {report["records"]} 条；折叠重复 {report["duplicates"]} 条；'
                 f'过时印象 {report["stale"]} 条；本轮建议 {report["suggestions"]} 条。')
    if 'active' in report:
        text += f'\n活跃 {report["active"]} 条 · 归档 {report["archived"]} 条（仍可检索）'
        text += f'\n逻辑存储 {report["bytes"]/1024/1024:.2f} / {report["budget_mb"]} MiB'
        if report.get('capacity_warning'):
            text += '\n容量预警：已用超过 80%，请增加预算或手动管理；不会自动删除。'
    return text


class PolicyModal(discord.ui.Modal):
    def __init__(self, panel, *, channels=False, capacity=False):
        super().__init__(title='聊天频道范围' if channels else '记忆容量' if capacity else '记忆与整理策略', timeout=300)
        self.panel, self.channels, self.revision = panel, channels, panel.policy['revision']
        if channels:
            self.ids = discord.ui.TextInput(label='允许频道 ID / #频道（空=所有频道）',
                         style=discord.TextStyle.paragraph, required=False, max_length=650,
                         default=' '.join(panel.policy['channel_ids']), placeholder='空格、逗号或换行分隔，最多 25 个；线程需单独指定')
            self.add_item(self.ids)
        else:
            self.fields = {}
            fields = ([('memory_user_active', '每人每频道活跃目标（10～3000）'),
                       ('memory_channel_active', '每频道活跃目标（100～30000）'),
                       ('memory_archive_days', '闲置社交记忆归档天数（7～730）'),
                       ('memory_budget_mb', '每频道逻辑存储预算 MiB（16～1024）')] if capacity else
                      [('recall_limit', '每轮自动召回条数（1～6）'), ('organize_interval_hours', '频道自动整理间隔（1～48 小时）'),
                       ('semantic_daily_limit', '本服语义核查上限（滚动 24 小时，0～6 次）')])
            for key, label in fields:
                field = discord.ui.TextInput(label=label, default=str(panel.policy[key]), max_length=5)
                self.fields[key] = field
                self.add_item(field)

    async def on_submit(self, interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            await self.panel.authorize(interaction)
            if self.channels:
                raw = self.ids.value.strip()
                ids = []
                for token in re.split(r'[\s,，]+', raw) if raw else []:
                    match = re.fullmatch(r'(?:<#([0-9]+)>|([0-9]+))', token)
                    if not match:
                        raise PolicyError('请只填写频道 ID 或 #频道提及。')
                    identity = match.group(1) or match.group(2)
                    if identity not in ids:
                        ids.append(identity)
                if len(ids) > 25:
                    raise PolicyError('最多选择 25 个频道。')
                for identity in ids:
                    channel = interaction.guild.get_channel_or_thread(int(identity)) or await self.panel.cog.bot.fetch_channel(int(identity))
                    if getattr(getattr(channel, 'guild', None), 'id', None) != self.panel.guild_id or not callable(getattr(channel, 'send', None)):
                        raise PolicyError('频道必须属于当前服务器且可以发送消息。')
                changes = dict(channel_ids=ids, channel_mode='allowlist' if ids else 'all')
            else:
                try:
                    changes = {key: int(field.value.strip()) for key, field in self.fields.items()}
                except ValueError:
                    raise PolicyError('策略数值必须是整数。') from None
            await self.panel.save(interaction, changes, revision=self.revision, authenticated=True)
            await interaction.followup.send('已保存，仅当前服务器生效。原面板可点击刷新。', ephemeral=True)
        except (PolicyError, discord.HTTPException, sqlite3.Error, OSError) as exc:
            await reject(interaction, exc)


class ServerSettingsView(discord.ui.View):
    def __init__(self, cog, interaction):
        super().__init__(timeout=600)
        self.cog, self.guild_id, self.user_id = cog, interaction.guild.id, interaction.user.id
        self.guild_name = interaction.guild.name
        self.policy = cog.service.store.policies.get(self.guild_id)
        self.message = None
        self.rebuild()

    async def authorize(self, interaction):
        if interaction.user.id != self.user_id:
            raise PolicyError('请使用 /服务器设置 打开自己的面板。')
        await require_manager(self.cog.bot, interaction, self.guild_id)

    async def interaction_check(self, interaction):
        try:
            await self.authorize(interaction)
            return True
        except (PolicyError, discord.HTTPException) as exc:
            await reject(interaction, exc)
            return False

    def embed(self):
        p = self.policy
        whitelisted = self.cog.bot.get_cog('AtriChat')._is_chat_guild_whitelisted(self.guild_id)
        channels = '所有可访问频道' if p['channel_mode'] == 'all' else ' '.join(f'<#{v}>' for v in p['channel_ids']) or '无'
        enabled = lambda key: '开启' if p[key] else '关闭'
        embed = discord.Embed(title=f'萝卜子 · {self.guild_name[:80]}', color=0x96DACE,
                              description='本服配置，立即生效。开关只限制功能，不授予额外管理权限。')
        embed.add_field(name='聊天与搜索', value=f'聊天：{enabled("chat_enabled")}\nAgent 联网搜索：{enabled("web_search_enabled")}\n开发者白名单：{"已通过" if whitelisted else "未通过（本面板不能绕过）"}')
        embed.add_field(name='日语语音', value=f'语音工具：{enabled("tts_enabled")}\n亚托莉音色 · Fish S2.1 Pro Free\n仅发当前频道音频附件，不加入语音频道')
        embed.add_field(name='长期记忆', value=f'记忆：{enabled("memory_enabled")}\n策略：{"事实＋社交记忆" if p["memory_style"] == "social" else "仅事实与约定"}\n自动召回：最多 {p["recall_limit"]} 条')
        embed.add_field(name='容量与归档', value=f'活跃目标：每人每频道 {p["memory_user_active"]} / 每频道 {p["memory_channel_active"]} 条\n旧社交记忆闲置 {p["memory_archive_days"]} 天归档；仍可检索\n每频道逻辑预算 {p["memory_budget_mb"]} MiB；80% 预警\n固定/事实/未完成待办不自动归档；活跃目标是软上限', inline=False)
        embed.add_field(name='聊天/记忆频道范围', value=channels[:1000], inline=False)
        embed.add_field(name='后台整理', value=f'自动整理：{enabled("organize_enabled")} · 间隔 {p["organize_interval_hours"]} 小时\n语义核查：{enabled("semantic_review_enabled")} · 本服 {p["semantic_daily_limit"]} 次/24h\n语义核查沿用聊天模型，有 API 用量；只看本频道已有记忆，不读整段聊天。', inline=False)
        embed.set_footer(text=f'配置版本 {p["revision"]} · 不修改音乐/视频解析、全局 API、白名单或权限层级 · 关闭不删除记忆')
        return embed

    def rebuild(self):
        self.clear_items()
        for index, (key, label) in enumerate([('chat_enabled','聊天'), ('web_search_enabled','Agent 搜索'),
                                             ('memory_enabled','记忆'), ('organize_enabled','自动整理'),
                                             ('semantic_review_enabled','模型核查')]):
            button = discord.ui.Button(label=f'{label}：{"开" if self.policy[key] else "关"}', row=0,
                                       style=discord.ButtonStyle.success if self.policy[key] else discord.ButtonStyle.secondary)
            async def toggle(interaction, key=key):
                await self.apply(interaction, {key: not self.policy[key]})
            button.callback = toggle
            self.add_item(button)
        for label, callback in [('切换记忆模式', self.style), ('频道范围', self.channels), ('整理/召回策略', self.strategy),
                                ('刷新', self.refresh), ('修改记录', self.audit)]:
            button = discord.ui.Button(label=label, row=1)
            button.callback = callback
            self.add_item(button)
        button = discord.ui.Button(label=f'日语语音：{"开" if self.policy["tts_enabled"] else "关"}', row=2)
        async def speech(interaction):
            await self.apply(interaction, {'tts_enabled': not self.policy['tts_enabled']})
        button.callback = speech
        self.add_item(button)
        button = discord.ui.Button(label='记忆容量 / 归档', row=2)
        button.callback = self.capacity
        self.add_item(button)

    async def save(self, interaction, changes, *, revision=None, authenticated=False):
        if not authenticated:
            await self.authorize(interaction)
        self.policy = await asyncio.to_thread(self.cog.service.store.policies.update, self.guild_id, interaction.user.id,
                                             changes, expected_revision=self.policy['revision'] if revision is None else revision)
        self.rebuild()

    async def apply(self, interaction, changes):
        try:
            await interaction.response.defer()
            await self.save(interaction, changes)
            await interaction.edit_original_response(embed=self.embed(), view=self)
        except (PolicyError, discord.HTTPException, sqlite3.Error, OSError) as exc:
            await reject(interaction, exc)

    async def style(self, interaction):
        await self.apply(interaction, {'memory_style': 'facts' if self.policy['memory_style'] == 'social' else 'social'})

    async def channels(self, interaction):
        await interaction.response.send_modal(PolicyModal(self, channels=True))

    async def strategy(self, interaction):
        await interaction.response.send_modal(PolicyModal(self))

    async def capacity(self, interaction):
        await interaction.response.send_modal(PolicyModal(self, capacity=True))

    async def refresh(self, interaction):
        self.policy = self.cog.service.store.policies.get(self.guild_id)
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def audit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await asyncio.to_thread(self.cog.service.store.policies.audit, self.guild_id)
        text = '\n'.join(f'<t:{int(r["updated"])}:R> · 用户 `{r["actor"]}` · 字段：{", ".join(json.loads(r["changed"]))}' for r in rows)
        await interaction.followup.send(text or '还没有修改记录。', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass
