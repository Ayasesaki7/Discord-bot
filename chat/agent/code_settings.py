from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class AgentCodeSettingsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentCodeSettings:
    enabled: bool
    base_url: str
    api_key: str
    model: str
    max_tokens: int | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.base_url.strip()
            and self.api_key.strip()
            and self.model.strip()
        )

    def as_runtime_env(self) -> dict[str, str]:
        values = {
            "ATRI_AGENT_CODE_BASE_URL": self.base_url,
            "ATRI_AGENT_CODE_API_KEY": self.api_key,
            "ATRI_AGENT_CODE_MODEL": self.model,
        }
        if self.max_tokens is not None:
            values["ATRI_AGENT_CODE_MAX_TOKENS"] = str(self.max_tokens)
        return values


class AgentCodeSettingsStore:
    """Protected, atomically updated settings for the maintenance runtime."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.path = (
            self.project_root
            / "config"
            / "credentials"
            / "agent_code_api.json"
        )

    def load(self) -> AgentCodeSettings:
        if not self.path.is_file():
            return self._from_environment()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentCodeSettingsError("maintenance settings file is invalid") from exc
        if not isinstance(payload, dict):
            raise AgentCodeSettingsError("maintenance settings must be a JSON object")
        settings = AgentCodeSettings(
            enabled=bool(payload.get("enabled", False)),
            base_url=str(payload.get("base_url") or "").strip(),
            api_key=str(payload.get("api_key") or "").strip(),
            model=str(payload.get("model") or "").strip(),
            max_tokens=self._optional_positive_int(payload.get("max_tokens")),
        )
        self.validate(settings)
        return settings

    def save(self, settings: AgentCodeSettings) -> None:
        self.validate(settings)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        payload = {
            "enabled": settings.enabled,
            "base_url": settings.base_url,
            "api_key": settings.api_key,
            "model": settings.model,
            "max_tokens": settings.max_tokens,
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def fingerprint(self) -> str:
        if not self.path.is_file():
            return "env:" + self._settings_digest(self._from_environment())
        try:
            raw = self.path.read_bytes()
        except OSError:
            return "unreadable"
        return "file:" + hashlib.sha256(raw).hexdigest()

    @classmethod
    def validate(cls, settings: AgentCodeSettings) -> None:
        for label, value, limit in (
            ("base URL", settings.base_url, 2048),
            ("API key", settings.api_key, 4096),
            ("model", settings.model, 300),
        ):
            if len(value) > limit or any(ord(char) < 32 for char in value):
                raise AgentCodeSettingsError(f"maintenance {label} is invalid")
        if settings.max_tokens is not None and settings.max_tokens <= 0:
            raise AgentCodeSettingsError("max tokens must be a positive integer")
        if settings.enabled:
            if not settings.base_url or not settings.api_key or not settings.model:
                raise AgentCodeSettingsError(
                    "enabled maintenance runtime requires base URL, API key, and model"
                )
            parsed = urlparse(settings.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise AgentCodeSettingsError(
                    "maintenance base URL must be an http(s) URL"
                )
            if parsed.username or parsed.password:
                raise AgentCodeSettingsError(
                    "maintenance base URL may not contain embedded credentials"
                )

    @staticmethod
    def _optional_positive_int(value: object) -> int | None:
        if value is None or value == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise AgentCodeSettingsError("max tokens must be a positive integer") from exc
        if parsed <= 0:
            raise AgentCodeSettingsError("max tokens must be a positive integer")
        return parsed

    @classmethod
    def _from_environment(cls) -> AgentCodeSettings:
        enabled = os.getenv("ATRI_AGENT_CODE_ENABLED", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        settings = AgentCodeSettings(
            enabled=enabled,
            base_url=os.getenv("ATRI_AGENT_CODE_BASE_URL", "").strip(),
            api_key=os.getenv("ATRI_AGENT_CODE_API_KEY", "").strip(),
            model=os.getenv("ATRI_AGENT_CODE_MODEL", "").strip(),
            max_tokens=cls._optional_positive_int(
                os.getenv("ATRI_AGENT_CODE_MAX_TOKENS", "").strip()
            ),
        )
        # The historical .env may intentionally be enabled before its three
        # values are filled. Preserve that intent for the local editor while
        # callers use ``configured`` to decide whether a runtime can start.
        return settings

    @staticmethod
    def _settings_digest(settings: AgentCodeSettings) -> str:
        raw = json.dumps(
            {
                "enabled": settings.enabled,
                "base_url": settings.base_url,
                "api_key": settings.api_key,
                "model": settings.model,
                "max_tokens": settings.max_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class WebSearchSettingsStore(AgentCodeSettingsStore):
    """Protected settings for the independent web-search model gateway."""

    def __init__(self, project_root: Path) -> None:
        super().__init__(project_root)
        self.path = (
            self.project_root
            / "config"
            / "credentials"
            / "web_search_api.json"
        )

    @classmethod
    def _from_environment(cls) -> AgentCodeSettings:
        enabled = os.getenv("ATRI_WEB_SEARCH_ENABLED", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return AgentCodeSettings(
            enabled=enabled,
            base_url=os.getenv("ATRI_WEB_SEARCH_BASE_URL", "").strip(),
            api_key=os.getenv("ATRI_WEB_SEARCH_API_KEY", "").strip(),
            model=os.getenv("ATRI_WEB_SEARCH_MODEL", "").strip(),
            max_tokens=cls._optional_positive_int(
                os.getenv("ATRI_WEB_SEARCH_MAX_TOKENS", "").strip()
            ),
        )
