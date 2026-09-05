from __future__ import annotations

import json
from typing import TYPE_CHECKING

import aiohttp
import discord

from .agent.code_settings import AgentCodeSettings, AgentCodeSettingsError
from .tls import build_verified_connector

if TYPE_CHECKING:
    from .cog import AtriChat


PANEL_TIMEOUT_SECONDS = 900
MODEL_PAGE_SIZE = 25


def _compact(text: str, limit: int = 160) -> str:
    normalized = " ".join(str(text or "").split())
    return normalized if len(normalized) <= limit else normalized[: limit - 3] + "..."


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes", "on", "是", "启用"}:
        return True
    if normalized in {"false", "0", "no", "off", "否", "停用"}:
        return False
    raise ValueError("启用状态请填 true 或 false。")


def _models_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/models"):
        return base
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")] + "/models"
    return base + "/models"


def _extract_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    candidates = payload.get("data")
    if not isinstance(candidates, list):
        candidates = payload.get("models")
    if not isinstance(candidates, list):
        return []
    model_ids: set[str] = set()
    for item in candidates:
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            raw_id = item.get("id") or item.get("name") or item.get("model")
            model_id = raw_id.strip() if isinstance(raw_id, str) else ""
        else:
            model_id = ""
        if model_id:
            model_ids.add(model_id)
    return sorted(model_ids, key=str.casefold)


async def _fetch_models(settings: AgentCodeSettings) -> list[str]:
    if not settings.base_url or not settings.api_key:
        raise AgentCodeSettingsError("请先填写 Base URL 和 API Key。")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    connector = build_verified_connector()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.get(_models_url(settings.base_url), headers=headers) as response:
            body = await response.text(encoding="utf-8", errors="replace")
            if response.status >= 400:
                raise RuntimeError(
                    f"模型列表拉取失败 ({response.status}): {_compact(body, 220)}"
                )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("模型列表接口返回的不是合法 JSON。") from exc
    models = _extract_model_ids(payload)
    if not models:
        raise RuntimeError("上游没有返回可选模型。")
    return models


class AgentCodeSettingsModal(discord.ui.Modal):
    def __init__(self, panel: "AgentCodeSettingsView") -> None:
        super().__init__(title=f"ATRI {panel.display_name} API 设置", timeout=300)
        self.panel = panel
        settings = panel.settings
        self.base_url = discord.ui.TextInput(
            label="Base URL",
            default=settings.base_url,
            placeholder="例如 https://api.example.com/v1",
            required=True,
            max_length=1000,
        )
        self.api_key = discord.ui.TextInput(
            label="API Key（留空保留现有值）",
            default="",
            placeholder=("已设置，留空保留" if settings.api_key else "请输入 API Key"),
            required=False,
            max_length=1000,
        )
        self.model = discord.ui.TextInput(
            label="手动 Model（留空则拉取列表）",
            default="",
            placeholder=(f"当前 {settings.model}" if settings.model else "留空，提交后从 /models 选择"),
            required=False,
            max_length=300,
        )
        self.max_tokens = discord.ui.TextInput(
            label="Max Tokens（可选）",
            default=(str(settings.max_tokens) if settings.max_tokens is not None else ""),
            placeholder="留空使用上游默认值",
            required=False,
            max_length=10,
        )
        self.enabled = discord.ui.TextInput(
            label="启用状态（true / false）",
            default="true" if settings.enabled else "false",
            required=True,
            max_length=5,
        )
        items = [self.base_url, self.api_key]
        if panel.profile != "web_search":
            items.append(self.model)
        items.extend([self.max_tokens, self.enabled])
        for item in items:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.panel.cog.owner_user_id:
            await interaction.response.send_message(
                "这个面板只允许开发者使用。",
                ephemeral=True,
            )
            return
        current = self.panel.settings
        manual_model = (
            "" if self.panel.profile == "web_search" else self.model.value.strip()
        )
        pull_requested = not manual_model
        try:
            enabled = _parse_bool(self.enabled.value)
            raw_max_tokens = self.max_tokens.value.strip()
            max_tokens = int(raw_max_tokens) if raw_max_tokens else None
            settings = AgentCodeSettings(
                enabled=enabled,
                base_url=self.base_url.value.strip(),
                api_key=(self.api_key.value.strip() or current.api_key),
                model=(manual_model or current.model),
                max_tokens=max_tokens,
            )
            await interaction.response.defer(ephemeral=True)
            if manual_model:
                await self.panel.save_settings(settings)
            else:
                # Fetch before persisting. The API key stays only in this
                # owner panel until a model is selected.
                models = await _fetch_models(settings)
                self.panel.pending_settings = settings
                self.panel.available_models = models
                self.panel.model_page = 0
                self.panel.error_message = None
                self.panel.status_message = (
                    f'已拉取 {len(models)} 个模型，请在面板下拉框中选择。'
                )
                self.panel.refresh_model_controls()
                if self.panel.panel_message is None:
                    panel_message = await interaction.followup.send(
                        embed=self.panel.build_embed(),
                        view=self.panel,
                        ephemeral=True,
                        wait=True,
                    )
                    self.panel.bind_message(panel_message)
                else:
                    await self.panel.refresh_message()
                    await interaction.followup.send(
                        '模型列表已拉取，请回到面板选择模型。',
                        ephemeral=True,
                    )
                return
        except (ValueError, AgentCodeSettingsError) as exc:
            if interaction.response.is_done():
                await interaction.followup.send(str(exc), ephemeral=True)
            else:
                await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception as exc:
            self.panel.error_message = _compact(str(exc), 240)
            failure_message = (
                "模型列表拉取失败，设置尚未保存。"
                if pull_requested
                else f"配置已写入，但 {self.panel.display_name} 热更新失败。"
            )
            if interaction.response.is_done():
                await interaction.followup.send(
                    failure_message,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    failure_message,
                    ephemeral=True,
                )
            await self.panel.refresh_message()
            return

        self.panel.error_message = None
        self.panel.status_message = (
            f"设置已保存并热更新，新的 {self.panel.display_name} 任务会立即使用。"
        )
        await self.panel.refresh_message()
        await interaction.followup.send(
            f"已保存并热更新 {self.panel.display_name} 设置。",
            ephemeral=True,
        )


class AgentCodeModelSelect(discord.ui.Select):
    def __init__(self, panel: "AgentCodeSettingsView") -> None:
        self.panel = panel
        page_models = panel.current_page_models
        if page_models:
            page_start = panel.model_page * MODEL_PAGE_SIZE
            options = [
                discord.SelectOption(
                    label=model[:100],
                    value=str(page_start + offset),
                    default=(model == panel.settings.model),
                )
                for offset, model in enumerate(page_models)
            ]
            placeholder = panel.model_placeholder
            disabled = False
        else:
            options = [discord.SelectOption(label="暂无已拉取模型", value="__none__")]
            placeholder = "先编辑连接或点击拉取模型"
            disabled = True
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values or self.values[0] == "__none__":
            await interaction.response.defer(ephemeral=True)
            return
        try:
            model = self.panel.available_models[int(self.values[0])]
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "这个模型选项已过期，请重新拉取列表。",
                ephemeral=True,
            )
            return
        await self.panel.select_model(interaction, model)


class AgentCodeSettingsView(discord.ui.View):
    def __init__(self, cog: "AtriChat", *, profile: str = "maintenance") -> None:
        super().__init__(timeout=PANEL_TIMEOUT_SECONDS)
        if profile not in {"maintenance", "web_search"}:
            raise ValueError("unsupported API settings panel profile")
        self.cog = cog
        self.profile = profile
        self.panel_message: discord.Message | None = None
        self.status_message = "可以编辑连接信息，或不改连接信息直接启用/停用。"
        self.error_message: str | None = None
        self.available_models: list[str] = []
        self.model_page = 0
        self.pending_settings: AgentCodeSettings | None = None
        self.refresh_model_controls()

    @property
    def settings(self) -> AgentCodeSettings:
        return (
            self.cog.web_search_settings
            if self.profile == "web_search"
            else self.cog.agent_code_settings
        )

    @property
    def display_name(self) -> str:
        return "联网搜索 Agent" if self.profile == "web_search" else "开发 Agent"

    @property
    def runtime_enabled(self) -> bool:
        if self.profile == "web_search":
            return self.settings.configured
        return bool(
            self.cog.agent_code_enabled and self.cog.dsh_code_runtime_pool is not None
        )

    async def save_settings(self, settings: AgentCodeSettings) -> None:
        if self.profile == "web_search":
            await self.cog.save_web_search_settings(settings)
        else:
            await self.cog.save_agent_code_settings(settings)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.cog.owner_user_id:
            return True
        await interaction.response.send_message(
            "这个面板只允许开发者使用。",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.pending_settings = None
        self.available_models = []
        if self.panel_message is not None:
            try:
                await self.panel_message.edit(embed=self.build_embed(), view=None)
            except discord.HTTPException:
                pass

    def bind_message(self, message: discord.Message) -> None:
        self.panel_message = message

    @property
    def total_model_pages(self) -> int:
        return max(1, (len(self.available_models) + MODEL_PAGE_SIZE - 1) // MODEL_PAGE_SIZE)

    @property
    def current_page_models(self) -> list[str]:
        start = self.model_page * MODEL_PAGE_SIZE
        return self.available_models[start : start + MODEL_PAGE_SIZE]

    @property
    def model_placeholder(self) -> str:
        start = self.model_page * MODEL_PAGE_SIZE + 1
        end = min((self.model_page + 1) * MODEL_PAGE_SIZE, len(self.available_models))
        return f"选择模型 ({start}-{end} / {len(self.available_models)})"

    def refresh_model_controls(self) -> None:
        for child in tuple(self.children):
            if isinstance(child, AgentCodeModelSelect):
                self.remove_item(child)
        self.add_item(AgentCodeModelSelect(self))
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.custom_id == "agent-code-model-prev":
                child.disabled = not self.available_models or self.model_page <= 0
            elif child.custom_id == "agent-code-model-next":
                child.disabled = (
                    not self.available_models
                    or self.model_page >= self.total_model_pages - 1
                )

    def build_embed(self) -> discord.Embed:
        settings = self.settings
        runtime_enabled = self.runtime_enabled
        color = discord.Color.green() if runtime_enabled else discord.Color.orange()
        embed = discord.Embed(
            title=f"ATRI {self.display_name} 设置",
            description=(
                "仅所有者可见和操作。保存后立即热更新，不重启普通聊天。\n"
                "API Key 只显示“已设置/未设置”，不会在面板中回显。"
            ),
            color=color,
        )
        embed.add_field(
            name="连接",
            value=(
                f'Base URL: `{_compact(settings.base_url or "未设置")}`\n'
                f'API Key: `{"已设置" if settings.api_key else "未设置"}`\n'
                f'Model: `{_compact(settings.model or "未设置")}`\n'
                f'Max Tokens: `{settings.max_tokens or "上游默认"}`'
            ),
            inline=False,
        )
        embed.add_field(
            name="状态",
            value=(
                f'希望启用: `{"是" if settings.enabled else "否"}`\n'
                f'配置完整: `{"是" if settings.configured else "否"}`\n'
                f'运行时已启用: `{"是" if runtime_enabled else "否"}`\n'
                f'最近操作: {self.status_message}'
            ),
            inline=False,
        )
        if self.error_message:
            embed.add_field(name="最近错误", value=self.error_message, inline=False)
        if self.available_models:
            embed.add_field(
                name="模型列表",
                value=(
                    f'已拉取 `{len(self.available_models)}` 个模型，'
                    f'当前第 `{self.model_page + 1}/{self.total_model_pages}` 页。'
                ),
                inline=False,
            )
        return embed

    async def refresh_message(self) -> None:
        if self.panel_message is None:
            return
        await self.panel_message.edit(embed=self.build_embed(), view=self)

    @discord.ui.button(label="编辑 API 设置", style=discord.ButtonStyle.primary, row=1)
    async def edit_settings(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(AgentCodeSettingsModal(self))

    @discord.ui.button(label="拉取模型列表", style=discord.ButtonStyle.secondary, row=1)
    async def fetch_models(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        settings = self.settings
        try:
            self.available_models = await _fetch_models(settings)
        except Exception as exc:
            self.available_models = []
            self.model_page = 0
            self.error_message = _compact(str(exc), 240)
            self.status_message = "模型列表拉取失败。"
            await interaction.followup.send(self.error_message, ephemeral=True)
        else:
            self.pending_settings = settings
            self.model_page = 0
            self.error_message = None
            self.status_message = f"已拉取 {len(self.available_models)} 个模型。"
            await interaction.followup.send(
                "模型列表已刷新，请从下拉框选择。",
                ephemeral=True,
            )
        self.refresh_model_controls()
        await self.refresh_message()

    @discord.ui.button(label="启用 / 停用", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_enabled(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        current = self.settings
        settings = AgentCodeSettings(
            enabled=not current.enabled,
            base_url=current.base_url,
            api_key=current.api_key,
            model=current.model,
            max_tokens=current.max_tokens,
        )
        await interaction.response.defer(ephemeral=True)
        try:
            await self.save_settings(settings)
        except Exception as exc:
            self.error_message = _compact(str(exc), 240)
            self.status_message = "启用状态修改失败。"
            await interaction.followup.send(self.error_message, ephemeral=True)
        else:
            self.error_message = None
            self.status_message = "已启用。" if settings.enabled else "已停用。"
            await interaction.followup.send(self.status_message, ephemeral=True)
        await self.refresh_message()

    @discord.ui.button(
        label="上一页",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="agent-code-model-prev",
    )
    async def previous_page(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.model_page = max(0, self.model_page - 1)
        self.refresh_model_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(
        label="下一页",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="agent-code-model-next",
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.model_page = min(self.total_model_pages - 1, self.model_page + 1)
        self.refresh_model_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def select_model(
        self,
        interaction: discord.Interaction,
        model: str,
    ) -> None:
        base = self.pending_settings or self.settings
        settings = AgentCodeSettings(
            enabled=base.enabled,
            base_url=base.base_url,
            api_key=base.api_key,
            model=model,
            max_tokens=base.max_tokens,
        )
        await interaction.response.defer(ephemeral=True)
        try:
            await self.save_settings(settings)
        except Exception as exc:
            self.error_message = _compact(str(exc), 240)
            self.status_message = "模型保存或热更新失败。"
            await interaction.followup.send(self.error_message, ephemeral=True)
        else:
            self.pending_settings = None
            self.error_message = None
            self.status_message = f"已选择并热更新模型 {model}。"
            await interaction.followup.send(
                f"已切换 {self.display_name} 模型为 `{_compact(model, 100)}`。",
                ephemeral=True,
            )
        self.refresh_model_controls()
        await self.refresh_message()

    @discord.ui.button(label="关闭面板", style=discord.ButtonStyle.danger, row=2)
    async def close_panel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.pending_settings = None
        self.available_models = []
        self.stop()
        await interaction.response.edit_message(embed=self.build_embed(), view=None)
