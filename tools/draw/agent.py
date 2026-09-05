from __future__ import annotations

import asyncio
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
import discord

try:
    from ...chat.client import OpenAICompatibleClient, OpenAICompatibleConfig
except ImportError:  # Top-level compatibility for local tests and scripts.
    from chat.client import OpenAICompatibleClient, OpenAICompatibleConfig
from .models import CharacterProfile, LastGeneration
from .nai import NovelAIImageClient
from .search import CharacterSearchClient, SearchResult
from .store import DrawStore, normalize_key, normalize_novelai_prompt_syntax


DrawProgressCallback = Callable[[str], Awaitable[None]]
DrawFailureCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DrawHandleResult:
    handled: bool
    succeeded: bool = True

    def __bool__(self) -> bool:
        return self.handled


def _read_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _draw_router_config_from_env(
    fallback: OpenAICompatibleConfig,
) -> OpenAICompatibleConfig:
    base_url = os.getenv("ATRI_DRAW_ROUTER_BASE_URL", "").strip() or fallback.base_url
    api_key = os.getenv("ATRI_DRAW_ROUTER_API_KEY", "").strip() or fallback.api_key
    model = os.getenv("ATRI_DRAW_ROUTER_MODEL", "").strip() or fallback.model
    return OpenAICompatibleConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=_read_env_float("ATRI_DRAW_ROUTER_TEMPERATURE", 0.1),
        timeout_seconds=max(5, _read_env_int("ATRI_DRAW_ROUTER_TIMEOUT", 30)),
        include_stream_usage=False,
        enable_web_search=False,
        retry_count=max(0, _read_env_int("ATRI_DRAW_ROUTER_RETRY_COUNT", 1)),
        retry_backoff_seconds=max(
            0.0,
            _read_env_float("ATRI_DRAW_ROUTER_RETRY_BACKOFF", 0.8),
        ),
        user_agent=fallback.user_agent,
        referer=fallback.referer,
        title=fallback.title,
        extra_headers=fallback.extra_headers,
    )

DRAW_ROUTER_PROMPT = """
You are the routing layer for a Discord chat bot that can draw images with NovelAI.
Return only one JSON object. No markdown.

Available actions:
- chat: the user is only chatting, asking about an image, or discussing drawing without asking the bot to act.
- ask_clarification: the user wants drawing, but the character/preset/request is ambiguous enough that one short question is needed.
- remember_artist_string: save an artist/style string supplied by the user.
- remember_preset: save a drawing preset supplied by the user.
- set_default_preset: switch the user's default preset.
- set_active_artist: switch the user's active saved artist string.
- list_presets: list saved presets and artist strings.
- generate_image: write tags and call NovelAI image generation.

Rules:
- Return chat unless the user is clearly asking for a drawing action, a drawing edit/reroll, or drawing configuration.
- Treat natural chat like "帮我画", "来张", "整一张", "生图", "重抽", "再来一张", "按上次", "同人图" as generate_image.
- If draw_context.replies_to_draw_output is true and the user asks for a visual change such as changing background, pose, expression, outfit, composition, color, or rerolling, use generate_image and set use_previous=true even if the message does not contain words like "画" or "图".
- If the user is only complimenting, joking, asking why the image looks wrong, asking how drawing works, or chatting about the image without requesting a new/edit image, use chat.
- If the user asks to save/remember/store a 画师串 or artist string, use remember_artist_string.
- If the user asks to save/remember/store a 预设 or preset, use remember_preset.
- If the user asks to switch/change/use a saved artist string without asking for a new image, use set_active_artist and fill artist_name with the saved name.
- If the user asks to draw using a saved artist string, use generate_image and fill artist_name with the saved name; do not include that saved artist string name in user_request as a visual tag.
- If an existing/canon/fan character is mentioned and the local profile may not be enough, set needs_character_search=true.
- For character search queries, output objects with name and optional work.
- If the user says "重抽/再来一张/按上次改", set use_previous=true.
- If drawing intent is unclear, use chat instead of calling a tool. Ask clarification only when the user clearly wants drawing but one required detail is missing.
- Keep names short and practical. Use Simplified Chinese in clarification questions.

JSON schema:
{
  "action": "chat|ask_clarification|remember_artist_string|remember_preset|set_default_preset|set_active_artist|list_presets|generate_image",
  "question": "",
  "user_request": "",
  "preset_name": "",
  "artist_name": "",
  "artist_string": "",
  "preset_positive_prefix": "",
  "preset_negative_prompt": "",
  "preset_params": {},
  "make_default": false,
  "needs_character_search": false,
  "character_queries": [{"name": "", "work": ""}],
  "use_previous": false
}
"""

CHARACTER_SUMMARY_PROMPT = """
You summarize web search snippets into a compact character reference for anime image generation.
Return only one JSON object. No markdown.

Requirements:
- If results are weak, keep confidence low instead of inventing.
- Convert useful visual facts into English Danbooru-style prompt tags.
- Avoid plot spoilers unless they change visible appearance.

JSON schema:
{
  "name": "",
  "work": "",
  "aliases": [],
  "traits": [],
  "prompt_tags": [],
  "confidence": 0.0
}
"""

TAG_WRITER_PROMPT = """
You write NovelAI image prompts from a natural Discord chat request.
Return only one JSON object. No markdown.

Requirements:
- Persona mapping is important: if the user asks to draw "you", "yourself", "亚托莉", "ATRI", or this bot in the current conversation, draw ATRI from "ATRI -My Dear Moments-", not Roboco-san, Hololive, or the Discord display name.
- For that persona use visual anchors such as: atri_(atri_-my_dear_moments-), petite android girl, short light blue hair, blue eyes, white-and-blue sailor-style outfit, hair ornament.
- The prompt must be English tags or concise English natural language suitable for NovelAI.
- Prefer comma-separated Danbooru-style tags.
- Preserve NovelAI emphasis syntax exactly, such as {tag}, [tag], and 1.5::tag ::.
- Never rewrite NovelAI numeric emphasis into Stable Diffusion syntax like (tag:1.5).
- Preserve the user's requested scene, pose, mood, outfit, and constraints.
- Use character profiles when provided. Do not invent unknown character details.
- Do not include saved preset prefix or saved artist strings; the local tool will prepend them.
- If active_artist_strings contains a name from the user message, treat that phrase only as tool selection; never include the saved artist string name itself in prompt.
- For a reroll/edit request, preserve useful previous prompt details and only change what the user asked to change.
- Keep prompt under 1200 characters and negative_prompt under 800 characters.
- parameters may include width, height, steps, scale, cfg_rescale, sampler, n_samples, seed, model.

JSON schema:
{
  "prompt": "",
  "negative_prompt": "",
  "parameters": {},
  "character_names": [],
  "note": ""
}
"""

DRAW_PERSONA_REPLY_PROMPT = """
You write one short Discord reply as the active bot persona after a drawing action.
Return plain text only. No JSON, no markdown table.

Rules:
- The active persona is defined by the earlier system prompts from chat/prompt.py. Follow that persona first.
- You are not a separate drawing helper, backend module, agent, or tool. You are the same Discord character speaking naturally.
- Sound like a real Discord chat reply, not a backend status report.
- Do not say you are an AI, a module, a tool, or a service.
- It is okay to mention NAI only when explaining configuration, queueing, or rate limits.
- Be concise: usually 1 sentence, at most 2 short sentences.
- Treat fallback_meaning as meaning only, not wording to copy.
- If prompt/debug facts are provided, do not dump them; the code will append technical footer lines separately.
"""

DRAW_ROUTE_CHAT_THRESHOLD = 25
DRAW_ROUTE_DIRECT_PREVIOUS_THRESHOLD = 60
DRAW_ROUTE_DIRECT_INLINE_THRESHOLD = 85

DRAW_CANDIDATE_PATTERN = re.compile(
    r"(画|绘|图|生图|出图|来张|来个|整个|整张|搞张|搞一张|一张|重抽|再来|按上次|同人|二创|"
    r"nai|novelai|tag|prompt|预设|preset|画师|artist|draw|image|generate)",
    re.IGNORECASE,
)
DRAW_ACTION_PATTERN = re.compile(
    r"(帮.*画|给我.*画|画(?:一下|一张|个|张|幅)|来(?:张|个|一张|一个)|整(?:一)?(?:张|个)|"
    r"搞(?:一)?张|弄(?:一)?张|生图|出图|重抽|重画|"
    r"再来(?:一张)?|按上次|同人图|二创图|draw|generate)",
    re.IGNORECASE,
)
DRAW_BARE_COMMAND_PATTERN = re.compile(
    r"^\s*画(?!图工具|师串|风|面|质|图为什么|图怎么|图是|图的|图里|图中)\S+",
    re.IGNORECASE,
)
DRAW_EDIT_PATTERN = re.compile(
    r"(不要|别|去掉|删掉|换|改|加|加上|变成|换成|改成|重新|重画|重抽|再来|"
    r"背景|姿势|动作|表情|衣服|服装|发型|颜色|构图|镜头|近景|远景|全身|半身|"
    r"横图|竖图|白色背景|透明背景|简单背景|复杂背景|笑|哭|生气|战斗)",
    re.IGNORECASE,
)
DRAW_FOLLOWUP_EDIT_PATTERN = re.compile(
    r"(更像|更有|再(?:强|弱|亮|暗|大|小)|调整|调成|修一下|优化|换个|一点|"
    r"那种感觉|氛围(?:更|换|改)|风格(?:更|换|改)|夏天|冬天|春天|秋天|"
    r"角度|视角|光照|灯光|色调)",
    re.IGNORECASE,
)
DRAW_REFERENCE_PATTERN = re.compile(
    r"(这张|那张|这版|这一版|那版|上一版|这次|上一张|上张|上次|刚才|刚刚|之前|按上次|原图|图里|画面)",
    re.IGNORECASE,
)
DRAW_CONFIG_PATTERN = re.compile(
    r"(nai|novelai|tag|prompt|提示词|预设|preset|画师串|画风|artist|style|seed|sampler|scale|cfg)",
    re.IGNORECASE,
)
DRAW_META_CHAT_PATTERN = re.compile(
    r"(为什么|为啥|怎么|如何|原因|问题|区别|是什么|什么意思|原理|参数|设置|教程|能不能|可以吗)",
    re.IGNORECASE,
)
DRAW_CLEANUP_PATTERN = re.compile(
    r"(删|删除|撤回|清理).{0,12}(图|画|绘图|生图|上一张|上次|刚才|最近)|"
    r"(图|画|绘图|生图).{0,12}(删|删除|撤回|清理)",
    re.IGNORECASE,
)


class AtriDrawAgent:
    def __init__(self, chat_cog: Any) -> None:
        self.chat = chat_cog
        data_path = (
            Path(__file__).resolve().parents[2]
            / "chat"
            / "draw"
            / "data"
            / "draw_memory.json"
        )
        self.store = DrawStore(data_path)
        self.nai = NovelAIImageClient()
        self.search = CharacterSearchClient()
        self.router_client = OpenAICompatibleClient(
            _draw_router_config_from_env(chat_cog.client.config)
        )
        self._nai_generation_lock = asyncio.Lock()
        self._nai_waiting_count = 0
        self._structured_output_retry_count = max(
            0,
            _read_env_int("ATRI_DRAW_STRUCTURED_RETRY_COUNT", 2),
        )
        self._nai_transport_retry_count = max(
            0,
            _read_env_int("NAI_IMAGE_TRANSPORT_RETRY_COUNT", 2),
        )

    def reload_router_client_from_env(self) -> None:
        self.router_client = OpenAICompatibleClient(
            _draw_router_config_from_env(self.chat.client.config)
        )

    async def try_handle_message(
        self,
        *,
        message: discord.Message,
        channel_id: int,
        raw_content: str,
        user_content: Any,
        progress: DrawProgressCallback | None = None,
    ) -> DrawHandleResult:
        if self._looks_like_draw_cleanup(raw_content):
            await self._cleanup_recent_draw_messages(message, raw_content)
            return DrawHandleResult(handled=True)

        scope_key = self._memory_scope_key(message)
        profile = self.store.get_user(message.author.id, scope_key)
        standalone_artist_name = self._detect_standalone_artist_switch(raw_content, profile)
        if standalone_artist_name:
            await self._set_active_artist(
                message,
                {"artist_name": standalone_artist_name},
            )
            return DrawHandleResult(handled=True)
        last_generation = self.store.get_last_generation(message.author.id, scope_key)
        draw_context = await self._build_draw_context(
            message=message,
            raw_content=raw_content,
            last_generation=last_generation,
        )
        route_score = self._draw_route_score(
            raw_content=raw_content,
            profile=profile,
            last_generation=last_generation,
            draw_context=draw_context,
        )
        draw_context["route_score"] = route_score
        if route_score <= DRAW_ROUTE_CHAT_THRESHOLD:
            return DrawHandleResult(handled=False)
        direct_decision = self._build_direct_generate_decision(
            raw_content=raw_content,
            profile=profile,
            last_generation=last_generation,
            draw_context=draw_context,
            route_score=route_score,
        )
        if direct_decision is not None:
            succeeded = await self._generate_image(
                message=message,
                channel_id=channel_id,
                raw_content=raw_content,
                user_content=user_content,
                decision=direct_decision,
                scope_key=scope_key,
                progress=progress,
            )
            return DrawHandleResult(handled=True, succeeded=succeeded)
        try:
            decision = await self._plan_decision(
                raw_content=raw_content,
                user_content=user_content,
                profile=profile,
                last_generation=last_generation,
                draw_context=draw_context,
            )
        except Exception as exc:
            print(
                "[WARN] Draw router failed, falling back to chat: "
                f"error_type={exc.__class__.__name__}"
            )
            return DrawHandleResult(handled=False)

        action = normalize_key(str(decision.get("action") or "chat"))
        if action == "chat":
            return DrawHandleResult(handled=False)
        if action == "ask_clarification":
            question = str(decision.get("question") or "你想让我画的是哪个角色/哪个预设？")
            await message.reply(
                await self._persona_reply(
                    message,
                    event="ask_clarification",
                    facts={"question": question},
                    fallback=question,
                ),
                mention_author=False,
            )
            return DrawHandleResult(handled=True)
        if action == "remember_artist_string":
            await self._remember_artist_string(message, decision, raw_content)
            return DrawHandleResult(handled=True)
        if action == "remember_preset":
            await self._remember_preset(message, decision, raw_content)
            return DrawHandleResult(handled=True)
        if action == "set_default_preset":
            await self._set_default_preset(message, decision)
            return DrawHandleResult(handled=True)
        if action == "set_active_artist":
            await self._set_active_artist(message, decision)
            return DrawHandleResult(handled=True)
        if action == "list_presets":
            await self._list_presets(message)
            return DrawHandleResult(handled=True)
        if action == "generate_image":
            succeeded = await self._generate_image(
                message=message,
                channel_id=channel_id,
                raw_content=raw_content,
                user_content=user_content,
                decision=decision,
                scope_key=scope_key,
                progress=progress,
            )
            return DrawHandleResult(handled=True, succeeded=succeeded)
        return DrawHandleResult(handled=False)

    def _looks_like_draw_candidate(self, raw_content: str) -> bool:
        return bool(DRAW_CANDIDATE_PATTERN.search(raw_content or ""))

    def _looks_like_draw_action(self, raw_content: str) -> bool:
        text = raw_content or ""
        return bool(DRAW_ACTION_PATTERN.search(text) or DRAW_BARE_COMMAND_PATTERN.search(text))

    def _looks_like_draw_edit(self, raw_content: str) -> bool:
        text = raw_content or ""
        return bool(DRAW_EDIT_PATTERN.search(text) or DRAW_FOLLOWUP_EDIT_PATTERN.search(text))

    def _looks_like_draw_reference(self, raw_content: str) -> bool:
        return bool(DRAW_REFERENCE_PATTERN.search(raw_content or ""))

    def _looks_like_draw_config(self, raw_content: str) -> bool:
        return bool(DRAW_CONFIG_PATTERN.search(raw_content or ""))

    def _looks_like_draw_meta_chat(self, raw_content: str) -> bool:
        return bool(DRAW_META_CHAT_PATTERN.search(raw_content or ""))

    def _looks_like_draw_cleanup(self, raw_content: str) -> bool:
        return bool(DRAW_CLEANUP_PATTERN.search(raw_content or ""))

    def _draw_route_score(
        self,
        *,
        raw_content: str,
        profile: Any,
        last_generation: LastGeneration | None,
        draw_context: dict[str, Any],
    ) -> int:
        text = raw_content or ""
        if not text.strip():
            return 0

        score = 0
        has_inline_prompt = self._contains_inline_artist_or_emphasis(text)
        has_requested_artist = bool(self._detect_requested_artist_name(text, profile))
        has_saved_preset = self._mentions_saved_preset(text, profile)
        has_action = self._looks_like_draw_action(text)
        has_candidate = self._looks_like_draw_candidate(text)
        has_config = self._looks_like_draw_config(text)
        is_reply_to_draw = bool(draw_context.get("replies_to_draw_output"))
        is_edit_request = self._looks_like_draw_edit(text)
        has_reference = self._looks_like_draw_reference(text)
        has_previous = last_generation is not None

        if has_inline_prompt:
            score += 55
        if has_requested_artist:
            score += 30
        if has_saved_preset:
            score += 35
        if has_action:
            score += 55
        if has_config:
            score += 35
        if has_candidate:
            score += 18
        if is_edit_request:
            score += 15
        if is_reply_to_draw:
            score += 25
            if is_edit_request:
                score += 35
            elif has_reference:
                score += 20
        if has_previous:
            if is_edit_request and has_reference:
                score += 45
            elif is_edit_request:
                score += 30
            elif has_reference:
                score += 15

        if self._looks_like_draw_meta_chat(text):
            if is_edit_request and (is_reply_to_draw or has_previous):
                score -= 10
            elif not has_action:
                score -= 55
            else:
                score -= 20

        return max(0, min(score, 100))

    def _build_direct_generate_decision(
        self,
        *,
        raw_content: str,
        profile: Any,
        last_generation: LastGeneration | None,
        draw_context: dict[str, Any],
        route_score: int,
    ) -> dict[str, Any] | None:
        text = (raw_content or "").strip()
        if not text:
            return None
        if self._should_keep_router_for_detail_extraction(text, profile):
            return None

        is_reply_to_draw = bool(draw_context.get("replies_to_draw_output"))
        is_edit_request = self._looks_like_draw_edit(text)
        has_reference = self._looks_like_draw_reference(text)
        has_action = self._looks_like_draw_action(text)
        has_inline_prompt = self._contains_inline_artist_or_emphasis(text)
        use_previous = bool(
            (is_reply_to_draw and is_edit_request)
            or (
                last_generation is not None
                and has_reference
                and (is_edit_request or has_action)
            )
        )

        if use_previous and route_score >= DRAW_ROUTE_DIRECT_PREVIOUS_THRESHOLD:
            return {
                "action": "generate_image",
                "user_request": text,
                "use_previous": True,
            }
        if (
            has_inline_prompt
            and has_action
            and route_score >= DRAW_ROUTE_DIRECT_INLINE_THRESHOLD
        ):
            return {
                "action": "generate_image",
                "user_request": text,
                "use_previous": False,
            }
        return None

    def _should_keep_router_for_detail_extraction(self, raw_content: str, profile: Any) -> bool:
        text = raw_content or ""
        if self._mentions_saved_preset(text, profile):
            return True
        return bool(
            re.search(
                r"(保存|记住|存|设为|设置|默认|切换|列表|列出|有哪些|查看).{0,16}"
                r"(预设|preset|画师串|画风|artist|style)|"
                r"(预设|preset|画师串|画风|artist|style).{0,16}"
                r"(保存|记住|存|设为|设置|默认|切换|列表|列出|有哪些|查看)",
                text,
                re.IGNORECASE,
            )
        )

    def _memory_scope_key(self, message: discord.Message) -> str:
        guild = getattr(message, "guild", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        if guild is not None and getattr(guild, "id", None) is not None:
            return f"guild:{guild.id}:channel:{channel_id or message.author.id}"
        return f"dm:{channel_id or message.author.id}"

    async def _build_draw_context(
        self,
        *,
        message: discord.Message,
        raw_content: str,
        last_generation: LastGeneration | None,
    ) -> dict[str, Any]:
        referenced_message = await self.chat._resolve_referenced_message(message)
        replies_to_draw_output = (
            referenced_message is not None
            and self.chat.bot.user is not None
            and referenced_message.author.id == self.chat.bot.user.id
            and self._is_draw_output_message(referenced_message)
        )
        referenced_summary = ""
        if referenced_message is not None and referenced_message.author.bot:
            referenced_summary = self._truncate(
                self._history_safe_message_content(referenced_message),
                1000,
            )
        return {
            "looks_like_draw_candidate": self._looks_like_draw_candidate(raw_content),
            "replies_to_draw_output": replies_to_draw_output,
            "has_scoped_previous_generation": last_generation is not None,
            "referenced_bot_message": referenced_summary,
        }

    def _history_safe_message_content(self, message: discord.Message) -> str:
        content = message.content or ""
        if message.attachments:
            content = f"{content}\n[attachments: {len(message.attachments)}]"
        return content.strip()

    def _detect_requested_artist_name(self, raw_content: str, profile: Any) -> str:
        normalized_content = normalize_key(raw_content or "")
        if not normalized_content:
            return ""
        for artist_name in sorted(profile.artist_strings.keys(), key=len, reverse=True):
            normalized_name = normalize_key(artist_name)
            if normalized_name and normalized_name in normalized_content:
                return artist_name
        return ""

    def _mentions_saved_preset(self, raw_content: str, profile: Any) -> bool:
        normalized_content = normalize_key(raw_content or "")
        if not normalized_content:
            return False
        if not re.search(r"(预设|preset)", raw_content or "", re.IGNORECASE):
            return False
        for preset_name in sorted(profile.presets.keys(), key=len, reverse=True):
            normalized_name = normalize_key(preset_name)
            if normalized_name and normalized_name in normalized_content:
                return True
        return False

    def _looks_like_artist_string_content(self, value: str) -> bool:
        text = value or ""
        return bool(
            "," in text
            or "::" in text
            or re.search(r"\bartist\s*:", text, re.IGNORECASE)
            or re.search(r"\([^)]*:\s*\d+(?:\.\d+)?\)", text)
        )

    def _contains_inline_artist_or_emphasis(self, *values: str) -> bool:
        text = "\n".join(value for value in values if value)
        if not text:
            return False
        return bool(
            re.search(r"\bartist\s*:", text, re.IGNORECASE)
            or re.search(r"-?\d+(?:\.\d+)?\s*::", text)
            or re.search(r"::\s*,?", text)
        )

    def _extract_inline_prompt(self, raw_content: str, routed_request: str = "") -> str:
        fenced = self._extract_first_fenced_block(raw_content)
        if fenced and self._contains_inline_artist_or_emphasis(fenced):
            return fenced

        candidates = [routed_request, raw_content]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            match = re.search(
                r"(?:tag|prompt|提示词|画师串|原文|原始)\s*[:：]\s*(?P<prompt>.+)$",
                text,
                re.IGNORECASE | re.DOTALL,
            )
            if match is not None:
                text = match.group("prompt").strip()
            if self._contains_inline_artist_or_emphasis(text):
                return text
        return ""

    def _extract_first_fenced_block(self, value: str) -> str:
        match = re.search(r"```(?:[A-Za-z0-9_-]+)?\s*(?P<body>.*?)```", value or "", re.DOTALL)
        if match is None:
            return ""
        return match.group("body").strip()

    def _remove_inline_prompt_from_text(self, raw_content: str, inline_prompt: str) -> str:
        text = raw_content or ""
        if not inline_prompt:
            return text.strip()
        fenced_pattern = r"```(?:[A-Za-z0-9_-]+)?\s*.*?```"
        text = re.sub(fenced_pattern, " ", text, count=1, flags=re.DOTALL)
        if text == raw_content:
            text = text.replace(inline_prompt, " ", 1)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _detect_standalone_artist_switch(self, raw_content: str, profile: Any) -> str:
        artist_name = self._detect_requested_artist_name(raw_content, profile)
        if not artist_name:
            return ""
        text = raw_content or ""
        has_artist_context = bool(re.search(r"(画师串|画风|artist|style)", text, re.IGNORECASE))
        has_switch_verb = bool(re.search(r"(改成|改为|换成|切到|切换|使用|用|设为|设置)", text, re.IGNORECASE))
        has_generation_verb = bool(
            re.search(
                r"(帮我画|画一|画个|画张|来张|整一张|生图|出图|重抽|再来|draw|generate|image)",
                text,
                re.IGNORECASE,
            )
        )
        if has_artist_context and has_switch_verb and not has_generation_verb:
            return artist_name
        return ""

    async def _plan_decision(
        self,
        *,
        raw_content: str,
        user_content: Any,
        profile: Any,
        last_generation: LastGeneration | None,
        draw_context: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "user_message": raw_content,
            "context_summary": self.chat._history_safe_content(user_content)[:2000],
            "draw_context": draw_context,
            "available_presets": sorted(profile.presets.keys()),
            "default_preset": profile.default_preset,
            "artist_strings": sorted(profile.artist_strings.keys()),
            "active_artist": profile.active_artist,
            "last_generation": (
                {
                    "user_request": last_generation.user_request,
                    "prompt": last_generation.prompt[:800],
                    "preset_name": last_generation.preset_name,
                    "character_names": last_generation.character_names,
                }
                if last_generation is not None
                else None
            ),
        }
        return await self._request_json_object(
            messages=[
                {"role": "system", "content": DRAW_ROUTER_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            client=self.router_client,
            stage="router",
            temperature=None,
        )

    async def _remember_artist_string(
        self,
        message: discord.Message,
        decision: dict[str, Any],
        raw_content: str,
    ) -> None:
        artist_name = str(decision.get("artist_name") or "default").strip() or "default"
        artist_string = str(decision.get("artist_string") or "").strip()
        if not artist_string:
            artist_string = self._text_after_colon(raw_content)
        if not artist_string:
            await message.reply(
                await self._persona_reply(
                    message,
                    event="missing_artist_string",
                    facts={},
                    fallback="我没抓到要保存的画师串，直接说“记住画师串：xxx”就行。",
                ),
                mention_author=False,
            )
            return
        scope_key = self._memory_scope_key(message)
        profile = self.store.get_user(message.author.id, scope_key)
        if (
            normalize_key(artist_string) in profile.artist_strings
            and not self._looks_like_artist_string_content(artist_string)
        ):
            await self._set_active_artist(
                message,
                {"artist_name": artist_string},
            )
            return
        artist = self.store.upsert_artist_string(
            message.author.id,
            scope_key,
            name=artist_name,
            content=artist_string,
            make_active=True,
        )
        await message.reply(
            await self._persona_reply(
                message,
                event="artist_string_saved",
                facts={"artist_name": artist.name},
                fallback=f"记住了，画师串 `{artist.name}` 已保存，并设为当前使用。",
            ),
            mention_author=False,
        )

    async def _cleanup_recent_draw_messages(
        self,
        message: discord.Message,
        raw_content: str,
    ) -> None:
        channel = message.channel
        if channel is None or not hasattr(channel, "history"):
            return

        limit = self._cleanup_limit_from_text(raw_content)
        deleted_count = 0
        inspected_count = 0
        is_owner = message.author.id == self.chat.owner_user_id

        async for history_message in channel.history(
            limit=80,
            before=message,
            oldest_first=False,
        ):
            inspected_count += 1
            if deleted_count >= limit:
                break
            if self.chat.bot.user is None or history_message.author.id != self.chat.bot.user.id:
                continue
            if not self._is_draw_output_message(history_message):
                continue
            if not is_owner and not await self._message_replies_to_user(
                history_message,
                message.author.id,
            ):
                continue
            try:
                await history_message.delete()
                deleted_count += 1
            except discord.HTTPException as exc:
                print(f"[WARN] Failed to delete draw output message: {exc}")

        if deleted_count <= 0:
            fallback = "我往上翻过了，没找到能删的绘图回复。"
        else:
            fallback = f"清掉了 {deleted_count} 条刚才的绘图回复。"
        await message.reply(
            await self._persona_reply(
                message,
                event="draw_cleanup_complete",
                facts={
                    "deleted_count": deleted_count,
                    "inspected_count": inspected_count,
                    "requested_limit": limit,
                },
                fallback=fallback,
            ),
            mention_author=False,
        )

    def _cleanup_limit_from_text(self, raw_content: str) -> int:
        match = re.search(r"(\d{1,2})", raw_content or "")
        if match is None:
            return 3
        return max(1, min(int(match.group(1)), 10))

    def _is_draw_output_message(self, message: discord.Message) -> bool:
        content = message.content or ""
        if "-# prompt:" in content or "\nprompt:" in content:
            return True
        if "negative:" in content and ("画师串" in content or "seed" in content):
            return True
        if message.attachments and ("画师串" in content or "prompt:" in content):
            return True
        return False

    async def _message_replies_to_user(
        self,
        message: discord.Message,
        user_id: int,
    ) -> bool:
        reference = message.reference
        if reference is None or reference.message_id is None:
            return False
        referenced_message = await self.chat._resolve_referenced_message(message)
        return referenced_message is not None and referenced_message.author.id == user_id

    async def _remember_preset(
        self,
        message: discord.Message,
        decision: dict[str, Any],
        raw_content: str,
    ) -> None:
        preset_name = str(decision.get("preset_name") or "default").strip() or "default"
        positive_prefix = str(decision.get("preset_positive_prefix") or "").strip()
        if not positive_prefix:
            positive_prefix = self._text_after_colon(raw_content)
        if not positive_prefix:
            await message.reply(
                await self._persona_reply(
                    message,
                    event="missing_preset_content",
                    facts={},
                    fallback="我没抓到预设内容，直接说“保存预设 名字：xxx”会更稳。",
                ),
                mention_author=False,
            )
            return
        negative_prompt = str(decision.get("preset_negative_prompt") or "").strip()
        params = decision.get("preset_params")
        preset = self.store.upsert_preset(
            message.author.id,
            self._memory_scope_key(message),
            name=preset_name,
            positive_prefix=positive_prefix,
            negative_prompt=negative_prompt,
            params=params if isinstance(params, dict) else {},
            make_default=bool(decision.get("make_default")),
        )
        suffix = "，并设为默认预设" if bool(decision.get("make_default")) else ""
        await message.reply(
            await self._persona_reply(
                message,
                event="preset_saved",
                facts={"preset_name": preset.name, "make_default": bool(decision.get("make_default"))},
                fallback=f"收好，预设 `{preset.name}` 已保存{suffix}。",
            ),
            mention_author=False,
        )

    async def _set_default_preset(
        self,
        message: discord.Message,
        decision: dict[str, Any],
    ) -> None:
        preset_name = str(decision.get("preset_name") or "").strip()
        if not preset_name:
            await message.reply(
                await self._persona_reply(
                    message,
                    event="missing_default_preset_name",
                    facts={},
                    fallback="你要切到哪个预设？",
                ),
                mention_author=False,
            )
            return
        scope_key = self._memory_scope_key(message)
        if self.store.set_default_preset(message.author.id, scope_key, preset_name):
            await message.reply(
                await self._persona_reply(
                    message,
                    event="default_preset_changed",
                    facts={"preset_name": normalize_key(preset_name)},
                    fallback=f"默认预设切到 `{normalize_key(preset_name)}` 了。",
                ),
                mention_author=False,
            )
            return
        profile = self.store.get_user(message.author.id, scope_key)
        available = ", ".join(f"`{name}`" for name in sorted(profile.presets.keys()))
        await message.reply(
            await self._persona_reply(
                message,
                event="preset_not_found",
                facts={"preset_name": preset_name, "available": available},
                fallback=f"我这里还没有 `{preset_name}` 这个预设。现有预设：{available}",
            ),
            mention_author=False,
        )

    async def _set_active_artist(
        self,
        message: discord.Message,
        decision: dict[str, Any],
    ) -> None:
        artist_name = str(decision.get("artist_name") or "").strip()
        if not artist_name:
            await message.reply(
                await self._persona_reply(
                    message,
                    event="missing_active_artist_name",
                    facts={},
                    fallback="你要切到哪个画师串？",
                ),
                mention_author=False,
            )
            return
        scope_key = self._memory_scope_key(message)
        if self.store.set_active_artist(message.author.id, scope_key, artist_name):
            await message.reply(
                await self._persona_reply(
                    message,
                    event="active_artist_changed",
                    facts={"artist_name": normalize_key(artist_name)},
                    fallback=f"画师串切到 `{normalize_key(artist_name)}` 了。",
                ),
                mention_author=False,
            )
            return
        profile = self.store.get_user(message.author.id, scope_key)
        available = ", ".join(f"`{name}`" for name in sorted(profile.artist_strings.keys())) or "无"
        await message.reply(
            await self._persona_reply(
                message,
                event="artist_not_found",
                facts={"artist_name": artist_name, "available": available},
                fallback=f"我这里还没有 `{artist_name}` 这个画师串。现有画师串：{available}",
            ),
            mention_author=False,
        )

    async def _list_presets(self, message: discord.Message) -> None:
        profile = self.store.get_user(message.author.id, self._memory_scope_key(message))
        presets = ", ".join(f"`{name}`" for name in sorted(profile.presets.keys())) or "无"
        artists = ", ".join(f"`{name}`" for name in sorted(profile.artist_strings.keys())) or "无"
        intro = await self._persona_reply(
            message,
            event="list_presets",
            facts={
                "default_preset": profile.default_preset,
                "presets": sorted(profile.presets.keys()),
                "artist_strings": sorted(profile.artist_strings.keys()),
            },
            fallback="我翻了一下记忆，当前这些预设和画师串在这里。",
        )
        await message.reply(
            f"{intro}\n当前默认预设：`{profile.default_preset}`\n预设：{presets}\n画师串：{artists}",
            mention_author=False,
        )

    async def _generate_image(
        self,
        *,
        message: discord.Message,
        channel_id: int,
        raw_content: str,
        user_content: Any,
        decision: dict[str, Any],
        scope_key: str,
        progress: DrawProgressCallback | None = None,
        report_failure: bool = True,
        on_failure: DrawFailureCallback | None = None,
    ) -> bool:
        if progress is not None:
            await progress("正在整理画面需求…")
        if not self.nai.is_configured():
            failure_text = "我这边画图核心还没接上 `NAI_API_TOKEN`，现在只能先把笔放下。"
            if on_failure is not None:
                await on_failure(failure_text)
            if report_failure:
                await message.reply(
                    failure_text,
                    mention_author=False,
                )
            return False

        status = None if progress is not None else await self._create_draw_status_message(message)
        profile = self.store.get_user(message.author.id, scope_key)
        owner_artist_profile = None
        if message.author.id != self.chat.owner_user_id and not profile.artist_strings:
            owner_artist_profile = self.store.get_user(
                self.chat.owner_user_id,
                scope_key,
            )
        previous_generation = self.store.get_last_generation(message.author.id, scope_key)
        preset = self._select_preset(profile, str(decision.get("preset_name") or ""))
        requested_artist_name = (
            str(decision.get("artist_name") or "").strip()
            or self._detect_requested_artist_name(raw_content, profile)
        )
        inline_style_prompt = self._contains_inline_artist_or_emphasis(
            raw_content,
            str(decision.get("user_request") or ""),
        )
        inline_prompt = (
            self._extract_inline_prompt(raw_content, str(decision.get("user_request") or ""))
            if inline_style_prompt
            else ""
        )
        inline_request = (
            self._remove_inline_prompt_from_text(raw_content, inline_prompt)
            if inline_prompt
            else ""
        )
        artist_strings = (
            self._select_artist_strings(
                profile,
                requested_artist_name,
                fallback_profile=owner_artist_profile,
            )
            if requested_artist_name or not inline_prompt
            else []
        )
        artist_names = [artist.name for artist in artist_strings]
        using_owner_artist_default = bool(
            owner_artist_profile is not None
            and not profile.artist_strings
            and artist_strings
        )
        tag_raw_content = self._strip_artist_selection_from_text(
            raw_content,
            artist_names,
        )
        tag_decision = dict(decision)
        routed_request = str(tag_decision.get("user_request") or "")
        if routed_request:
            tag_decision["user_request"] = self._strip_artist_selection_from_text(
                routed_request,
                artist_names,
            )
        if inline_request:
            tag_decision["user_request"] = inline_request

        failure_stage = "preparing"
        try:
            if inline_prompt:
                failure_stage = "character_profiles"
                character_profiles = await self._resolve_character_profiles(tag_decision, scope_key)
                await self._edit_status_persona(
                    status,
                    message,
                    event="raw_prompt_ready",
                    facts={"mode": "raw_novelai_prompt"},
                    fallback="拿到画师串了，我只补角色和画面 tags。",
                    progress=progress,
                )
                failure_stage = "tag_writer"
                tag_plan = await self._write_tags(
                    raw_content=inline_request or tag_raw_content,
                    user_content=inline_request or tag_raw_content,
                    decision=tag_decision,
                    preset=preset,
                    artist_strings=[],
                    character_profiles=character_profiles,
                    previous_generation=previous_generation,
                )
            else:
                failure_stage = "character_profiles"
                character_profiles = await self._resolve_character_profiles(decision, scope_key)
                await self._edit_status_persona(
                    status,
                    message,
                    event="character_profiles_ready",
                    facts={
                        "characters": [
                            profile.name for profile in character_profiles if profile.name
                        ],
                        "searched": [
                            profile.name for profile in character_profiles if profile.sources
                        ],
                    },
                    fallback="角色资料整理好了，我开始缝 tags。",
                    progress=progress,
                )
                failure_stage = "tag_writer"
                tag_plan = await self._write_tags(
                    raw_content=tag_raw_content,
                    user_content=user_content,
                    decision=tag_decision,
                    preset=preset,
                    artist_strings=artist_strings,
                    character_profiles=character_profiles,
                    previous_generation=previous_generation,
                )

            tag_prompt = normalize_novelai_prompt_syntax(str(tag_plan.get("prompt") or tag_raw_content))
            if inline_prompt:
                tag_prompt = self._strip_inline_style_leaks(tag_prompt)
            preset_positive_prefix = "" if inline_prompt else preset.positive_prefix
            tag_prompt = self._strip_known_prompt_parts(
                tag_prompt,
                [
                    preset_positive_prefix,
                    inline_prompt,
                    *[artist.content for artist in artist_strings],
                ],
            )
            prompt = self._join_prompt_parts(
                preset_positive_prefix,
                inline_prompt,
                *[artist.content for artist in artist_strings],
                tag_prompt,
            )
            prompt = normalize_novelai_prompt_syntax(prompt)
            negative_prompt = self._join_prompt_parts(
                preset.negative_prompt,
                str(tag_plan.get("negative_prompt") or ""),
            )
            negative_prompt = normalize_novelai_prompt_syntax(negative_prompt)
            params = dict(preset.params)
            plan_params = tag_plan.get("parameters")
            if isinstance(plan_params, dict):
                params.update(plan_params)

            failure_stage = "novelai_generation"
            images, debug_payload = await self._run_queued_nai_generation(
                message=message,
                status=status,
                progress=progress,
                prompt=prompt,
                negative_prompt=negative_prompt,
                params=params,
            )
            failure_stage = "discord_delivery"
            files = [
                discord.File(
                    io.BytesIO(image.data),
                    filename=self._safe_image_filename(index, image.filename),
                )
                for index, image in enumerate(images, start=1)
            ]
            result_text = await self._build_result_text(
                message=message,
                preset_name=preset.name,
                artist_names=(
                    ["所有者默认画风"]
                    if using_owner_artist_default
                    else [artist.name for artist in artist_strings]
                ),
                prompt=(
                    self._strip_known_prompt_parts(
                        prompt,
                        [artist.content for artist in artist_strings],
                    )
                    if using_owner_artist_default
                    else prompt
                ),
                negative_prompt=negative_prompt,
                params=debug_payload.get("parameters", params),
                model=str(debug_payload.get("model") or self.nai.config.model),
                character_profiles=character_profiles,
            )
            sent_message = await self._finalize_status_with_images(
                status=status,
                message=message,
                content=result_text,
                files=files,
            )

            self.store.save_scoped_last_generation(
                message.author.id,
                scope_key,
                LastGeneration(
                    user_request=str(decision.get("user_request") or raw_content),
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    model=str(debug_payload.get("model") or self.nai.config.model),
                    params=dict(debug_payload.get("parameters", params)),
                    preset_name=preset.name,
                    artist_names=[artist.name for artist in artist_strings],
                    character_names=[
                        profile.name for profile in character_profiles if profile.name
                    ],
                    image_message_url=sent_message.jump_url,
                ),
            )
            self._store_synthetic_assistant_turn(channel_id, result_text, sent_message.created_at)
            return True
        except Exception as exc:
            print(
                "[ERROR] Draw generation failed: "
                f"stage={failure_stage}, error_type={exc.__class__.__name__}"
            )
            failure_text = self._safe_draw_failure_text(exc, stage=failure_stage)
            if on_failure is not None:
                await on_failure(failure_text)
            if not report_failure:
                return False
            if status is not None:
                try:
                    await status.edit(content=failure_text)
                    return False
                except discord.HTTPException:
                    pass
            try:
                await message.reply(
                    failure_text,
                    mention_author=False,
                )
            except discord.HTTPException:
                pass
            return False

    def _select_preset(self, profile: Any, requested_name: str) -> Any:
        requested_key = normalize_key(requested_name)
        if requested_key and requested_key in profile.presets:
            return profile.presets[requested_key]
        if profile.default_preset in profile.presets:
            return profile.presets[profile.default_preset]
        return profile.presets["default"]

    def _select_artist_strings(
        self,
        profile: Any,
        requested_name: str = "",
        *,
        fallback_profile: Any | None = None,
    ) -> list[Any]:
        requested_key = normalize_key(requested_name)
        if requested_key and requested_key in profile.artist_strings:
            return [profile.artist_strings[requested_key]]
        if profile.active_artist and profile.active_artist in profile.artist_strings:
            return [profile.artist_strings[profile.active_artist]]
        if profile.artist_strings:
            return []
        if fallback_profile is not None:
            if requested_key and requested_key in fallback_profile.artist_strings:
                return [fallback_profile.artist_strings[requested_key]]
            if (
                fallback_profile.active_artist
                and fallback_profile.active_artist in fallback_profile.artist_strings
            ):
                return [fallback_profile.artist_strings[fallback_profile.active_artist]]
            if fallback_profile.artist_strings:
                newest = max(
                    fallback_profile.artist_strings.values(),
                    key=lambda artist: artist.updated_at,
                )
                return [newest]
        return []

    def _strip_artist_selection_from_text(self, raw_content: str, artist_names: list[str]) -> str:
        cleaned = raw_content or ""
        for artist_name in sorted({name for name in artist_names if name}, key=len, reverse=True):
            escaped = re.escape(artist_name)
            cleaned = re.sub(
                rf"(用|使用|换成|改成|改为|切到|切换到|设为|设置为)?\s*{escaped}\s*(画师串|画风|artist string|style)?",
                " ",
                cleaned,
                flags=re.IGNORECASE,
            )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or raw_content

    def _strip_known_prompt_parts(self, prompt: str, parts: list[str]) -> str:
        cleaned = prompt
        for part in sorted({item for item in parts if item}, key=len, reverse=True):
            normalized_part = normalize_novelai_prompt_syntax(part)
            if not normalized_part:
                continue
            for candidate in {part, normalized_part}:
                if not candidate:
                    continue
                escaped = re.escape(candidate)
                cleaned = re.sub(rf"(^|,\s*){escaped}(?=,|$)", r"\1", cleaned)
        cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
        cleaned = re.sub(r"^(,\s*)+|(\s*,)+$", "", cleaned)
        cleaned = re.sub(r",\s*,+", ", ", cleaned)
        return cleaned.strip()

    def _strip_inline_style_leaks(self, prompt: str) -> str:
        style_words = {
            "amazing quality",
            "best quality",
            "high quality",
            "masterpiece",
            "no text",
            "ultra-detailed",
            "ultra detailed",
            "very aesthetic",
        }
        output: list[str] = []
        for raw_chunk in str(prompt or "").split(","):
            chunk = " ".join(raw_chunk.split())
            if not chunk:
                continue
            lowered = chunk.casefold()
            if "artist:" in lowered or "::" in lowered:
                continue
            if lowered in style_words:
                continue
            output.append(chunk)
        return ", ".join(output)

    async def _run_queued_nai_generation(
        self,
        *,
        message: discord.Message,
        status: discord.Message | None,
        progress: DrawProgressCallback | None,
        prompt: str,
        negative_prompt: str,
        params: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        queued_position = 0
        counted_as_waiting = False
        if self._nai_generation_lock.locked():
            self._nai_waiting_count += 1
            counted_as_waiting = True
            queued_position = self._nai_waiting_count
            await self._edit_status_persona(
                status,
                message,
                event="draw_queued",
                facts={"queued_position": queued_position},
                fallback=f"NAI 现在正忙，我把你这张塞进队列了，前面大约还有 {queued_position} 个任务。",
                progress=progress,
            )

        try:
            async with self._nai_generation_lock:
                if counted_as_waiting:
                    self._nai_waiting_count = max(self._nai_waiting_count - 1, 0)
                    counted_as_waiting = False
                await self._edit_status_persona(
                    status,
                    message,
                    event="draw_queue_turn_started",
                    facts={},
                    fallback="轮到你这张了，我开始让 NAI 动笔。",
                    progress=progress,
                )

                async def on_rate_limit_retry(attempt: int, delay: float) -> None:
                    await self._edit_status_persona(
                        status,
                        message,
                        event="nai_rate_limited",
                        facts={"attempt": attempt, "delay_seconds": round(delay)},
                        fallback=f"NAI 那边限流了，我会等约 {delay:.0f} 秒后再试一次。",
                        progress=progress,
                    )

                transport_retries = int(
                    getattr(self, "_nai_transport_retry_count", 2)
                )
                for attempt in range(transport_retries + 1):
                    try:
                        return await self.nai.generate_image(
                            prompt=prompt,
                            negative_prompt=negative_prompt,
                            params=params,
                            on_rate_limit_retry=on_rate_limit_retry,
                        )
                    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                        if attempt >= transport_retries:
                            raise RuntimeError(
                                "NovelAI transport connection failed after retries"
                            ) from exc
                        delay = min(1.5 * (attempt + 1), 5.0)
                        print(
                            "[WARN] NovelAI transport will retry: "
                            f"error_type={exc.__class__.__name__}, "
                            f"attempt={attempt + 1}/{transport_retries + 1}, "
                            f"delay_seconds={delay:.1f}"
                        )
                        await self._edit_status_persona(
                            status,
                            message,
                            event="nai_transport_retry",
                            facts={
                                "attempt": attempt + 1,
                                "delay_seconds": delay,
                            },
                            fallback=(
                                "NAI 连接刚才抖了一下，我正在重新连接，"
                                f"约 {delay:.0f} 秒后继续。"
                            ),
                            progress=progress,
                        )
                        await asyncio.sleep(delay)
        except Exception:
            if counted_as_waiting:
                self._nai_waiting_count = max(self._nai_waiting_count - 1, 0)
            raise

    async def _resolve_character_profiles(
        self,
        decision: dict[str, Any],
        scope_key: str,
    ) -> list[CharacterProfile]:
        raw_queries = decision.get("character_queries")
        if not isinstance(raw_queries, list):
            return []

        profiles: list[CharacterProfile] = []
        for item in raw_queries[:4]:
            if isinstance(item, str):
                name = item.strip()
                work = ""
            elif isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                work = str(item.get("work") or "").strip()
            else:
                continue
            if not name:
                continue

            cached = self.store.find_character(name, scope_key)
            if cached is not None and cached.confidence >= 0.35:
                profiles.append(cached)
                continue

            if not bool(decision.get("needs_character_search")) or not self.search.is_configured():
                profiles.append(CharacterProfile(name=name, work=work))
                continue

            results = await self.search.search_character(name, work)
            summarized = await self._summarize_character(name, work, results)
            if summarized.sources or summarized.prompt_tags or summarized.traits:
                self.store.save_character(summarized, scope_key)
            profiles.append(summarized)
        return profiles

    async def _summarize_character(
        self,
        name: str,
        work: str,
        results: list[SearchResult],
    ) -> CharacterProfile:
        if not results:
            return CharacterProfile(name=name, work=work)

        payload = {
            "requested_name": name,
            "requested_work": work,
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                }
                for result in results
            ],
        }
        data = await self._request_json_object(
            messages=[
                {"role": "system", "content": CHARACTER_SUMMARY_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            client=self.chat.client,
            stage="character_summary",
            temperature=0.1,
        )
        profile = CharacterProfile.from_dict(
            {
                "name": data.get("name") or name,
                "work": data.get("work") or work,
                "aliases": data.get("aliases") or [],
                "traits": data.get("traits") or [],
                "prompt_tags": data.get("prompt_tags") or [],
                "confidence": data.get("confidence") or 0.0,
                "sources": [result.url for result in results if result.url],
            }
        )
        return profile

    async def _write_tags(
        self,
        *,
        raw_content: str,
        user_content: Any,
        decision: dict[str, Any],
        preset: Any,
        artist_strings: list[Any],
        character_profiles: list[CharacterProfile],
        previous_generation: LastGeneration | None,
    ) -> dict[str, Any]:
        payload = {
            "user_message": raw_content,
            "context_summary": self.chat._history_safe_content(user_content)[:2500],
            "routed_user_request": decision.get("user_request") or raw_content,
            "use_previous": bool(decision.get("use_previous")),
            "previous_generation": (
                previous_generation.to_dict()
                if bool(decision.get("use_previous")) and previous_generation is not None
                else None
            ),
            "preset": preset.to_dict(),
            "active_artist_strings": [artist.to_dict() for artist in artist_strings],
            "character_profiles": [item.to_dict() for item in character_profiles],
            "default_model": self.nai.config.model,
        }
        data = await self._request_json_object(
            messages=[
                {"role": "system", "content": TAG_WRITER_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            client=self.chat.client,
            stage="tag_writer",
            temperature=0.35,
        )
        if not str(data.get("prompt") or "").strip():
            fallback_tags = []
            for character in character_profiles:
                fallback_tags.extend(character.prompt_tags)
            fallback_tags.append(raw_content)
            data["prompt"] = ", ".join(tag for tag in fallback_tags if tag)
        return data

    async def _request_json_object(
        self,
        messages: list[dict[str, object]],
        *,
        client: OpenAICompatibleClient,
        stage: str,
        temperature: float | None,
    ) -> dict[str, Any]:
        retry_count = int(getattr(self, "_structured_output_retry_count", 2))
        last_error: Exception | None = None
        for attempt in range(retry_count + 1):
            request_messages = messages
            if attempt > 0:
                request_messages = [
                    *messages,
                    {
                        "role": "system",
                        "content": (
                            "The prior attempt was not a complete valid JSON object. "
                            "Return exactly one complete JSON object matching the requested "
                            "schema, with double-quoted keys and strings, no markdown or prose."
                        ),
                    },
                ]
            try:
                raw = await client.create_chat_completion(
                    request_messages,
                    temperature=temperature,
                )
                return self._extract_json_object(raw)
            except Exception as exc:
                last_error = exc
                if attempt >= retry_count:
                    break
                print(
                    "[WARN] Draw structured stage will retry: "
                    f"stage={stage}, error_type={exc.__class__.__name__}, "
                    f"attempt={attempt + 1}/{retry_count + 1}"
                )
                await asyncio.sleep(min(0.6 * (attempt + 1), 2.0))

        raise RuntimeError(
            f"draw structured stage failed: stage={stage}"
        ) from last_error

    def _extract_json_object(self, raw: str) -> dict[str, Any]:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end >= start:
            cleaned = cleaned[start : end + 1]
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("model JSON is not an object")
        return data

    def _join_prompt_parts(self, *parts: str) -> str:
        seen: set[str] = set()
        output: list[str] = []
        for part in parts:
            for chunk in str(part or "").split(","):
                tag = " ".join(chunk.split())
                if not tag:
                    continue
                key = tag.casefold()
                if key in seen:
                    continue
                seen.add(key)
                output.append(tag)
        return ", ".join(output)

    async def _build_result_text(
        self,
        *,
        message: discord.Message,
        preset_name: str,
        artist_names: list[str],
        prompt: str,
        negative_prompt: str,
        params: dict[str, Any],
        model: str,
        character_profiles: list[CharacterProfile],
    ) -> str:
        seed = params.get("seed")
        if seed is None:
            seed = "random"
        searched = [
            profile.name
            for profile in character_profiles
            if profile.name and (profile.sources or profile.prompt_tags)
        ]
        fallback_intro = "画好了，给你。"
        intro = await self._persona_reply(
            message,
            event="draw_complete",
            facts={
                "preset_name": preset_name,
                "artist_names": artist_names,
                "model": model,
                "seed": seed,
                "searched_characters": searched,
            },
            fallback=fallback_intro,
        )
        lines = [intro]
        details = [f"预设 `{preset_name}`", f"模型 `{model}`", f"seed `{seed}`"]
        if artist_names:
            details.append(f"画师串 {', '.join(f'`{name}`' for name in artist_names)}")
        if searched:
            details.append(f"参考角色资料 {', '.join(searched)}")
        lines.append(f"-# {' | '.join(details)}")
        lines.append(f"-# prompt: {self._truncate(prompt, 900)}")
        if negative_prompt:
            lines.append(f"-# negative: {self._truncate(negative_prompt, 450)}")
        return self._truncate("\n".join(lines), 1900)

    async def _create_draw_status_message(
        self,
        message: discord.Message,
    ) -> discord.Message | None:
        try:
            return await message.reply(
                "-# 正在整理画面需求…",
                mention_author=False,
                allowed_mentions=self.chat._chat_allowed_mentions(),
            )
        except discord.HTTPException as exc:
            print(f"[WARN] Failed to create draw status message: {exc}")
            return None

    async def _edit_status_persona(
        self,
        status: discord.Message | None,
        message: discord.Message,
        *,
        event: str,
        facts: dict[str, Any],
        fallback: str,
        progress: DrawProgressCallback | None = None,
    ) -> None:
        if progress is not None:
            await progress(fallback)
            return
        if status is None:
            return
        await status.edit(
            content=await self._persona_reply(
                message,
                event=event,
                facts=facts,
                fallback=fallback,
            )
        )

    async def _finalize_status_with_images(
        self,
        *,
        status: discord.Message | None,
        message: discord.Message,
        content: str,
        files: list[discord.File],
    ) -> discord.Message:
        if status is None:
            return await message.reply(
                content,
                files=files,
                mention_author=False,
                allowed_mentions=self.chat._chat_allowed_mentions(),
            )
        try:
            await status.edit(
                content=content,
                attachments=[],
                files=files,
                allowed_mentions=self.chat._chat_allowed_mentions(),
            )
            return status
        except (TypeError, discord.HTTPException):
            try:
                await status.delete()
            except discord.HTTPException:
                pass
            return await message.reply(
                content,
                files=files,
                mention_author=False,
                allowed_mentions=self.chat._chat_allowed_mentions(),
            )

    async def _persona_reply(
        self,
        message: discord.Message,
        *,
        event: str,
        facts: dict[str, Any],
        fallback: str,
    ) -> str:
        reply_emojis = self.chat._reply_emojis_for_guild(message.guild)
        try:
            now = self.chat._current_local_time()
            payload = {
                "event": event,
                "facts": facts,
                "fallback_meaning": fallback,
                "user_message": self.chat._strip_bot_mention(message.content),
                "speaker": self.chat._format_user_header(
                    message.author.id,
                    message.author.display_name,
                ),
            }
            messages = [
                    {"role": "system", "content": self.chat.system_prompt},
                    {"role": "system", "content": self.chat.supplemental_prompt},
                    {"role": "system", "content": self.chat._build_runtime_context(now)},
                    {"role": "system", "content": DRAW_PERSONA_REPLY_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ]
            if reply_emojis:
                messages.insert(
                    4,
                    {
                        "role": "system",
                        "content": self.chat._build_reply_emoji_prompt(reply_emojis),
                    },
                )
            raw = await self.chat.client.create_chat_completion(
                messages,
                temperature=0.75,
            )
            reply = self.chat._decorate_reply_with_emojis(raw.strip(), reply_emojis)
            return self._truncate(reply or fallback, 500)
        except Exception as exc:
            print(f"[WARN] Failed to generate draw persona reply: {exc}")
            return self.chat._decorate_reply_with_emojis(fallback, reply_emojis)

    def _safe_image_filename(self, index: int, original_name: str) -> str:
        lowered = original_name.lower()
        if lowered.endswith(".png"):
            extension = "png"
        elif lowered.endswith((".jpg", ".jpeg")):
            extension = "jpg"
        else:
            extension = "webp"
        return f"atri_nai_{index}.{extension}"

    def _store_synthetic_assistant_turn(
        self,
        channel_id: int,
        content: str,
        created_at: Any,
    ) -> None:
        try:
            history = self.chat._get_synthetic_history(channel_id)
            history.append((created_at, {"role": "assistant", "content": content}))
        except Exception as exc:
            print(f"[WARN] Failed to store draw synthetic assistant turn: {exc}")

    def _text_after_colon(self, value: str) -> str:
        parts = re.split(r"[:：]", value, maxsplit=1)
        if len(parts) == 2:
            return parts[1].strip()
        return ""

    def _safe_draw_failure_text(self, exc: Exception, *, stage: str = "") -> str:
        probe = str(exc).casefold()
        if stage in {"tag_writer", "character_profiles"} or "structured stage" in probe:
            return "画面提示词整理连续失败了，这次没有调用 NAI，也没有消耗绘图次数；稍后再试一次吧。"
        if "certificate" in probe and ("verify" in probe or "expired" in probe):
            return "画图链路没接通：上游 HTTPS 证书校验失败。等上游更新证书或更换 API 地址后再试吧。"
        if "timeout" in probe or "timed out" in probe:
            return "画图链路这次超时了，稍后再试一次吧。"
        if "connect" in probe or "network" in probe or "connection" in probe:
            return "画图链路暂时连接不上上游服务，稍后再试一次吧。"
        if "rate" in probe and "limit" in probe:
            return "画图服务现在有点拥挤，稍后再试一次吧。"
        return "画图任务这次没能完成，我没有把上游的技术错误原文发到频道里。"

    def _truncate(self, value: str, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."
