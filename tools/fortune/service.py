from __future__ import annotations

import asyncio
import json
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from ...chat.client import OpenAICompatibleClient, OpenAICompatibleConfig
except ImportError:  # Top-level compatibility for local tests and scripts.
    from chat.client import OpenAICompatibleClient, OpenAICompatibleConfig

from .storage import FortuneRecord, FortuneStorage


try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
NOON_RESET_HOUR = 12
DEFAULT_FORTUNE_PROMPT = """你是亚托莉，会用带一点温柔机灵感的语气，为用户生成“今日运势”。

要求：
1. 用中文输出。
2. 风格要像中国风签文、卦象解读，文雅但易懂，不要现代互联网黑话。
3. 内容要像“每日运势”，温柔、有画面感，避免恐吓，避免医疗、法律、投资断言。
4. 适合 Discord 展示，句子不要太长。
5. 必须严格返回 JSON，不要加代码块，不要加额外说明。
6. 保留一点亚托莉式的认真、体贴和轻微可爱，但不要过分卖萌，也不要脱离“签文解读”的形式。

JSON 字段要求：
{
  "summary": "一句 12 到 28 字的总评",
  "luck_score": 0 到 100 的整数,
  "sign": "四字内的卦名或签名",
  "omen": "大吉|中吉|小吉|平|小凶 之一",
  "lucky_color": "1 到 4 个字",
  "lucky_direction": "1 到 4 个字",
  "lucky_time": "例如 巳时 / 11:00-13:00",
  "suitable": ["2 到 4 条宜做事项，每条 2 到 8 字"],
  "avoid": ["2 到 4 条忌做事项，每条 2 到 8 字"],
  "poem": "一句 8 到 20 字的签诗",
  "detail": "80 到 180 字的解签内容，分成 2 到 4 句，自然一些，像亚托莉在认真替人解签"
}
"""


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _pick_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


@dataclass(slots=True)
class FortuneTiming:
    period_id: str
    generated_for_date_text: str
    now_text: str
    reset_at: datetime
    reset_text: str


@dataclass(slots=True)
class FortuneResult:
    record: FortuneRecord
    from_cache: bool


class DailyFortuneService:
    def __init__(self, storage_path: Path) -> None:
        self.storage = FortuneStorage(storage_path)
        self._lock = asyncio.Lock()
        self.client = OpenAICompatibleClient(self._build_client_config())
        self.prompt = os.getenv("FORTUNE_SYSTEM_PROMPT", "").strip() or DEFAULT_FORTUNE_PROMPT

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def reload_client_from_env(self) -> None:
        """Follow the live main chat API after an admin-panel hot update."""
        self.client = OpenAICompatibleClient(self._build_client_config())

    def current_timing(self, now: datetime | None = None) -> FortuneTiming:
        current = now.astimezone(BEIJING_TZ) if now else datetime.now(BEIJING_TZ)
        reset_today = current.replace(
            hour=NOON_RESET_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        if current >= reset_today:
            period_date = current.date()
            next_reset = reset_today + timedelta(days=1)
        else:
            period_date = (current - timedelta(days=1)).date()
            next_reset = reset_today

        return FortuneTiming(
            period_id=period_date.isoformat(),
            generated_for_date_text=period_date.strftime("%Y年%m月%d日"),
            now_text=current.strftime("%Y-%m-%d %H:%M"),
            reset_at=next_reset,
            reset_text=next_reset.strftime("%Y-%m-%d %H:%M"),
        )

    async def get_or_create_fortune(
        self,
        *,
        user_id: int,
        display_name: str,
        timing: FortuneTiming | None = None,
    ) -> FortuneResult:
        timing = timing or self.current_timing()
        async with self._lock:
            records = self.storage.load_all()
            cached = records.get(user_id)
            if cached is not None and cached.period_id == timing.period_id:
                return FortuneResult(record=cached, from_cache=True)

            generated = await self._generate_fortune(
                user_id=user_id,
                display_name=display_name,
                timing=timing,
            )
            records[user_id] = generated
            self.storage.save_all(records)
            return FortuneResult(record=generated, from_cache=False)

    async def peek_fortune(
        self,
        *,
        user_id: int,
        timing: FortuneTiming | None = None,
    ) -> FortuneRecord | None:
        timing = timing or self.current_timing()
        async with self._lock:
            records = self.storage.load_all()
            cached = records.get(user_id)
            if cached is None or cached.period_id != timing.period_id:
                return None
            return cached

    async def _generate_fortune(
        self,
        *,
        user_id: int,
        display_name: str,
        timing: FortuneTiming,
    ) -> FortuneRecord:
        if not self.client.is_configured():
            raise RuntimeError("每日运势功能还没配好 AI，请先配置聊天主 OPENAI_* API。")

        seed = f"{user_id}:{timing.period_id}"
        user_prompt = "\n".join(
            [
                f"用户昵称：{display_name}",
                f"用户ID：{user_id}",
                f"运势日期：{timing.generated_for_date_text}",
                f"北京时间当前时间：{timing.now_text}",
                f"本次起卦种子：{seed}",
                "请按照系统要求生成一份带有东方签文气质的今日运势。",
            ]
        )
        messages = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": user_prompt},
        ]
        raw = await self.client.create_chat_completion(
            messages,
            temperature=_read_float("FORTUNE_TEMPERATURE", _read_float("OPENAI_TEMPERATURE", 0.9)),
        )
        payload = self._parse_response(raw)
        return FortuneRecord(
            user_id=user_id,
            period_id=timing.period_id,
            generated_at=datetime.now(UTC).isoformat(),
            reset_at=timing.reset_at.astimezone(UTC).isoformat(),
            summary=payload["summary"],
            luck_score=payload["luck_score"],
            sign=payload["sign"],
            omen=payload["omen"],
            lucky_color=payload["lucky_color"],
            lucky_direction=payload["lucky_direction"],
            lucky_time=payload["lucky_time"],
            suitable=payload["suitable"],
            avoid=payload["avoid"],
            poem=payload["poem"],
            detail=payload["detail"],
        )

    def _build_client_config(self) -> OpenAICompatibleConfig:
        return OpenAICompatibleConfig(
            base_url=_pick_env("OPENAI_BASE_URL"),
            api_key=_pick_env("OPENAI_API_KEY"),
            model=_pick_env("OPENAI_MODEL"),
            temperature=_read_float("FORTUNE_TEMPERATURE", _read_float("OPENAI_TEMPERATURE", 0.9)),
            timeout_seconds=_read_int("FORTUNE_OPENAI_TIMEOUT", _read_int("OPENAI_TIMEOUT", 60)),
            include_stream_usage=False,
            enable_web_search=False,
            retry_count=_read_int("FORTUNE_OPENAI_RETRY_COUNT", _read_int("OPENAI_RETRY_COUNT", 2)),
            retry_backoff_seconds=_read_float(
                "FORTUNE_OPENAI_RETRY_BACKOFF",
                _read_float("OPENAI_RETRY_BACKOFF", 1.5),
            ),
            user_agent=_pick_env("OPENAI_USER_AGENT") or "ATRI-DiscordBot/1.0",
            referer=_pick_env("OPENAI_HTTP_REFERER"),
            title=_pick_env("OPENAI_X_TITLE"),
        )

    def _parse_response(self, raw: str) -> dict[str, object]:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"AI 返回的运势结果不是有效 JSON：{raw[:160]}") from exc

        if not isinstance(data, dict):
            raise RuntimeError("AI 返回的运势结果格式不正确。")

        suitable = self._normalize_list(data.get("suitable"), fallback=["静心", "整顿"])
        avoid = self._normalize_list(data.get("avoid"), fallback=["躁进", "熬夜"])
        luck_score = self._normalize_score(data.get("luck_score"))

        return {
            "summary": self._normalize_text(data.get("summary"), "气数平稳，静守自有回响"),
            "luck_score": luck_score,
            "sign": self._normalize_text(data.get("sign"), "云水签", 6),
            "omen": self._normalize_omen(data.get("omen")),
            "lucky_color": self._normalize_text(data.get("lucky_color"), "青黛", 6),
            "lucky_direction": self._normalize_text(data.get("lucky_direction"), "东南", 6),
            "lucky_time": self._normalize_text(data.get("lucky_time"), "巳时", 16),
            "suitable": suitable,
            "avoid": avoid,
            "poem": self._normalize_text(data.get("poem"), "风来花动，心定自明", 24),
            "detail": self._normalize_text(
                data.get("detail"),
                "今日之势贵在收敛锋芒，先稳住手上的事，再顺着机缘慢慢推进。人与事不必强求，留一寸缓意，反而更容易见到转机。",
                220,
            ),
        }

    def _normalize_list(self, value: object, *, fallback: list[str]) -> list[str]:
        if not isinstance(value, list):
            return fallback
        items = [
            str(item).strip()[:8]
            for item in value
            if str(item).strip()
        ]
        return items[:4] or fallback

    def _normalize_score(self, value: object) -> int:
        try:
            score = int(value)
        except (TypeError, ValueError):
            score = random.randint(58, 92)
        return max(0, min(score, 100))

    def _normalize_text(self, value: object, fallback: str, max_length: int = 40) -> str:
        text = str(value).strip() if value is not None else ""
        if not text:
            return fallback
        compact = " ".join(text.split())
        return compact[:max_length] or fallback

    def _normalize_omen(self, value: object) -> str:
        omen = self._normalize_text(value, "平", 4)
        if omen in {"大吉", "中吉", "小吉", "平", "小凶"}:
            return omen
        return "平"
