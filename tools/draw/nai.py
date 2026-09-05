from __future__ import annotations

import asyncio
import json
import os
import random
import string
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from typing import Any

import aiohttp


@dataclass(slots=True)
class GeneratedImage:
    filename: str
    data: bytes
    mime_type: str


@dataclass(slots=True)
class NovelAIConfig:
    api_base_url: str = "https://image.novelai.net"
    api_token: str = ""
    model: str = "nai-diffusion-4-5-full"
    timeout_seconds: int = 180
    width: int = 832
    height: int = 1216
    n_samples: int = 1
    steps: int = 28
    scale: float = 6.0
    cfg_rescale: float = 0.0
    sampler: str = "k_euler_ancestral"
    image_format: str = "webp"
    quality_toggle: bool = True
    uc_preset: int = 0
    rate_limit_retry_count: int = 4
    rate_limit_base_delay_seconds: float = 15.0
    rate_limit_max_delay_seconds: float = 180.0
    extra_parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "NovelAIConfig":
        return cls(
            api_base_url=os.getenv("NAI_IMAGE_API_BASE_URL", "https://image.novelai.net").strip().rstrip("/")
            or "https://image.novelai.net",
            api_token=os.getenv("NAI_API_TOKEN", "").strip(),
            model=os.getenv("NAI_IMAGE_MODEL", "nai-diffusion-4-5-full").strip()
            or "nai-diffusion-4-5-full",
            timeout_seconds=max(30, _read_int("NAI_IMAGE_TIMEOUT", 180)),
            width=_round_to_multiple(_read_int("NAI_IMAGE_WIDTH", 832), 64),
            height=_round_to_multiple(_read_int("NAI_IMAGE_HEIGHT", 1216), 64),
            n_samples=max(1, min(_read_int("NAI_IMAGE_SAMPLES", 1), 4)),
            steps=max(1, min(_read_int("NAI_IMAGE_STEPS", 28), 50)),
            scale=max(1.0, min(_read_float("NAI_IMAGE_SCALE", 6.0), 20.0)),
            cfg_rescale=max(0.0, min(_read_float("NAI_IMAGE_CFG_RESCALE", 0.0), 1.0)),
            sampler=os.getenv("NAI_IMAGE_SAMPLER", "k_euler_ancestral").strip()
            or "k_euler_ancestral",
            image_format=_read_choice("NAI_IMAGE_FORMAT", {"webp", "png"}, "webp"),
            quality_toggle=_read_bool("NAI_IMAGE_QUALITY_TOGGLE", True),
            uc_preset=max(0, _read_int("NAI_IMAGE_UC_PRESET", 0)),
            rate_limit_retry_count=max(0, _read_int("NAI_IMAGE_429_RETRY_COUNT", 4)),
            rate_limit_base_delay_seconds=max(
                1.0,
                _read_float("NAI_IMAGE_429_BASE_DELAY_SECONDS", 15.0),
            ),
            rate_limit_max_delay_seconds=max(
                1.0,
                _read_float("NAI_IMAGE_429_MAX_DELAY_SECONDS", 180.0),
            ),
            extra_parameters=_read_json_object("NAI_IMAGE_EXTRA_PARAMETERS_JSON"),
        )


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _read_choice(name: str, allowed: set[str], default: str) -> str:
    raw = os.getenv(name, "").strip().lower()
    if raw in allowed:
        return raw
    return default


def _read_json_object(name: str) -> dict[str, Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _round_to_multiple(value: int, multiple: int) -> int:
    return max(multiple, round(value / multiple) * multiple)


class NovelAIImageClient:
    def __init__(self, config: NovelAIConfig | None = None) -> None:
        self.config = config or NovelAIConfig.from_env()

    def is_configured(self) -> bool:
        return bool(self.config.api_token)

    async def generate_image(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        params: dict[str, Any] | None = None,
        on_rate_limit_retry: Callable[[int, float], Awaitable[None]] | None = None,
    ) -> tuple[list[GeneratedImage], dict[str, Any]]:
        if not self.is_configured():
            raise RuntimeError("Missing NAI_API_TOKEN.")

        payload = self._build_payload(
            prompt=prompt,
            negative_prompt=negative_prompt,
            params=params or {},
        )
        headers = {
            "Accept": "application/zip, application/json",
            "Content-Type": "application/json",
            "Authorization": self._authorization_header(),
            "x-correlation-id": _correlation_id(),
        }
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            images: list[GeneratedImage] = []
            for attempt_index in range(self.config.rate_limit_retry_count + 1):
                async with session.post(
                    f"{self.config.api_base_url}/ai/generate-image",
                    json=payload,
                ) as response:
                    raw = await response.read()
                    content_type = response.headers.get("Content-Type", "")
                    if (
                        response.status == 429
                        and attempt_index < self.config.rate_limit_retry_count
                    ):
                        delay = self._rate_limit_delay(response, attempt_index)
                        print(
                            "[WARN] NovelAI rate limited the image request; "
                            f"retrying in {delay:.1f}s "
                            f"(attempt {attempt_index + 1}/"
                            f"{self.config.rate_limit_retry_count})."
                        )
                        if on_rate_limit_retry is not None:
                            await on_rate_limit_retry(attempt_index + 1, delay)
                        await asyncio.sleep(delay)
                        continue
                    if response.status >= 400:
                        raise RuntimeError(self._format_error(response.status, raw, content_type))
                    images = self._extract_images(raw, content_type)
                    break
        if not images:
            raise RuntimeError("NovelAI returned no images.")
        debug_payload = {
            "model": payload.get("model"),
            "parameters": payload.get("parameters", {}),
        }
        return images, debug_payload

    def _authorization_header(self) -> str:
        token = self.config.api_token.strip()
        if token.lower().startswith(("bearer ", "token ")):
            return token
        return f"Bearer {token}"

    def _build_payload(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        params = dict(params)
        model = str(params.pop("model", self.config.model) or self.config.model)
        parameters: dict[str, Any] = {
            "params_version": 3 if "-4" in model else 1,
            "width": self.config.width,
            "height": self.config.height,
            "scale": self.config.scale,
            "sampler": self.config.sampler,
            "steps": self.config.steps,
            "n_samples": self.config.n_samples,
            "ucPreset": self.config.uc_preset,
            "qualityToggle": self.config.quality_toggle,
            "cfg_rescale": self.config.cfg_rescale,
            "image_format": self.config.image_format,
            "negative_prompt": negative_prompt,
        }
        parameters.update(self.config.extra_parameters)
        parameters.update({key: value for key, value in params.items() if value is not None})
        if "-4" in model:
            parameters.setdefault(
                "v4_prompt",
                {
                    "caption": {"base_caption": prompt, "char_captions": []},
                    "use_coords": False,
                    "use_order": True,
                },
            )
            parameters.setdefault(
                "v4_negative_prompt",
                {
                    "caption": {"base_caption": negative_prompt, "char_captions": []},
                    "legacy_uc": False,
                },
            )
        return {
            "action": "generate",
            "input": prompt,
            "model": model,
            "parameters": parameters,
        }

    def _extract_images(self, raw: bytes, content_type: str) -> list[GeneratedImage]:
        if "application/zip" in content_type or zipfile.is_zipfile(BytesIO(raw)):
            return self._extract_zip_images(raw)
        if raw.startswith(b"\x89PNG"):
            return [GeneratedImage("novelai.png", raw, "image/png")]
        if raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
            return [GeneratedImage("novelai.webp", raw, "image/webp")]
        return []

    def _extract_zip_images(self, raw: bytes) -> list[GeneratedImage]:
        images: list[GeneratedImage] = []
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            for name in archive.namelist():
                lowered = name.lower()
                if not lowered.endswith((".png", ".webp", ".jpg", ".jpeg")):
                    continue
                data = archive.read(name)
                if not data:
                    continue
                mime_type = "image/webp" if lowered.endswith(".webp") else "image/png"
                if lowered.endswith((".jpg", ".jpeg")):
                    mime_type = "image/jpeg"
                images.append(GeneratedImage(name.rsplit("/", 1)[-1], data, mime_type))
        return images

    def _format_error(self, status: int, raw: bytes, content_type: str) -> str:
        text = raw.decode("utf-8", errors="replace")
        if "application/json" in content_type:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error") or text
                return f"NovelAI API returned HTTP {status}: {message}"
        compact = " ".join(text.split())
        return f"NovelAI API returned HTTP {status}: {compact[:300]}"

    def _rate_limit_delay(
        self,
        response: aiohttp.ClientResponse,
        attempt_index: int,
    ) -> float:
        retry_after = self._retry_after_header_seconds(response)
        if retry_after is None:
            retry_after = self.config.rate_limit_base_delay_seconds * (2 ** attempt_index)
            retry_after += random.uniform(0.0, min(3.0, retry_after * 0.15))
        return min(max(retry_after, 1.0), self.config.rate_limit_max_delay_seconds)

    def _retry_after_header_seconds(
        self,
        response: aiohttp.ClientResponse,
    ) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        stripped = raw.strip()
        try:
            return max(float(stripped), 0.0)
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max((target - datetime.now(timezone.utc)).total_seconds(), 0.0)


def _correlation_id() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(6))
