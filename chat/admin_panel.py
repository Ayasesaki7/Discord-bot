from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import discord

from .tls import build_verified_connector

if TYPE_CHECKING:
    from .cog import AtriChat

MODEL_PAGE_SIZE = 25
CHAT_ADMIN_TIMEOUT_SECONDS = 900


def _compact_text(text: str, limit: int = 400) -> str:
    compact = ' '.join(text.split())
    if len(compact) > limit:
        compact = compact[: limit - 3] + '...'
    return compact


def _mask_secret(secret: str) -> str:
    if not secret:
        return '未设置'
    if len(secret) <= 8:
        return '*' * len(secret)
    return f'{secret[:4]}...{secret[-4:]}'


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


def _derive_models_url(base_url: str) -> str:
    base = base_url.rstrip('/')
    if base.endswith('/models'):
        return base
    if base.endswith('/chat/completions'):
        return f"{base[: -len('/chat/completions')]}/models"
    return f'{base}/models'


def _decode_bytes(body: bytes) -> str:
    for encoding in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode('utf-8', errors='replace')


def _extract_model_ids(data: object) -> list[str]:
    if not isinstance(data, dict):
        return []

    raw_items = data.get('data')
    if not isinstance(raw_items, list):
        raw_items = data.get('models')
    if not isinstance(raw_items, list):
        raw_items = []

    model_ids: set[str] = set()
    for item in raw_items:
        if isinstance(item, str):
            candidate = item.strip()
        elif isinstance(item, dict):
            raw_candidate = item.get('id') or item.get('name') or item.get('model')
            candidate = raw_candidate.strip() if isinstance(raw_candidate, str) else ''
        else:
            candidate = ''

        if candidate:
            model_ids.add(candidate)

    return sorted(model_ids, key=str.lower)


async def _fetch_available_models(cog: 'AtriChat') -> list[str]:
    config = cog.client.config
    base_url = (config.base_url or '').strip()
    if not base_url:
        raise RuntimeError('请先配置上游 URL。')

    headers = {'Accept': 'application/json'}
    if config.api_key:
        headers['Authorization'] = f'Bearer {config.api_key}'

    timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)
    connector = build_verified_connector()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.get(_derive_models_url(base_url), headers=headers) as response:
            body = _decode_bytes(await response.read())
            if response.status >= 400:
                raise RuntimeError(
                    f'模型列表拉取失败 ({response.status}): {_compact_text(body, 260)}'
                )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError('模型列表接口返回的不是合法 JSON。') from exc

    model_ids = _extract_model_ids(payload)
    if not model_ids:
        raise RuntimeError('模型列表接口没有返回任何可选模型。')
    return model_ids


def _resize_synthetic_histories(cog: 'AtriChat', history_limit: int) -> None:
    rebuilt: dict[int, deque] = {}
    for channel_id, history in cog.synthetic_channel_histories.items():
        rebuilt[channel_id] = deque(history, maxlen=history_limit)
    cog.synthetic_channel_histories = rebuilt


def _persist_runtime_config(
    cog: 'AtriChat',
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    history_limit: int | None = None,
    recent_visual_context_window: int | None = None,
) -> dict[str, str]:
    updates: dict[str, str] = {}
    api_config_changed = False

    if base_url is not None:
        normalized_url = base_url.strip()
        if not normalized_url:
            raise ValueError('上游 URL 不能为空。')
        if normalized_url != (cog.client.config.base_url or '').strip():
            cog.client.config.base_url = normalized_url
            os.environ['OPENAI_BASE_URL'] = normalized_url
            updates['OPENAI_BASE_URL'] = normalized_url
            api_config_changed = True

    if api_key is not None:
        normalized_key = api_key.strip()
        cog.client.config.api_key = normalized_key
        os.environ['OPENAI_API_KEY'] = normalized_key
        updates['OPENAI_API_KEY'] = normalized_key
        api_config_changed = True

    if model is not None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError('模型名称不能为空。')
        if normalized_model != (cog.client.config.model or '').strip():
            cog.client.config.model = normalized_model
            os.environ['OPENAI_MODEL'] = normalized_model
            updates['OPENAI_MODEL'] = normalized_model
            api_config_changed = True

    if history_limit is not None:
        if history_limit < 1 or history_limit > 1000:
            raise ValueError('上下文楼层需要在 1 到 1000 之间。')
        cog.history_limit = history_limit
        os.environ['CHAT_HISTORY_LIMIT'] = str(history_limit)
        updates['CHAT_HISTORY_LIMIT'] = str(history_limit)
        _resize_synthetic_histories(cog, history_limit)

    if recent_visual_context_window is not None:
        if recent_visual_context_window < 0 or recent_visual_context_window > 50:
            raise ValueError('视觉上下文楼层需要在 0 到 50 之间。')
        cog.recent_visual_context_window = recent_visual_context_window
        os.environ['CHAT_RECENT_VISUAL_CONTEXT_WINDOW'] = str(recent_visual_context_window)
        updates['CHAT_RECENT_VISUAL_CONTEXT_WINDOW'] = str(recent_visual_context_window)

    if updates:
        _write_env_updates(updates)
        if api_config_changed:
            fortune_cog = cog.bot.get_cog('DailyFortuneCog')
            reload_fortune = getattr(fortune_cog, 'reload_client_from_env', None)
            if callable(reload_fortune):
                reload_fortune()
        safe_updates = {
            key: ('***' if 'KEY' in key else value)
            for key, value in updates.items()
        }
        print(
            '[INFO] Chat runtime config updated: '
            f"{json.dumps(safe_updates, ensure_ascii=False)}"
        )

    return updates


async def _reload_agent_chat_runtime_if_needed(
    cog: 'AtriChat',
    updates: dict[str, str],
) -> bool:
    agent_keys = {'OPENAI_BASE_URL', 'OPENAI_API_KEY', 'OPENAI_MODEL'}
    if not agent_keys.intersection(updates):
        return False
    await cog.reload_agent_chat_runtime_from_env()
    return True


def _persist_draw_router_config(
    cog: 'AtriChat',
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, str]:
    updates: dict[str, str] = {}

    if base_url is not None:
        normalized_url = base_url.strip()
        if not normalized_url:
            raise ValueError('绘图路由 Base URL 不能为空。')
        os.environ['ATRI_DRAW_ROUTER_BASE_URL'] = normalized_url
        updates['ATRI_DRAW_ROUTER_BASE_URL'] = normalized_url

    if api_key is not None:
        normalized_key = api_key.strip()
        os.environ['ATRI_DRAW_ROUTER_API_KEY'] = normalized_key
        updates['ATRI_DRAW_ROUTER_API_KEY'] = normalized_key

    if model is not None:
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError('绘图路由模型不能为空。')
        os.environ['ATRI_DRAW_ROUTER_MODEL'] = normalized_model
        updates['ATRI_DRAW_ROUTER_MODEL'] = normalized_model

    if temperature is not None:
        if temperature < 0.0 or temperature > 2.0:
            raise ValueError('绘图路由温度需要在 0 到 2 之间。')
        value = f'{temperature:g}'
        os.environ['ATRI_DRAW_ROUTER_TEMPERATURE'] = value
        updates['ATRI_DRAW_ROUTER_TEMPERATURE'] = value

    if timeout_seconds is not None:
        if timeout_seconds < 5 or timeout_seconds > 300:
            raise ValueError('绘图路由超时需要在 5 到 300 秒之间。')
        value = str(timeout_seconds)
        os.environ['ATRI_DRAW_ROUTER_TIMEOUT'] = value
        updates['ATRI_DRAW_ROUTER_TIMEOUT'] = value

    if updates:
        _write_env_updates(updates)
        cog.draw_agent.reload_router_client_from_env()
        safe_updates = {
            key: ('***' if 'KEY' in key else value)
            for key, value in updates.items()
        }
        print(
            '[INFO] Draw router runtime config updated: '
            f"{json.dumps(safe_updates, ensure_ascii=False)}"
        )

    return updates


class ChatConfigModal(discord.ui.Modal):
    def __init__(self, panel: 'ChatAdminView'):
        super().__init__(title='编辑聊天配置', timeout=300)
        self.panel = panel
        config = panel.cog.client.config

        self.base_url = discord.ui.TextInput(
            label='上游 Base URL',
            default=config.base_url or '',
            placeholder='例如 https://api.openai.com/v1',
            required=True,
            max_length=400,
        )
        self.api_key = discord.ui.TextInput(
            label='API Key',
            default='',
            placeholder='留空表示保持当前密钥不变',
            required=False,
            max_length=400,
        )
        self.model = discord.ui.TextInput(
            label='模型（可手动填写）',
            default=config.model or '',
            placeholder='可填写未出现在 /models 列表中的模型 ID',
            required=True,
            max_length=200,
        )
        self.history_limit = discord.ui.TextInput(
            label='上下文楼层',
            default=str(panel.cog.history_limit),
            placeholder='例如 200',
            required=True,
            max_length=4,
        )
        self.recent_visual_context_window = discord.ui.TextInput(
            label='视觉上下文楼层',
            default=str(panel.cog.recent_visual_context_window),
            placeholder='例如 3，填 0 表示关闭',
            required=True,
            max_length=2,
        )

        self.add_item(self.base_url)
        self.add_item(self.api_key)
        self.add_item(self.model)
        self.add_item(self.history_limit)
        self.add_item(self.recent_visual_context_window)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.cog.owner_user_id:
            await interaction.response.send_message(
                '这个面板只允许开发者使用。',
                ephemeral=True,
            )
            return

        try:
            history_limit = int(self.history_limit.value.strip())
        except ValueError:
            await interaction.response.send_message(
                '上下文楼层必须是整数。',
                ephemeral=True,
            )
            return

        try:
            recent_visual_context_window = int(self.recent_visual_context_window.value.strip())
        except ValueError:
            await interaction.response.send_message(
                '视觉上下文楼层必须是整数。',
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        old_base_url = self.panel.cog.client.config.base_url
        try:
            updates = _persist_runtime_config(
                self.panel.cog,
                base_url=self.base_url.value,
                api_key=(self.api_key.value if self.api_key.value.strip() else None),
                model=self.model.value,
                history_limit=history_limit,
                recent_visual_context_window=recent_visual_context_window,
            )
            agent_reloaded = await _reload_agent_chat_runtime_if_needed(
                self.panel.cog,
                updates,
            )
            self.panel.error_message = None
            self.panel.status_message = (
                '基础配置已更新并写回 .env，新的请求会立即使用。'
                + (' Agent Runtime 也已切换。' if agent_reloaded else '')
            )

            should_refresh_models = (
                bool(self.api_key.value.strip())
                or self.base_url.value.strip() != old_base_url.strip()
                or not self.panel.available_models
            )
            if should_refresh_models:
                try:
                    self.panel.available_models = await _fetch_available_models(self.panel.cog)
                    self.panel.jump_to_model(self.panel.cog.client.config.model)
                    self.panel.status_message = (
                        '基础配置已更新并写回 .env，模型列表也已经刷新。'
                        + (' Agent Runtime 也已切换。' if agent_reloaded else '')
                    )
                except Exception as exc:
                    self.panel.available_models = []
                    self.panel.model_page = 0
                    self.panel.error_message = _compact_text(str(exc), 320)

            if updates:
                await self.panel.refresh_panel_message()
        except Exception as exc:
            self.panel.error_message = _compact_text(str(exc), 320)
            self.panel.status_message = '配置更新失败，请检查输入内容。'
            await self.panel.refresh_panel_message()


class DrawRouterConfigModal(discord.ui.Modal):
    def __init__(self, panel: 'ChatAdminView'):
        super().__init__(title='编辑绘图路由 API', timeout=300)
        self.panel = panel
        config = panel.cog.draw_agent.router_client.config

        self.base_url = discord.ui.TextInput(
            label='绘图路由 Base URL',
            default=config.base_url or '',
            placeholder='例如 https://api.openai.com/v1',
            required=True,
            max_length=400,
        )
        self.api_key = discord.ui.TextInput(
            label='绘图路由 API Key',
            default='',
            placeholder='留空表示保持当前密钥不变',
            required=False,
            max_length=400,
        )
        self.model = discord.ui.TextInput(
            label='绘图路由模型',
            default=config.model or '',
            placeholder='建议使用快模型，只负责 JSON intent',
            required=True,
            max_length=200,
        )
        self.temperature = discord.ui.TextInput(
            label='绘图路由温度',
            default=f'{config.temperature:g}',
            placeholder='建议 0.0 到 0.2',
            required=True,
            max_length=8,
        )
        self.timeout_seconds = discord.ui.TextInput(
            label='绘图路由超时秒数',
            default=str(config.timeout_seconds),
            placeholder='例如 15 或 30',
            required=True,
            max_length=4,
        )

        self.add_item(self.base_url)
        self.add_item(self.api_key)
        self.add_item(self.model)
        self.add_item(self.temperature)
        self.add_item(self.timeout_seconds)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.cog.owner_user_id:
            await interaction.response.send_message(
                '这个面板只允许开发者使用。',
                ephemeral=True,
            )
            return

        try:
            temperature = float(self.temperature.value.strip())
        except ValueError:
            await interaction.response.send_message(
                '绘图路由温度必须是数字。',
                ephemeral=True,
            )
            return

        try:
            timeout_seconds = int(self.timeout_seconds.value.strip())
        except ValueError:
            await interaction.response.send_message(
                '绘图路由超时必须是整数。',
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            updates = _persist_draw_router_config(
                self.panel.cog,
                base_url=self.base_url.value,
                api_key=(self.api_key.value if self.api_key.value.strip() else None),
                model=self.model.value,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
            )
            self.panel.error_message = None
            self.panel.status_message = (
                '绘图路由 API 已更新并写回 .env，新的意图判断会立即使用。'
            )
            if updates:
                await self.panel.refresh_panel_message()
        except Exception as exc:
            self.panel.error_message = _compact_text(str(exc), 320)
            self.panel.status_message = '绘图路由配置更新失败，请检查输入内容。'
            await self.panel.refresh_panel_message()


class ChatModelSelect(discord.ui.Select):
    def __init__(self, panel: 'ChatAdminView'):
        self.panel = panel
        current_model = panel.cog.client.config.model
        current_page_models = panel.current_page_models
        options = [
            discord.SelectOption(
                label=model[:100],
                value=model,
                default=(model == current_model),
            )
            for model in current_page_models
        ]

        placeholder = panel.model_placeholder
        if not options:
            options = [
                discord.SelectOption(
                    label='暂无可选模型',
                    value='__no_models__',
                )
            ]

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=not current_page_models,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values:
            await interaction.response.defer()
            return
        await self.panel.handle_model_selected(interaction, self.values[0])


class ChatAdminView(discord.ui.View):
    def __init__(self, cog: 'AtriChat'):
        super().__init__(timeout=CHAT_ADMIN_TIMEOUT_SECONDS)
        self.cog = cog
        self.available_models: list[str] = []
        self.model_page = 0
        self.status_message = '可以先编辑连接信息，再刷新模型列表并直接切换模型。'
        self.error_message: str | None = None
        self.panel_message: discord.InteractionMessage | None = None
        self.closed = False
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
    def total_model_pages(self) -> int:
        if not self.available_models:
            return 1
        return ((len(self.available_models) - 1) // MODEL_PAGE_SIZE) + 1

    @property
    def current_page_models(self) -> list[str]:
        start = self.model_page * MODEL_PAGE_SIZE
        end = start + MODEL_PAGE_SIZE
        return self.available_models[start:end]

    @property
    def model_placeholder(self) -> str:
        if not self.available_models:
            return '先点击“刷新模型列表”再选择模型'

        start = self.model_page * MODEL_PAGE_SIZE + 1
        end = min((self.model_page + 1) * MODEL_PAGE_SIZE, len(self.available_models))
        return f'选择模型 ({start}-{end} / {len(self.available_models)})'

    def bind_message(self, message: discord.InteractionMessage) -> None:
        self.panel_message = message

    def jump_to_model(self, model_name: str) -> None:
        if not model_name or model_name not in self.available_models:
            self.model_page = 0
            return

        self.model_page = self.available_models.index(model_name) // MODEL_PAGE_SIZE

    def _rebuild_items(self) -> None:
        self.clear_items()
        self.add_item(ChatModelSelect(self))

        edit_button = discord.ui.Button(
            label='编辑连接/上下文',
            style=discord.ButtonStyle.primary,
            row=1,
        )
        edit_button.callback = self.open_config_modal
        self.add_item(edit_button)

        refresh_button = discord.ui.Button(
            label='刷新模型列表',
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        refresh_button.callback = self.refresh_models
        self.add_item(refresh_button)

        router_button = discord.ui.Button(
            label='编辑绘图路由',
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        router_button.callback = self.open_draw_router_modal
        self.add_item(router_button)

        previous_button = discord.ui.Button(
            label='上一页',
            style=discord.ButtonStyle.secondary,
            disabled=(not self.available_models or self.model_page <= 0),
            row=2,
        )
        previous_button.callback = self.previous_model_page
        self.add_item(previous_button)

        next_button = discord.ui.Button(
            label='下一页',
            style=discord.ButtonStyle.secondary,
            disabled=(not self.available_models or self.model_page >= self.total_model_pages - 1),
            row=2,
        )
        next_button.callback = self.next_model_page
        self.add_item(next_button)

        close_button = discord.ui.Button(
            label='关闭面板',
            style=discord.ButtonStyle.danger,
            row=2,
        )
        close_button.callback = self.close_panel
        self.add_item(close_button)

    def build_embed(self) -> discord.Embed:
        config = self.cog.client.config
        connection_ready = bool(config.base_url and config.api_key)
        chat_ready = self.cog.client.is_configured()

        if self.closed:
            description = '这个管理面板已经关闭。重新执行命令可以再打开一个新的。'
        else:
            description = '仅开发者可用。修改后会立即生效，并同步写回 `.env`。'

        color = discord.Color.from_rgb(255, 95, 95)
        if self.error_message:
            color = discord.Color.orange()
        elif chat_ready:
            color = discord.Color.green()

        embed = discord.Embed(
            title='聊天配置管理面板',
            description=description,
            color=color,
        )
        embed.add_field(
            name='连接配置',
            value=(
                f'Base URL: `{_compact_text(config.base_url or "未设置", 120)}`\n'
                f'API Key: `{_mask_secret(config.api_key)}`\n'
                f'连接就绪: `{"是" if connection_ready else "否"}`'
            ),
            inline=False,
        )
        embed.add_field(
            name='聊天配置',
            value=(
                f'当前模型: `{_compact_text(config.model or "未选择", 120)}`\n'
                f'上下文楼层: `{self.cog.history_limit}`\n'
                f'视觉上下文楼层: `{self.cog.recent_visual_context_window}`\n'
                f'聊天可用: `{"是" if chat_ready else "否"}`'
            ),
            inline=False,
        )
        router_config = self.cog.draw_agent.router_client.config
        router_has_env = any(
            os.getenv(key, '').strip()
            for key in (
                'ATRI_DRAW_ROUTER_BASE_URL',
                'ATRI_DRAW_ROUTER_API_KEY',
                'ATRI_DRAW_ROUTER_MODEL',
            )
        )
        embed.add_field(
            name='绘图路由 API',
            value=(
                f'Base URL: `{_compact_text(router_config.base_url or "未设置", 120)}`\n'
                f'API Key: `{_mask_secret(router_config.api_key)}`\n'
                f'模型: `{_compact_text(router_config.model or "未选择", 120)}`\n'
                f'温度/超时: `{router_config.temperature:g}` / `{router_config.timeout_seconds}s`\n'
                f'来源: `{"独立配置" if router_has_env else "跟随主聊天配置"}`'
            ),
            inline=False,
        )

        if self.available_models:
            start = self.model_page * MODEL_PAGE_SIZE + 1
            end = min((self.model_page + 1) * MODEL_PAGE_SIZE, len(self.available_models))
            model_status = (
                f'已加载 `{len(self.available_models)}` 个模型\n'
                f'当前页: `{self.model_page + 1}/{self.total_model_pages}`\n'
                f'显示范围: `{start}-{end}`'
            )
        else:
            model_status = '还没有加载模型列表。点击“刷新模型列表”后即可选择。'

        embed.add_field(name='模型列表', value=model_status, inline=False)

        status_lines = [f'最近操作: {self.status_message}']
        if self.error_message:
            status_lines.append(f'最近错误: {self.error_message}')
        embed.add_field(name='状态', value='\n'.join(status_lines), inline=False)
        return embed

    async def refresh_panel_message(self) -> None:
        if self.panel_message is None:
            return

        self._rebuild_items()
        await self.panel_message.edit(embed=self.build_embed(), view=None if self.closed else self)

    async def open_config_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ChatConfigModal(self))

    async def open_draw_router_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DrawRouterConfigModal(self))

    async def refresh_models(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            self.available_models = await _fetch_available_models(self.cog)
            self.jump_to_model(self.cog.client.config.model)
            self.error_message = None
            self.status_message = f'模型列表已刷新，共加载 {len(self.available_models)} 个模型。'
        except Exception as exc:
            self.available_models = []
            self.model_page = 0
            self.error_message = _compact_text(str(exc), 320)
            self.status_message = '模型列表刷新失败，请检查连接配置或上游服务。'

        if interaction.message is not None:
            self.bind_message(interaction.message)
        await self.refresh_panel_message()

    async def previous_model_page(self, interaction: discord.Interaction) -> None:
        self.model_page = max(0, self.model_page - 1)
        self.error_message = None
        self.status_message = f'已切换到模型列表第 {self.model_page + 1} 页。'
        self.bind_message(interaction.message)
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next_model_page(self, interaction: discord.Interaction) -> None:
        self.model_page = min(self.total_model_pages - 1, self.model_page + 1)
        self.error_message = None
        self.status_message = f'已切换到模型列表第 {self.model_page + 1} 页。'
        self.bind_message(interaction.message)
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def handle_model_selected(
        self,
        interaction: discord.Interaction,
        model_name: str,
    ) -> None:
        try:
            updates = _persist_runtime_config(self.cog, model=model_name)
            await _reload_agent_chat_runtime_if_needed(self.cog, updates)
            self.error_message = None
            self.status_message = f'当前模型和 Agent Runtime 已切换为 {model_name}。'
            self.jump_to_model(model_name)
        except Exception as exc:
            self.error_message = _compact_text(str(exc), 320)
            self.status_message = '模型切换失败。'

        self.bind_message(interaction.message)
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def close_panel(self, interaction: discord.Interaction) -> None:
        self.closed = True
        self.stop()
        self.bind_message(interaction.message)
        await interaction.response.edit_message(embed=self.build_embed(), view=None)
