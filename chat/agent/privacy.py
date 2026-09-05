from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


ScopeKind = Literal["guild", "dm"]
_SAFE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{15,80}$")
_SENSITIVE_DEBUG_KEY_PARTS = (
    "argument",
    "body",
    "content",
    "excerpt",
    "error",
    "input",
    "message",
    "output",
    "payload",
    "prompt",
    "response",
    "text",
)


class CrossTenantAccessError(PermissionError):
    """Raised when data or a reply target crosses its bound Discord scope."""


@dataclass(frozen=True, slots=True)
class ConversationScope:
    """Immutable Discord origin used to derive an agent tenant and session.

    Guild conversations are tenant-scoped by guild and session-scoped by the
    concrete channel/thread. Direct messages use the Discord user as tenant so
    one user's private conversation can never share a namespace with another.
    """

    kind: ScopeKind
    guild_id: int | None
    channel_id: int
    user_id: int

    @classmethod
    def from_discord_ids(
        cls,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> "ConversationScope":
        cls._require_snowflake("channel_id", channel_id)
        cls._require_snowflake("user_id", user_id)
        if guild_id is None:
            return cls(kind="dm", guild_id=None, channel_id=channel_id, user_id=user_id)
        cls._require_snowflake("guild_id", guild_id)
        return cls(kind="guild", guild_id=guild_id, channel_id=channel_id, user_id=user_id)

    @staticmethod
    def _require_snowflake(name: str, value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer Discord id")

    @property
    def tenant_material(self) -> str:
        if self.kind == "guild":
            return f"discord:guild:{self.guild_id}"
        return f"discord:dm-user:{self.user_id}"

    @property
    def session_material(self) -> str:
        return f"{self.tenant_material}:channel:{self.channel_id}"


@dataclass(frozen=True, slots=True)
class ScopedSessionIdentity:
    tenant_key: str
    session_key: str

    def __post_init__(self) -> None:
        if not _SAFE_KEY_RE.fullmatch(self.tenant_key):
            raise ValueError("invalid tenant key")
        if not _SAFE_KEY_RE.fullmatch(self.session_key):
            raise ValueError("invalid session key")


@dataclass(frozen=True, slots=True)
class ReplyBinding:
    """A destination chosen by Discord, never by model-generated arguments."""

    guild_id: int | None
    channel_id: int

    def require_destination(self, *, guild_id: int | None, channel_id: int) -> None:
        if guild_id != self.guild_id or channel_id != self.channel_id:
            raise CrossTenantAccessError(
                "agent reply destination does not match the originating Discord conversation"
            )


@dataclass(frozen=True, slots=True)
class AgentRequestContext:
    scope: ConversationScope
    identity: ScopedSessionIdentity
    reply: ReplyBinding


class PrivacyBoundary:
    """Derives opaque namespaces and validates all local session paths."""

    def __init__(self, *, secret: bytes, session_root: Path) -> None:
        if len(secret) < 32:
            raise ValueError("agent session secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self.session_root = session_root.resolve()
        self._revision_path = self.session_root / "_session-revisions.json"
        self._revision_lock = threading.Lock()
        self._session_revisions = self._load_session_revisions()

    @classmethod
    def from_env(cls) -> "PrivacyBoundary":
        configured_root = os.getenv("ATRI_AGENT_SESSION_ROOT", "").strip()
        if configured_root:
            session_root = Path(configured_root).expanduser()
        else:
            local_app_data = os.getenv("LOCALAPPDATA", "").strip()
            if not local_app_data:
                raise RuntimeError(
                    "ATRI_AGENT_SESSION_ROOT is required when LOCALAPPDATA is unavailable"
                )
            session_root = Path(local_app_data) / "ATRI" / "agent-sessions"

        raw_secret = os.getenv("ATRI_AGENT_SESSION_SECRET", "")
        if raw_secret:
            secret = raw_secret.encode("utf-8")
            if len(secret) < 32:
                raise RuntimeError(
                    "ATRI_AGENT_SESSION_SECRET must contain at least 32 bytes when configured"
                )
        else:
            secret = _load_or_create_local_secret(session_root.parent / "agent-session.key")
        return cls(secret=secret, session_root=session_root)

    def bind(self, scope: ConversationScope) -> AgentRequestContext:
        base_session_key = self._opaque_key("session", scope.session_material)
        revision = self._session_revisions.get(base_session_key, 0)
        identity = ScopedSessionIdentity(
            tenant_key=self._opaque_key("tenant", scope.tenant_material),
            session_key=self._revised_session_key(base_session_key, revision),
        )
        return AgentRequestContext(
            scope=scope,
            identity=identity,
            reply=ReplyBinding(guild_id=scope.guild_id, channel_id=scope.channel_id),
        )

    def rotate_session(self, scope: ConversationScope) -> AgentRequestContext:
        """Start a fresh durable conversation without making the old log reachable.

        dsh's JSON-RPC protocol has no per-session delete operation. Rotating the
        opaque identifier is therefore the safe reset primitive: old data remains
        tenant-scoped on disk, while every future turn uses a new session id.
        """

        base_session_key = self._opaque_key("session", scope.session_material)
        with self._revision_lock:
            revision = self._session_revisions.get(base_session_key, 0) + 1
            self._session_revisions[base_session_key] = revision
            self._save_session_revisions()
        return self.bind(scope)

    def require_same_session(
        self,
        expected: AgentRequestContext,
        candidate: AgentRequestContext,
    ) -> None:
        same_tenant = hmac.compare_digest(
            expected.identity.tenant_key,
            candidate.identity.tenant_key,
        )
        same_session = hmac.compare_digest(
            expected.identity.session_key,
            candidate.identity.session_key,
        )
        if not same_tenant or not same_session:
            raise CrossTenantAccessError("agent session access crossed its bound conversation")

    def session_path(self, context: AgentRequestContext) -> Path:
        candidate = (
            self.session_root
            / context.identity.tenant_key
            / context.identity.session_key
        ).resolve()
        try:
            candidate.relative_to(self.session_root)
        except ValueError as exc:
            raise CrossTenantAccessError("agent session path escaped its storage root") from exc
        return candidate

    def _opaque_key(self, label: str, material: str) -> str:
        digest = hmac.new(
            self._secret,
            f"atri-agent-v1:{label}:{material}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{label[0]}_{digest[:40]}"

    def _revised_session_key(self, base_session_key: str, revision: int) -> str:
        if revision <= 0:
            return base_session_key
        return self._opaque_key("session", f"{base_session_key}:revision:{revision}")

    def _load_session_revisions(self) -> dict[str, int]:
        if not self._revision_path.is_file():
            return {}
        try:
            payload = json.loads(self._revision_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("agent session revision state is unreadable") from exc
        revisions = payload.get("revisions") if isinstance(payload, dict) else None
        if not isinstance(revisions, dict):
            raise RuntimeError("agent session revision state is invalid")
        result: dict[str, int] = {}
        for key, value in revisions.items():
            if not isinstance(key, str) or not _SAFE_KEY_RE.fullmatch(key):
                raise RuntimeError("agent session revision state contains an invalid key")
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise RuntimeError("agent session revision state contains an invalid revision")
            result[key] = value
        return result

    def _save_session_revisions(self) -> None:
        self.session_root.mkdir(parents=True, exist_ok=True)
        temporary_path = self._revision_path.with_suffix(".tmp")
        payload = {
            "version": 1,
            "revisions": dict(sorted(self._session_revisions.items())),
        }
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, self._revision_path)


def sensitive_content_logging_enabled() -> bool:
    raw = os.getenv("ATRI_CHAT_LOG_SENSITIVE_CONTENT", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def build_sensitive_content_metadata(content: str) -> dict[str, object]:
    """Return useful failure diagnostics without placing message text in logs."""

    encoded = content.encode("utf-8", errors="replace")
    return {
        "characters": len(content),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest()[:16],
    }


def build_safe_debug_snapshot(debug: dict[str, object] | None) -> dict[str, object]:
    """Remove provider payloads and text-like fields from upstream diagnostics."""

    if not debug:
        return {}
    sanitized = _sanitize_debug_value(debug)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_debug_value(value: Any, *, key: str = "") -> Any:
    normalized_key = key.casefold()
    if any(part in normalized_key for part in _SENSITIVE_DEBUG_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(child_key): _sanitize_debug_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_debug_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= 200 and not _looks_like_json_or_multiline(value):
            return value
        return "[redacted]"
    return "[redacted]"


def _looks_like_json_or_multiline(value: str) -> bool:
    stripped = value.strip()
    if "\n" in value or "\r" in value:
        return True
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            return False
        return True
    return False


def _load_or_create_local_secret(path: Path) -> bytes:
    """Persist a machine-local namespace key without placing it in the repo."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        encoded = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        encoded = ""
    except OSError as exc:
        raise RuntimeError("agent session key could not be read") from exc
    if encoded:
        try:
            secret = bytes.fromhex(encoded)
        except ValueError as exc:
            raise RuntimeError("agent session key file is invalid") from exc
        if len(secret) != 32:
            raise RuntimeError("agent session key file is invalid")
        return secret

    secret = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            secret = bytes.fromhex(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as exc:
            raise RuntimeError("agent session key could not be loaded") from exc
        if len(secret) != 32:
            raise RuntimeError("agent session key file is invalid")
        return secret
    except OSError as exc:
        raise RuntimeError("agent session key could not be created") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(secret.hex() + "\n")
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return secret
