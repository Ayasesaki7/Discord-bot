from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .privacy import AgentRequestContext, CrossTenantAccessError, PrivacyBoundary


TextDeltaCallback = Callable[[str], Awaitable[None] | None]
EventCallback = Callable[[dict[str, object]], Awaitable[None] | None]

DEFAULT_DSH_STDIO_LIMIT_BYTES = 8 * 1024 * 1024
MIN_DSH_STDIO_LIMIT_BYTES = 256 * 1024
MAX_DSH_STDIO_LIMIT_BYTES = 32 * 1024 * 1024


class DshRuntimeError(RuntimeError):
    pass


class DshTurnFailedError(DshRuntimeError):
    """A completed DSH turn whose model/provider reported a structured failure."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = (code or "").strip().upper() or None


_DSH_ERROR_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)(?P<prefix>\b(?:api[_ -]?key|authorization|cookie|password|secret|token)\b"
        r"\s*[:=]\s*)(?P<quote>['\"]?)[^'\"\s,;}]{6,}(?P=quote)"
    ),
    re.compile(
        r"(?i)(?P<prefix>[?&](?:api[_-]?key|access[_-]?token|token|key)=)"
        r"[^&\s]{6,}"
    ),
)


def describe_dsh_error(exc: BaseException, *, max_chars: int = 1000) -> str:
    """Return one useful log line without leaking common credential formats."""

    compact = " ".join(str(exc).split()) or exc.__class__.__name__
    for pattern in _DSH_ERROR_SECRET_PATTERNS:
        if "prefix" in pattern.groupindex:
            compact = pattern.sub(
                lambda match: f"{match.group('prefix')}[redacted-secret]",
                compact,
            )
        else:
            compact = pattern.sub("[redacted-secret]", compact)
    if max_chars <= 0:
        return ""
    if len(compact) > max_chars:
        compact = compact[: max(max_chars - 3, 0)] + ("..." if max_chars >= 3 else "")
    return compact


@dataclass(frozen=True, slots=True)
class DshRuntimeTemplate:
    launch_args: tuple[str, ...]
    cordis_path: Path
    provider: str
    model: str
    persona: str
    request_timeout_seconds: float = 180.0
    max_tokens: int | None = None
    stdio_limit_bytes: int = DEFAULT_DSH_STDIO_LIMIT_BYTES
    extra_env: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_project_env(
        cls,
        *,
        project_root: Path,
        persona: str,
        cordis_filename: str = "cordis.yml",
        request_timeout_env: str = "ATRI_DSH_REQUEST_TIMEOUT",
        default_request_timeout_seconds: float = 600.0,
        api_env_prefix: str = "OPENAI",
        provider_env: str = "ATRI_DSH_PROVIDER",
        max_tokens_env: str = "ATRI_DSH_MAX_TOKENS",
        env_overrides: dict[str, str] | None = None,
    ) -> "DshRuntimeTemplate":
        runtime_root = project_root / "agent_runtime" / "dsh"
        if Path(cordis_filename).name != cordis_filename:
            raise DshRuntimeError("dsh config filename must not contain a path")
        cordis_path = runtime_root / cordis_filename
        bin_path = (
            runtime_root
            / "node_modules"
            / "@deepseek-ai"
            / "dsh-sdk-jsonrpc-demo"
            / "lib"
            / "bin.js"
        )
        node = os.getenv("ATRI_DSH_NODE", "").strip() or shutil.which("node")
        if not node:
            raise DshRuntimeError("Node.js 22.19 or newer is required for DeepSeek Harness")
        if not cordis_path.is_file():
            raise DshRuntimeError(f"DeepSeek Harness config is missing: {cordis_path}")
        if not bin_path.is_file():
            raise DshRuntimeError(
                "DeepSeek Harness runtime is not installed; run npm install in "
                f"{runtime_root}"
            )

        effective_env = os.environ.copy()
        safe_overrides = {
            str(key): str(value)
            for key, value in (env_overrides or {}).items()
            if str(key) and value is not None
        }
        effective_env.update(safe_overrides)

        normalized_prefix = api_env_prefix.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized_prefix):
            raise DshRuntimeError("invalid dsh API environment prefix")
        model_env = f"{normalized_prefix}_MODEL"
        base_url_env = f"{normalized_prefix}_BASE_URL"
        api_key_env = f"{normalized_prefix}_API_KEY"
        model = effective_env.get(model_env, "").strip()
        if not model:
            raise DshRuntimeError(f"{model_env} is required for the dsh runtime")
        base_url = effective_env.get(base_url_env, "").strip()
        api_key = effective_env.get(api_key_env, "").strip()
        if not base_url or not api_key:
            raise DshRuntimeError(
                f"{base_url_env} and {api_key_env} are required for dsh"
            )

        timeout = _parse_positive_float(
            effective_env.get(request_timeout_env),
            default_request_timeout_seconds,
        )
        max_tokens = _parse_optional_positive_int(effective_env.get(max_tokens_env))
        stdio_limit_bytes = _parse_bounded_int(
            effective_env.get("ATRI_DSH_STDIO_LIMIT_BYTES"),
            DEFAULT_DSH_STDIO_LIMIT_BYTES,
            MIN_DSH_STDIO_LIMIT_BYTES,
            MAX_DSH_STDIO_LIMIT_BYTES,
        )
        return cls(
            launch_args=(str(node), str(bin_path), str(cordis_path)),
            cordis_path=cordis_path,
            provider=effective_env.get(provider_env, "").strip() or "atri-gateway",
            model=model,
            persona=persona,
            request_timeout_seconds=timeout,
            max_tokens=max_tokens,
            stdio_limit_bytes=stdio_limit_bytes,
            extra_env=safe_overrides,
        )


@dataclass(frozen=True, slots=True)
class DshTurnResult:
    session_id: str
    final_response: str
    finish_reason: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class DshCompactionResult:
    session_id: str
    compacted: bool
    shadowed_items: int = 0
    shadowed_tokens: int = 0
    summary_seq: int | None = None


@dataclass(frozen=True, slots=True)
class DshSessionSnapshot:
    byte_size: int = 0
    latest_compaction_summary: str = ""
    post_compaction_delta: str = ""
    post_compaction_event_count: int = 0
    post_compaction_dropped_count: int = 0
    latest_input_tokens: int | None = None
    latest_turn_error_code: str | None = None
    latest_turn_error_message: str = ""


@dataclass(slots=True)
class _ActiveTurn:
    on_text_delta: TextDeltaCallback | None
    on_event: EventCallback | None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    saw_running: bool = False
    final_response: str = ""
    finish_reason: str | None = None
    text_chunks: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    saw_input_usage: bool = False
    saw_output_usage: bool = False
    protocol_error: DshRuntimeError | None = None
    turn_error: DshTurnFailedError | None = None


class DshJsonRpcProcess:
    """One long-running dsh process bound to exactly one Discord tenant."""

    def __init__(
        self,
        *,
        template: DshRuntimeTemplate,
        tenant_key: str,
        session_root: Path,
        runtime_env: dict[str, str] | None = None,
    ) -> None:
        self.template = template
        self.tenant_key = tenant_key
        self.session_root = session_root.resolve()
        self.runtime_cwd = self.session_root / "runtime"
        self.runtime_env = dict(runtime_env or {})
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._active_turns: dict[str, _ActiveTurn] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._next_request_id = 1
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._closed = False

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        async with self._start_lock:
            if self.is_running:
                return
            if self._closed:
                raise DshRuntimeError("dsh tenant runtime is closed")

            self.session_root.mkdir(parents=True, exist_ok=True)
            self.runtime_cwd.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(self.template.extra_env)
            env.update(self.runtime_env)
            env["DSH_SESSION_ROOT"] = str(self.session_root)
            env["ATRI_DSH_SYSTEM_PROMPT"] = self.template.persona
            self._stderr_tail.clear()

            creationflags = 0x08000000 if os.name == "nt" else 0
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.template.launch_args,
                    cwd=str(self.runtime_cwd),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.template.stdio_limit_bytes,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise DshRuntimeError(f"failed to start dsh runtime: {exc}") from exc

            self._reader_task = asyncio.create_task(self._read_stdout(), name=f"dsh:{self.tenant_key}:stdout")
            self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"dsh:{self.tenant_key}:stderr")
            try:
                await self._request(
                    "initialize",
                    {
                        "cwd": str(self.runtime_cwd),
                        "provider": self.template.provider,
                        "model": self.template.model,
                        **(
                            {"maxTokens": self.template.max_tokens}
                            if self.template.max_tokens is not None
                            else {}
                        ),
                    },
                    timeout=self.template.request_timeout_seconds,
                )
            except Exception:
                await self._terminate()
                raise

    async def run_turn(
        self,
        *,
        session_id: str,
        text: str,
        on_text_delta: TextDeltaCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> DshTurnResult:
        if not text.strip():
            raise ValueError("dsh prompt text cannot be empty")
        await self.start()
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if session_id in self._active_turns:
                raise DshRuntimeError(f"session already has an active turn: {session_id}")
            active = _ActiveTurn(on_text_delta=on_text_delta, on_event=on_event)
            self._active_turns[session_id] = active
            try:
                await self._request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "contentBlocks": [{"type": "text", "text": text}],
                    },
                    timeout=self.template.request_timeout_seconds,
                )
                await asyncio.wait_for(
                    active.done.wait(),
                    timeout=self.template.request_timeout_seconds,
                )
            except TimeoutError as exc:
                raise DshRuntimeError(
                    f"dsh turn timed out after {self.template.request_timeout_seconds:.0f}s"
                ) from exc
            finally:
                self._active_turns.pop(session_id, None)

            if active.protocol_error is not None:
                raise active.protocol_error
            if active.turn_error is not None:
                raise active.turn_error
            final_response = active.final_response.strip() or "".join(active.text_chunks).strip()
            if not final_response:
                finish_detail = (
                    f"; finish_reason={active.finish_reason}"
                    if active.finish_reason
                    else ""
                )
                raise DshRuntimeError(
                    "dsh completed the turn without an assistant response"
                    f"{finish_detail}"
                )
            return DshTurnResult(
                session_id=session_id,
                final_response=final_response,
                finish_reason=active.finish_reason,
                input_tokens=active.input_tokens if active.saw_input_usage else None,
                output_tokens=active.output_tokens if active.saw_output_usage else None,
            )

    async def inject_context(
        self,
        *,
        session_id: str,
        text: str,
    ) -> int:
        """Append durable model-facing history without waking the model."""

        if not text.strip():
            raise ValueError("dsh injected context cannot be empty")
        await self.start()
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            result = await self._request(
                "session/inject",
                {
                    "sessionId": session_id,
                    "contentBlocks": [{"type": "text", "text": text}],
                },
                timeout=self.template.request_timeout_seconds,
            )
        event_seq = result.get("eventSeq") if isinstance(result, dict) else None
        recorded = result.get("recorded") if isinstance(result, dict) else None
        if (
            not isinstance(event_seq, int)
            or isinstance(event_seq, bool)
            or event_seq < 0
            or recorded is not True
        ):
            raise DshRuntimeError("dsh session/inject returned an invalid durable event")
        return event_seq

    async def compact_session(self, *, session_id: str) -> DshCompactionResult:
        """Run DSH's native manual compactor for one idle durable session."""

        await self.start()
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            result = await self._request(
                "session/compact",
                {"sessionId": session_id},
                timeout=self.template.request_timeout_seconds,
            )
        if not isinstance(result, dict):
            raise DshRuntimeError("dsh session/compact returned an invalid result")
        compacted = result.get("compacted")
        shadowed_items = result.get("shadowedItems")
        shadowed_tokens = result.get("shadowedTokens")
        summary_seq = result.get("summarySeq")
        if (
            not isinstance(compacted, bool)
            or not isinstance(shadowed_items, int)
            or isinstance(shadowed_items, bool)
            or shadowed_items < 0
            or not isinstance(shadowed_tokens, int)
            or isinstance(shadowed_tokens, bool)
            or shadowed_tokens < 0
            or (
                summary_seq is not None
                and (
                    not isinstance(summary_seq, int)
                    or isinstance(summary_seq, bool)
                    or summary_seq < 0
                )
            )
            or (compacted and summary_seq is None)
        ):
            raise DshRuntimeError("dsh session/compact returned invalid statistics")
        return DshCompactionResult(
            session_id=session_id,
            compacted=compacted,
            shadowed_items=shadowed_items,
            shadowed_tokens=shadowed_tokens,
            summary_seq=summary_seq,
        )

    async def configure_session_persona(
        self,
        *,
        session_id: str,
        persona: str,
    ) -> bool:
        """Set model-facing session persona without appending chat history.

        The custom DSH host mounts this as an agent-scoped system-prompt row.
        Repeating the same value is a no-op; changing it disposes only this
        idle live agent and resumes its existing durable session with the new
        persona. The persona itself never becomes a user/message event.
        """

        normalized = persona.strip()
        if not normalized:
            raise ValueError("dsh session persona cannot be empty")
        if len(normalized) > 250_000:
            raise ValueError("dsh session persona exceeds 250000 characters")
        await self.start()
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            result = await self._request(
                "session/persona",
                {
                    "sessionId": session_id,
                    "persona": normalized,
                },
                timeout=self.template.request_timeout_seconds,
            )
        configured = result.get("configured") if isinstance(result, dict) else None
        changed = result.get("changed") if isinstance(result, dict) else None
        if configured is not True or not isinstance(changed, bool):
            raise DshRuntimeError("dsh session/persona returned an invalid result")
        return changed

    async def configure_session_context(
        self,
        *,
        session_id: str,
        context: str,
    ) -> bool:
        """Update ephemeral model-facing turn state without a session event."""

        normalized = context.strip()
        if not normalized:
            raise ValueError("dsh session context cannot be empty")
        if len(normalized) > 50_000:
            raise ValueError("dsh session context exceeds 50000 characters")
        await self.start()
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            result = await self._request(
                "session/context",
                {
                    "sessionId": session_id,
                    "context": normalized,
                },
                timeout=self.template.request_timeout_seconds,
            )
        configured = result.get("configured") if isinstance(result, dict) else None
        changed = result.get("changed") if isinstance(result, dict) else None
        if configured is not True or not isinstance(changed, bool):
            raise DshRuntimeError("dsh session/context returned an invalid result")
        return changed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.is_running:
            try:
                await self._request("shutdown", None, timeout=10.0)
            except Exception:
                pass
        await self._terminate()

    async def cancel_turn(self, session_id: str, *, force: bool = False) -> bool:
        if not self.is_running:
            return False
        cancelled = False
        try:
            result = await self._request(
                "session/cancel",
                {"sessionId": session_id},
                timeout=10.0,
            )
            cancelled = not isinstance(result, dict) or bool(result.get("cancelled", True))
        except Exception:
            # A forced cancel is reserved for the isolated maintenance pool.
            # Normal guild chat must not kill other channels sharing a tenant
            # process merely because one session failed to acknowledge cancel.
            if force:
                await self._terminate()
                cancelled = True
        active = self._active_turns.get(session_id)
        if active is not None:
            active.finish_reason = "cancelled"
            active.done.set()
        return cancelled

    async def recover_session(self, session_id: str) -> str:
        """Dispose one wedged session handle while preserving its JSONL history.

        A successful session/cancel removes only the in-memory DSH handle; the
        next prompt resumes the same persisted session. If the JSON-RPC process
        cannot acknowledge that request, terminate the broken tenant process so
        its next use starts a clean process against the same storage root.
        """

        if not self.is_running:
            await self._terminate()
            return "process_restarted"
        try:
            await self._request(
                "session/cancel",
                {"sessionId": session_id},
                timeout=10.0,
            )
        except Exception:
            await self._terminate()
            return "process_restarted"
        active = self._active_turns.get(session_id)
        if active is not None:
            active.finish_reason = "recovered"
            active.done.set()
        return "session_recycled"

    async def _request(self, method: str, params: object, *, timeout: float) -> object:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise DshRuntimeError("dsh runtime is not running")
        request_id = self._next_request_id
        self._next_request_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[object] = loop.create_future()
        self._pending[request_id] = future
        frame = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            **({"params": params} if params is not None else {}),
        }
        try:
            encoded = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            async with self._write_lock:
                process.stdin.write(encoded)
                await process.stdin.drain()
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise DshRuntimeError(f"dsh JSON-RPC request timed out: {method}") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                try:
                    raw_line = await process.stdout.readline()
                except ValueError as exc:
                    await self._fail_protocol(
                        "dsh stdout JSON-RPC frame exceeded the configured "
                        f"{self.template.stdio_limit_bytes}-byte safety limit"
                    )
                    return
                if not raw_line:
                    break
                try:
                    message = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._fail_protocol("dsh emitted a non-JSON stdout frame")
                    return
                if not isinstance(message, dict):
                    await self._fail_protocol("dsh emitted a non-object JSON-RPC frame")
                    return
                if "id" in message:
                    self._resolve_response(message)
                elif isinstance(message.get("method"), str):
                    await self._handle_notification(message)
        finally:
            if not self._closed:
                stdout_closed_while_alive = False
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2.0)
                except TimeoutError:
                    # A process with a closed stdout pipe can never satisfy the
                    # JSON-RPC contract again. Terminate it immediately so the
                    # next recovery starts a fresh runtime instead of waiting
                    # on a doomed session/cancel request.
                    stdout_closed_while_alive = True
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                stderr_detail = ' | '.join(self._stderr_tail)
                message = "dsh runtime exited unexpectedly"
                if stdout_closed_while_alive:
                    message = f"{message} (stdout_closed_while_alive, forced_exit={process.returncode})"
                else:
                    message = f"{message} (exit_code={process.returncode})"
                if stderr_detail:
                    message = f"{message}: {stderr_detail}"
                await self._fail_protocol(message)

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            raw_line = await process.stderr.readline()
            if not raw_line:
                return
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                self._stderr_tail.append(line[:1000])

    def _resolve_response(self, message: dict[str, object]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            detail = error.get("message")
            future.set_exception(DshRuntimeError(f"dsh JSON-RPC error {code}: {detail}"))
            return
        future.set_result(message.get("result"))

    async def _handle_notification(self, message: dict[str, object]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            return
        active = self._active_turns.get(session_id)
        if active is None:
            return

        if active.on_event is not None:
            await _maybe_await(active.on_event(message))

        if method == "session.status":
            status = params.get("status")
            if status == "running":
                active.saw_running = True
            return

        if method != "session.event":
            return
        event = params.get("event")
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            return

        if event_type == "assistant/chunk":
            chunk = data.get("chunk")
            if isinstance(chunk, dict) and chunk.get("type") == "text-delta":
                text = chunk.get("text")
                if isinstance(text, str) and text:
                    active.text_chunks.append(text)
                    if active.on_text_delta is not None:
                        await _maybe_await(active.on_text_delta(text))
            return

        if event_type == "assistant/message":
            message_text = ""
            model_message = data.get("message")
            if isinstance(model_message, dict):
                content = model_message.get("content")
                if isinstance(content, list):
                    message_text = "".join(
                        str(block.get("text") or "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                    if message_text:
                        active.final_response = message_text
            usage = data.get("usage")
            if isinstance(usage, dict):
                input_tokens = usage.get("inputTokens")
                cache_read_tokens = usage.get("cacheReadTokens")
                cache_write_tokens = usage.get("cacheWriteTokens")
                output_tokens = usage.get("outputTokens")
                request_input_tokens = 0
                request_has_input_usage = False
                for value in (input_tokens, cache_read_tokens, cache_write_tokens):
                    if isinstance(value, int) and not isinstance(value, bool):
                        request_input_tokens += max(value, 0)
                        request_has_input_usage = True
                if request_has_input_usage:
                    # The footer describes model context pressure, not the sum
                    # of every tool-loop request made during one Agent turn.
                    active.input_tokens = max(active.input_tokens, request_input_tokens)
                    active.saw_input_usage = True
                if (
                    message_text
                    and isinstance(output_tokens, int)
                    and not isinstance(output_tokens, bool)
                    and output_tokens > 0
                ):
                    # Only the final user-visible assistant message belongs in
                    # Out. Hidden reasoning/tool-call steps are deliberately
                    # excluded; missing/zero provider usage is estimated later.
                    active.output_tokens = output_tokens
                    active.saw_output_usage = True
            return

        if event_type == "turn/end":
            reason = data.get("reason")
            if isinstance(reason, dict) and isinstance(reason.get("kind"), str):
                active.finish_reason = reason["kind"]
                if reason["kind"] == "error":
                    failure = reason.get("error")
                    if not isinstance(failure, dict):
                        failure = reason.get("failure")
                    if isinstance(failure, dict):
                        message = str(failure.get("message") or "").strip()
                        code = str(failure.get("code") or "").strip()
                    else:
                        message = ""
                        code = ""
                    active.turn_error = DshTurnFailedError(
                        message or "dsh model turn failed",
                        code=code or None,
                    )
            active.done.set()

    async def _fail_protocol(self, message: str) -> None:
        error = DshRuntimeError(message)
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        for active in tuple(self._active_turns.values()):
            if not active.done.is_set():
                active.protocol_error = error
                active.done.set()

    async def _terminate(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        tasks = [task for task in (self._reader_task, self._stderr_task) if task is not None]
        current = asyncio.current_task()
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(
                *(task for task in tasks if task is not current),
                return_exceptions=True,
            )
        self._process = None


class DshTenantRuntimePool:
    """Owns one isolated dsh process and storage root per Discord tenant."""

    def __init__(
        self,
        *,
        template: DshRuntimeTemplate,
        privacy: PrivacyBoundary,
        storage_namespace: str = "",
    ) -> None:
        if storage_namespace and not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", storage_namespace):
            raise ValueError("invalid dsh storage namespace")
        self.template = template
        self.privacy = privacy
        self.storage_namespace = storage_namespace
        self._runtimes: dict[str, DshJsonRpcProcess] = {}
        self._runtime_env: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def configure_runtime_env(self, values: dict[str, str]) -> None:
        if self._runtimes:
            raise DshRuntimeError("dsh runtime environment cannot change after a tenant starts")
        self._runtime_env.update({key: value for key, value in values.items() if value})

    async def run_turn(
        self,
        context: AgentRequestContext,
        *,
        text: str,
        on_text_delta: TextDeltaCallback | None = None,
        on_event: EventCallback | None = None,
        session_id_override: str | None = None,
    ) -> DshTurnResult:
        runtime = await self._runtime_for(context)
        if runtime.tenant_key != context.identity.tenant_key:
            raise CrossTenantAccessError("dsh runtime tenant did not match the request")
        session_id = session_id_override or context.identity.session_key
        if not re.fullmatch(r"s_[a-z0-9_]{16,160}", session_id):
            raise DshRuntimeError("invalid dsh session id")
        return await runtime.run_turn(
            session_id=session_id,
            text=text,
            on_text_delta=on_text_delta,
            on_event=on_event,
        )

    async def inject_context(
        self,
        context: AgentRequestContext,
        *,
        text: str,
    ) -> int:
        runtime = await self._runtime_for(context)
        if runtime.tenant_key != context.identity.tenant_key:
            raise CrossTenantAccessError("dsh runtime tenant did not match the request")
        session_id = context.identity.session_key
        if not re.fullmatch(r"s_[a-z0-9_]{16,160}", session_id):
            raise DshRuntimeError("invalid dsh session id")
        return await runtime.inject_context(
            session_id=session_id,
            text=text,
        )

    async def compact_session(
        self,
        context: AgentRequestContext,
    ) -> DshCompactionResult:
        runtime = await self._runtime_for(context)
        if runtime.tenant_key != context.identity.tenant_key:
            raise CrossTenantAccessError("dsh runtime tenant did not match the request")
        session_id = context.identity.session_key
        if not re.fullmatch(r"s_[a-z0-9_]{16,160}", session_id):
            raise DshRuntimeError("invalid dsh session id")
        return await runtime.compact_session(session_id=session_id)

    async def configure_session_persona(
        self,
        context: AgentRequestContext,
        *,
        persona: str,
    ) -> bool:
        runtime = await self._runtime_for(context)
        if runtime.tenant_key != context.identity.tenant_key:
            raise CrossTenantAccessError("dsh runtime tenant did not match the request")
        session_id = context.identity.session_key
        if not re.fullmatch(r"s_[a-z0-9_]{16,160}", session_id):
            raise DshRuntimeError("invalid dsh session id")
        return await runtime.configure_session_persona(
            session_id=session_id,
            persona=persona,
        )

    async def configure_session_context(
        self,
        request_context: AgentRequestContext,
        *,
        context: str,
    ) -> bool:
        runtime = await self._runtime_for(request_context)
        if runtime.tenant_key != request_context.identity.tenant_key:
            raise CrossTenantAccessError("dsh runtime tenant did not match the request")
        session_id = request_context.identity.session_key
        if not re.fullmatch(r"s_[a-z0-9_]{16,160}", session_id):
            raise DshRuntimeError("invalid dsh session id")
        return await runtime.configure_session_context(
            session_id=session_id,
            context=context,
        )

    async def session_snapshot(
        self,
        context: AgentRequestContext,
        *,
        max_summary_chars: int = 30_000,
        max_delta_chars: int = 40_000,
    ) -> DshSessionSnapshot:
        """Read bounded recovery metadata from this exact tenant/session log."""

        tenant_root = self.privacy.session_path(context).parent
        return await asyncio.to_thread(
            _read_dsh_session_snapshot,
            tenant_root,
            context.identity.session_key,
            max(max_summary_chars, 0),
            max(max_delta_chars, 0),
        )

    async def close(self) -> None:
        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)

    async def recycle_runtimes(self) -> int:
        """Dispose live tenant processes while keeping the configured pool reusable."""

        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)
        return len(runtimes)

    async def cancel_turn(
        self,
        context: AgentRequestContext,
        *,
        session_id_override: str | None = None,
        force: bool = False,
    ) -> bool:
        session_id = session_id_override or context.identity.session_key
        async with self._lock:
            runtime = self._runtimes.get(context.identity.tenant_key)
        if runtime is None:
            return False
        return await runtime.cancel_turn(session_id, force=force)

    async def recover_session(
        self,
        context: AgentRequestContext,
        *,
        session_id_override: str | None = None,
    ) -> str:
        """Reset one cached DSH session/process without rotating its identity."""

        session_id = session_id_override or context.identity.session_key
        if not re.fullmatch(r"s_[a-z0-9_]{16,160}", session_id):
            raise DshRuntimeError("invalid dsh session id")
        async with self._lock:
            runtime = self._runtimes.get(context.identity.tenant_key)
        if runtime is None:
            return "runtime_not_started"
        return await runtime.recover_session(session_id)

    async def _runtime_for(self, context: AgentRequestContext) -> DshJsonRpcProcess:
        tenant_key = context.identity.tenant_key
        async with self._lock:
            runtime = self._runtimes.get(tenant_key)
            if runtime is None:
                tenant_root = self.privacy.session_path(context).parent
                if self.storage_namespace:
                    tenant_root = tenant_root / self.storage_namespace
                runtime = DshJsonRpcProcess(
                    template=self.template,
                    tenant_key=tenant_key,
                    session_root=tenant_root,
                    runtime_env=self._runtime_env,
                )
                self._runtimes[tenant_key] = runtime
            return runtime


def _read_dsh_session_snapshot(
    tenant_root: Path,
    session_id: str,
    max_summary_chars: int,
    max_delta_chars: int = 40_000,
) -> DshSessionSnapshot:
    """Read one DSH log's checkpoint plus a bounded post-checkpoint delta."""

    resolved_root = tenant_root.resolve()
    candidates: list[Path] = []
    try:
        discovered = resolved_root.rglob("session.jsonl")
        for candidate in discovered:
            if candidate.parent.name != session_id:
                continue
            resolved = candidate.resolve()
            try:
                resolved.relative_to(resolved_root)
            except ValueError:
                continue
            if resolved.is_file():
                candidates.append(resolved)
    except OSError:
        return DshSessionSnapshot()
    if not candidates:
        return DshSessionSnapshot()

    try:
        selected = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        byte_size = selected.stat().st_size
    except OSError:
        return DshSessionSnapshot()

    latest_summary = ""
    delta_records: list[str] = []
    latest_input_tokens: int | None = None
    pending_turn_input_tokens = 0
    latest_turn_error_code: str | None = None
    latest_turn_error_message = ""
    try:
        with selected.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "assistant/message":
                    data = event.get("data")
                    usage = data.get("usage") if isinstance(data, dict) else None
                    request_input_tokens = 0
                    request_has_input_usage = False
                    if isinstance(usage, dict):
                        for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens"):
                            value = usage.get(key)
                            if isinstance(value, int) and not isinstance(value, bool):
                                request_input_tokens += max(value, 0)
                                request_has_input_usage = True
                    if request_has_input_usage:
                        pending_turn_input_tokens = max(
                            pending_turn_input_tokens,
                            request_input_tokens,
                        )
                    delta_text = _extract_dsh_assistant_text(data)
                    if delta_text:
                        delta_records.append(
                            _format_dsh_delta_record("Assistant", delta_text)
                        )
                    continue
                if event.get("type") == "user/message":
                    data = event.get("data")
                    raw_user_text = (
                        _extract_text_blocks(data.get("content"))
                        if isinstance(data, dict)
                        else ""
                    )
                    is_recovered_frame = raw_user_text.startswith(
                        (
                            "[Recovered same-channel memory checkpoint; ",
                            "[Recovered post-checkpoint DSH delta; ",
                        )
                    )
                    recovered_summary = (
                        _extract_recovered_memory_frame(
                            raw_user_text,
                            start_marker=(
                                "[Recovered same-channel memory checkpoint; "
                                "host-authored boundary]"
                            ),
                            end_marker="[End recovered memory checkpoint]",
                        )
                        if is_recovered_frame
                        else ""
                    )
                    recovered_delta = (
                        _extract_recovered_memory_frame(
                            raw_user_text,
                            start_marker=(
                                "[Recovered post-checkpoint DSH delta; "
                                "host-authored boundary]"
                            ),
                            end_marker="[End recovered post-checkpoint DSH delta]",
                        )
                        if is_recovered_frame
                        else ""
                    )
                    if recovered_summary or recovered_delta:
                        if recovered_summary:
                            latest_summary = recovered_summary
                            delta_records.clear()
                        if recovered_delta:
                            delta_records.append(recovered_delta)
                        continue
                    delta_text = _extract_dsh_user_text(data)
                    if delta_text:
                        delta_records.append(_format_dsh_delta_record("User", delta_text))
                    continue
                if event.get("type") == "tool/call":
                    data = event.get("data")
                    name = str(data.get("name") or "unknown") if isinstance(data, dict) else "unknown"
                    delta_records.append(f"[Tool call recorded: {name[:120]}]")
                    continue
                if event.get("type") == "tool/result":
                    data = event.get("data")
                    if isinstance(data, dict):
                        name = str(data.get("name") or "unknown")[:120]
                        status = "failed" if bool(data.get("error")) else "completed"
                    else:
                        name = "unknown"
                        status = "completed"
                    delta_records.append(f"[Tool result recorded: {name}; {status}]")
                    continue
                if event.get("type") == "turn/end":
                    data = event.get("data")
                    reason = data.get("reason") if isinstance(data, dict) else None
                    if (
                        isinstance(reason, dict)
                        and reason.get("kind") == "completed"
                        and pending_turn_input_tokens > 0
                    ):
                        latest_input_tokens = pending_turn_input_tokens
                    pending_turn_input_tokens = 0
                    if isinstance(reason, dict) and reason.get("kind") == "error":
                        failure = reason.get("error")
                        if not isinstance(failure, dict):
                            failure = reason.get("failure")
                        if isinstance(failure, dict):
                            latest_turn_error_code = (
                                str(failure.get("code") or "").strip().upper() or None
                            )
                            latest_turn_error_message = str(
                                failure.get("message") or ""
                            ).strip()
                        else:
                            latest_turn_error_code = None
                            latest_turn_error_message = ""
                    else:
                        latest_turn_error_code = None
                        latest_turn_error_message = ""
                    continue
                if event.get("type") != "compaction/summary":
                    continue
                data = event.get("data")
                summary = data.get("summary") if isinstance(data, dict) else None
                if isinstance(summary, str):
                    text = summary
                elif isinstance(summary, list):
                    text = "".join(
                        str(block.get("text") or "")
                        for block in summary
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                else:
                    text = ""
                if text.strip():
                    latest_summary = text.strip()
                    # A successful newer summary subsumes every earlier delta.
                    delta_records.clear()
    except OSError:
        return DshSessionSnapshot(byte_size=byte_size)

    if max_summary_chars <= 0:
        latest_summary = ""
    elif len(latest_summary) > max_summary_chars:
        latest_summary = latest_summary[:max_summary_chars].rstrip()
    post_compaction_delta, kept_delta_count, dropped_delta_count = (
        _bound_dsh_delta_records(delta_records, max_chars=max_delta_chars)
    )
    return DshSessionSnapshot(
        byte_size=max(int(byte_size), 0),
        latest_compaction_summary=latest_summary,
        post_compaction_delta=post_compaction_delta,
        post_compaction_event_count=kept_delta_count,
        post_compaction_dropped_count=dropped_delta_count,
        latest_input_tokens=latest_input_tokens,
        latest_turn_error_code=latest_turn_error_code,
        latest_turn_error_message=latest_turn_error_message,
    )


def _extract_text_blocks(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in value
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _extract_dsh_assistant_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return _extract_text_blocks(content)


def _extract_dsh_user_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    text = _extract_text_blocks(data.get("content"))
    if not text:
        return ""
    for boundary in (
        "[Current addressed Discord request follows]",
        "[Recovered same-channel context ends; retry the current addressed request below]",
    ):
        if boundary in text:
            text = text.rsplit(boundary, 1)[-1].strip()
    if text.startswith("[Recovered same-channel memory checkpoint;"):
        return ""
    boundary_positions = [
        position
        for marker in (
            "\n\n[Discord host turn metadata",
            "\n\n[ATRI host-loaded extension capability supplement",
            "\n\n[以下是 Discord 宿主提供的当轮运行时规则",
        )
        if (position := text.find(marker)) >= 0
    ]
    if boundary_positions:
        text = text[: min(boundary_positions)].rstrip()
    lines = text.splitlines()
    if lines and lines[0].startswith("[nickname="):
        lines = lines[1:]
        if lines and lines[0].startswith("当前这条消息发送时间:"):
            lines = lines[1:]
    return "\n".join(lines).strip()


def _extract_recovered_memory_frame(
    text: str,
    *,
    start_marker: str,
    end_marker: str,
) -> str:
    start = text.find(start_marker)
    if start < 0 or end_marker not in text[start:]:
        return ""
    content_start = start + len(start_marker)
    framed = text[content_start : text.find(end_marker, content_start)].strip()
    lines = framed.splitlines()
    if lines and (
        lines[0].startswith("This is factual conversation memory")
        or lines[0].startswith("These are bounded user/assistant records")
    ):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _format_dsh_delta_record(role: str, text: str, *, max_chars: int = 4_000) -> str:
    compact = text.strip()
    if len(compact) > max_chars:
        compact = compact[: max_chars - 18].rstrip() + "\n[record truncated]"
    return f"[{role}]\n{compact}"


def _bound_dsh_delta_records(
    records: list[str],
    *,
    max_chars: int,
) -> tuple[str, int, int]:
    if max_chars <= 0 or not records:
        return "", 0, len(records)
    kept_reversed: list[str] = []
    used = 0
    for record in reversed(records):
        separator = 2 if kept_reversed else 0
        remaining = max_chars - used - separator
        if remaining <= 0:
            break
        candidate = record if len(record) <= remaining else record[-remaining:]
        kept_reversed.append(candidate)
        used += len(candidate) + separator
        if len(record) > remaining:
            break
    kept = list(reversed(kept_reversed))
    return "\n\n".join(kept), len(kept), max(len(records) - len(kept), 0)


def _read_positive_float(name: str, default: float) -> float:
    return _parse_positive_float(os.getenv(name), default)


def _parse_positive_float(raw_value: str | None, default: float) -> float:
    raw = str(raw_value or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _read_optional_positive_int(name: str) -> int | None:
    return _parse_optional_positive_int(os.getenv(name))


def _parse_optional_positive_int(raw_value: str | None) -> int | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_bounded_int(
    raw_value: str | None,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(raw_value or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value
