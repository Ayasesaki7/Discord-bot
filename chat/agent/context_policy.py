from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


OverflowStrategy = Literal["compress", "drop_oldest"]

DEFAULT_CONTEXT_TOKEN_BUDGET = 80_000
MIN_CONTEXT_TOKEN_BUDGET = 8_000
MAX_CONTEXT_TOKEN_BUDGET = 80_000
MIN_HISTORY_MESSAGES = 1
MAX_HISTORY_MESSAGES = 1_000
PASSIVE_HISTORY_BATCH_VERSION = 1


@dataclass(frozen=True, slots=True)
class ChannelContextPolicy:
    history_messages: int = 300
    token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET
    overflow_strategy: OverflowStrategy = "compress"

    def __post_init__(self) -> None:
        if not MIN_HISTORY_MESSAGES <= self.history_messages <= MAX_HISTORY_MESSAGES:
            raise ValueError(
                f"history_messages must be between {MIN_HISTORY_MESSAGES} and "
                f"{MAX_HISTORY_MESSAGES}"
            )
        if not MIN_CONTEXT_TOKEN_BUDGET <= self.token_budget <= MAX_CONTEXT_TOKEN_BUDGET:
            raise ValueError(
                f"token_budget must be between {MIN_CONTEXT_TOKEN_BUDGET} and "
                f"{MAX_CONTEXT_TOKEN_BUDGET}"
            )
        if self.overflow_strategy not in {"compress", "drop_oldest"}:
            raise ValueError("overflow_strategy must be compress or drop_oldest")


@dataclass(frozen=True, slots=True)
class ChannelContextState:
    policy: ChannelContextPolicy
    last_input_tokens: int | None = None
    last_output_tokens: int | None = None
    imported_message_count: int = 0
    imported_through_message_id: int | None = None
    observed_through_message_id: int | None = None
    passive_history_initialized: bool = False
    passive_history_version: int = 0


class ChannelContextPolicyStore:
    """Small private runtime store keyed by an exact Discord guild/channel pair."""

    def __init__(
        self,
        project_root: Path,
        *,
        default_history_messages: int = 300,
        default_token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
    ) -> None:
        self.path = project_root / "chat" / "agent" / "data" / "channel_contexts.json"
        self.default_policy = ChannelContextPolicy(
            history_messages=default_history_messages,
            token_budget=default_token_budget,
        )
        self._lock = threading.Lock()
        self._states = self._load()

    @staticmethod
    def _key(guild_id: int, channel_id: int) -> str:
        if guild_id <= 0 or channel_id <= 0:
            raise ValueError("guild_id and channel_id must be positive Discord ids")
        return f"{guild_id}:{channel_id}"

    def get(self, guild_id: int, channel_id: int) -> ChannelContextState:
        key = self._key(guild_id, channel_id)
        with self._lock:
            return self._states.get(
                key,
                ChannelContextState(policy=self.default_policy),
            )

    def update_policy(
        self,
        guild_id: int,
        channel_id: int,
        *,
        history_messages: int | None = None,
        token_budget: int | None = None,
        overflow_strategy: OverflowStrategy | None = None,
    ) -> ChannelContextState:
        key = self._key(guild_id, channel_id)
        with self._lock:
            current = self._states.get(
                key,
                ChannelContextState(policy=self.default_policy),
            )
            policy = ChannelContextPolicy(
                history_messages=(
                    current.policy.history_messages
                    if history_messages is None
                    else history_messages
                ),
                token_budget=(
                    current.policy.token_budget if token_budget is None else token_budget
                ),
                overflow_strategy=(
                    current.policy.overflow_strategy
                    if overflow_strategy is None
                    else overflow_strategy
                ),
            )
            updated = ChannelContextState(
                policy=policy,
                last_input_tokens=current.last_input_tokens,
                last_output_tokens=current.last_output_tokens,
                imported_message_count=current.imported_message_count,
                imported_through_message_id=current.imported_through_message_id,
                observed_through_message_id=current.observed_through_message_id,
                passive_history_initialized=current.passive_history_initialized,
                passive_history_version=current.passive_history_version,
            )
            self._states[key] = updated
            self._save_locked()
            return updated

    def record_usage(
        self,
        guild_id: int,
        channel_id: int,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> ChannelContextState:
        key = self._key(guild_id, channel_id)
        with self._lock:
            current = self._states.get(
                key,
                ChannelContextState(policy=self.default_policy),
            )
            updated = ChannelContextState(
                policy=current.policy,
                last_input_tokens=_positive_or_none(input_tokens),
                last_output_tokens=_positive_or_none(output_tokens),
                imported_message_count=current.imported_message_count,
                imported_through_message_id=current.imported_through_message_id,
                observed_through_message_id=current.observed_through_message_id,
                passive_history_initialized=current.passive_history_initialized,
                passive_history_version=current.passive_history_version,
            )
            self._states[key] = updated
            self._save_locked()
            return updated

    def record_import(
        self,
        guild_id: int,
        channel_id: int,
        *,
        message_count: int,
        through_message_id: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> ChannelContextState:
        key = self._key(guild_id, channel_id)
        with self._lock:
            current = self._states.get(
                key,
                ChannelContextState(policy=self.default_policy),
            )
            updated = ChannelContextState(
                policy=current.policy,
                last_input_tokens=_positive_or_none(input_tokens),
                last_output_tokens=_positive_or_none(output_tokens),
                imported_message_count=max(message_count, 0),
                imported_through_message_id=_positive_or_none(through_message_id),
                observed_through_message_id=_newer_snowflake(
                    current.observed_through_message_id,
                    through_message_id,
                ),
                passive_history_initialized=True,
                passive_history_version=PASSIVE_HISTORY_BATCH_VERSION,
            )
            self._states[key] = updated
            self._save_locked()
            return updated

    def record_observed(
        self,
        guild_id: int,
        channel_id: int,
        *,
        through_message_id: int,
        passive_history_initialized: bool | None = None,
        passive_history_version: int | None = None,
    ) -> ChannelContextState:
        """Advance the durable Discord-history watermark for one channel."""

        key = self._key(guild_id, channel_id)
        with self._lock:
            current = self._states.get(
                key,
                ChannelContextState(policy=self.default_policy),
            )
            updated = ChannelContextState(
                policy=current.policy,
                last_input_tokens=current.last_input_tokens,
                last_output_tokens=current.last_output_tokens,
                imported_message_count=current.imported_message_count,
                imported_through_message_id=current.imported_through_message_id,
                observed_through_message_id=_newer_snowflake(
                    current.observed_through_message_id,
                    through_message_id,
                ),
                passive_history_initialized=(
                    current.passive_history_initialized
                    if passive_history_initialized is None
                    else bool(passive_history_initialized)
                ),
                passive_history_version=(
                    current.passive_history_version
                    if passive_history_version is None
                    else max(int(passive_history_version), 0)
                ),
            )
            self._states[key] = updated
            self._save_locked()
            return updated

    def clear_runtime_usage(self, guild_id: int, channel_id: int) -> None:
        key = self._key(guild_id, channel_id)
        with self._lock:
            current = self._states.get(key)
            if current is None:
                return
            self._states[key] = ChannelContextState(policy=current.policy)
            self._save_locked()

    def _load(self) -> dict[str, ChannelContextState]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw_channels = payload.get("channels") if isinstance(payload, dict) else None
        if not isinstance(raw_channels, dict):
            return {}

        states: dict[str, ChannelContextState] = {}
        for key, raw in raw_channels.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                continue
            try:
                guild_raw, channel_raw = key.split(":", 1)
                self._key(int(guild_raw), int(channel_raw))
                policy = ChannelContextPolicy(
                    history_messages=int(raw.get("history_messages", self.default_policy.history_messages)),
                    token_budget=int(raw.get("token_budget", self.default_policy.token_budget)),
                    overflow_strategy=str(raw.get("overflow_strategy", "compress")),
                )
            except (TypeError, ValueError):
                continue
            states[key] = ChannelContextState(
                policy=policy,
                last_input_tokens=_positive_or_none(raw.get("last_input_tokens")),
                last_output_tokens=_positive_or_none(raw.get("last_output_tokens")),
                imported_message_count=max(_int_or_zero(raw.get("imported_message_count")), 0),
                imported_through_message_id=_positive_or_none(
                    raw.get("imported_through_message_id")
                ),
                observed_through_message_id=_positive_or_none(
                    raw.get("observed_through_message_id")
                ),
                passive_history_initialized=(
                    raw.get("passive_history_initialized") is True
                ),
                passive_history_version=max(
                    _int_or_zero(raw.get("passive_history_version")),
                    0,
                ),
            )
        return states

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "channels": {
                key: {
                    **asdict(state.policy),
                    "last_input_tokens": state.last_input_tokens,
                    "last_output_tokens": state.last_output_tokens,
                    "imported_message_count": state.imported_message_count,
                    "imported_through_message_id": state.imported_through_message_id,
                    "observed_through_message_id": state.observed_through_message_id,
                    "passive_history_initialized": state.passive_history_initialized,
                    "passive_history_version": state.passive_history_version,
                }
                for key, state in sorted(self._states.items())
            },
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def _positive_or_none(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _int_or_zero(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _newer_snowflake(current: object, candidate: object) -> int | None:
    current_id = _positive_or_none(current)
    candidate_id = _positive_or_none(candidate)
    if current_id is None:
        return candidate_id
    if candidate_id is None:
        return current_id
    return max(current_id, candidate_id)
