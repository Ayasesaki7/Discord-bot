from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .project_tools import ProjectToolError


_LOG_FILES = {
    "bot": "bot.log",
    "error": "bot.err.log",
}
_LOG_TAIL_DEFAULT_LINES = 160
_LOG_TAIL_MAX_LINES = 500
_LOG_READ_MAX_BYTES = 512 * 1024
_COMMAND_OUTPUT_MAX_BYTES = 24 * 1024
_COMMAND_TIMEOUT_SECONDS = 8.0
_SECRET_PATTERNS = (
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


class RuntimeToolError(ProjectToolError):
    pass


class RuntimeDiagnosticHost:
    """Owner-only diagnostics without an arbitrary shell or filesystem access.

    The model chooses a named probe, while the host owns the complete argv.
    There is deliberately no command-line string, arguments field, shell,
    interpreter evaluation mode, path input, pipe, redirect, or environment
    readout exposed to the model.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._started_monotonic = time.monotonic()

    def system_info(self, arguments: dict[str, object]) -> dict[str, object]:
        self._reject_unknown(arguments, set())
        payload = {
            "system": platform.system() or os.name,
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
            "pythonVersion": platform.python_version(),
            "pid": os.getpid(),
            "cpuCount": os.cpu_count(),
            "workingDirectory": str(self.project_root),
            "containerLikely": self._container_likely(),
            "availableCommands": self.available_commands(),
            "security": {
                "shell": False,
                "customArguments": False,
                "arbitraryPaths": False,
                "environmentVariables": False,
                "timeoutSeconds": _COMMAND_TIMEOUT_SECONDS,
                "outputLimitBytes": _COMMAND_OUTPUT_MAX_BYTES,
            },
        }
        return self._result(
            f"Detected {payload['system']} {payload['release']} on {payload['architecture']}.",
            json.dumps(payload, ensure_ascii=False),
        )

    def read_log(self, arguments: dict[str, object]) -> dict[str, object]:
        self._reject_unknown(arguments, {"stream", "tail_lines"})
        stream = str(arguments.get("stream") or "both").strip().casefold()
        if stream not in {"bot", "error", "both"}:
            raise RuntimeToolError("stream must be bot, error, or both")
        tail_lines = arguments.get("tail_lines", _LOG_TAIL_DEFAULT_LINES)
        if not isinstance(tail_lines, int) or isinstance(tail_lines, bool) or tail_lines <= 0:
            raise RuntimeToolError("tail_lines must be a positive integer")
        tail_lines = min(tail_lines, _LOG_TAIL_MAX_LINES)
        selected = tuple(_LOG_FILES) if stream == "both" else (stream,)
        sections: list[str] = []
        any_truncated = False
        for name in selected:
            path = (self.project_root / _LOG_FILES[name]).resolve(strict=False)
            try:
                path.relative_to(self.project_root)
            except ValueError as exc:  # pragma: no cover - fixed paths are defensive by design
                raise RuntimeToolError("configured log path escaped the project") from exc
            if not path.is_file():
                sections.append(f"[{name}] log file does not exist.")
                continue
            lines, truncated = self._tail_file(path, tail_lines)
            any_truncated = any_truncated or truncated
            safe_lines = [self._redact_secrets(line)[:4000] for line in lines]
            sections.append(f"[{name}]\n" + ("\n".join(safe_lines) or "(empty)"))
        return self._result(
            f"Read the fixed {stream} bot log tail (up to {tail_lines} lines per stream).",
            "\n\n".join(sections),
            truncated=any_truncated,
        )

    async def run_command(self, arguments: dict[str, object]) -> dict[str, object]:
        self._reject_unknown(arguments, {"command"})
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise RuntimeToolError("command must be one of the advertised diagnostic names")
        command = command.strip().casefold()
        if command not in self.available_commands():
            raise RuntimeToolError(
                "unsupported or unavailable diagnostic command; call runtime_system_info first"
            )

        if command == "disk":
            usage = shutil.disk_usage(self.project_root)
            return self._json_command_result(
                command,
                {
                    "path": str(self.project_root),
                    "totalBytes": usage.total,
                    "usedBytes": usage.used,
                    "freeBytes": usage.free,
                },
            )
        if command == "memory":
            return self._json_command_result(command, self._memory_snapshot())
        if command == "process":
            process_payload: dict[str, object] = {
                "pid": os.getpid(),
                "botProcessUptimeSeconds": round(time.monotonic() - self._started_monotonic, 3),
                "cpuCount": os.cpu_count(),
            }
            if hasattr(os, "getloadavg"):
                try:
                    process_payload["loadAverage"] = list(os.getloadavg())
                except OSError:
                    pass
            return self._json_command_result(command, process_payload)
        if command == "uptime" and platform.system() == "Windows":
            milliseconds = ctypes.windll.kernel32.GetTickCount64()
            return self._json_command_result(
                command,
                {"systemUptimeSeconds": round(milliseconds / 1000, 3)},
            )

        argv = self._external_argv(command)
        if argv is None:
            raise RuntimeToolError("diagnostic command is unavailable on this operating system")
        return await self._run_fixed_argv(command, argv)

    def available_commands(self) -> list[str]:
        commands = ["disk", "memory", "process"]
        for command in (
            "hostname",
            "whoami",
            "python_version",
            "node_version",
            "git_version",
            "ffmpeg_version",
            "uname",
            "uptime",
        ):
            if command == "uptime" and platform.system() == "Windows":
                commands.append(command)
            elif self._external_argv(command) is not None:
                commands.append(command)
        return commands

    def _external_argv(self, command: str) -> tuple[str, ...] | None:
        executable: str | None
        arguments: tuple[str, ...]
        if command == "python_version":
            executable, arguments = sys.executable, ("--version",)
        elif command == "node_version":
            executable, arguments = shutil.which("node"), ("--version",)
        elif command == "git_version":
            executable, arguments = shutil.which("git"), ("--version",)
        elif command == "ffmpeg_version":
            executable, arguments = shutil.which("ffmpeg"), ("-version",)
        elif command == "hostname":
            executable, arguments = shutil.which("hostname"), ()
        elif command == "whoami":
            executable, arguments = shutil.which("whoami"), ()
        elif command == "uname" and platform.system() != "Windows":
            executable, arguments = shutil.which("uname"), ("-a",)
        elif command == "uptime" and platform.system() != "Windows":
            executable, arguments = shutil.which("uptime"), ()
        else:
            return None
        return (executable, *arguments) if executable else None

    async def _run_fixed_argv(
        self,
        command: str,
        argv: tuple[str, ...],
    ) -> dict[str, object]:
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.project_root),
                env=self._sanitized_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except OSError as exc:
            raise RuntimeToolError(f"diagnostic command could not start: {command}") from exc

        stdout_task = asyncio.create_task(self._read_bounded(process.stdout))
        stderr_task = asyncio.create_task(self._read_bounded(process.stderr))
        try:
            await asyncio.wait_for(process.wait(), timeout=_COMMAND_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise RuntimeToolError(
                f"diagnostic command exceeded {_COMMAND_TIMEOUT_SECONDS:g} seconds"
            ) from exc
        stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        combined = stdout.strip()
        if stderr.strip():
            combined = f"{combined}\n[stderr]\n{stderr.strip()}".strip()
        combined = self._redact_secrets(combined) or "(no output)"
        return {
            "summary": f"Diagnostic command {command} exited with code {process.returncode}.",
            "content": json.dumps(
                {
                    "command": command,
                    "exitCode": process.returncode,
                    "output": combined,
                },
                ensure_ascii=False,
            ),
            "truncated": stdout_truncated or stderr_truncated,
        }

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader | None,
    ) -> tuple[str, bool]:
        if stream is None:
            return "", False
        saved = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = _COMMAND_OUTPUT_MAX_BYTES - len(saved)
            if remaining > 0:
                saved.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return saved.decode("utf-8", errors="replace"), truncated

    @staticmethod
    def _tail_file(path: Path, line_count: int) -> tuple[list[str], bool]:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                start = max(0, end - _LOG_READ_MAX_BYTES)
                handle.seek(start)
                raw = handle.read(_LOG_READ_MAX_BYTES)
        except OSError as exc:
            raise RuntimeToolError("bot log could not be read") from exc
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        truncated = start > 0 or len(lines) > line_count
        return lines[-line_count:], truncated

    @staticmethod
    def _memory_snapshot() -> dict[str, object]:
        proc_meminfo = Path("/proc/meminfo")
        if proc_meminfo.is_file():
            values: dict[str, int] = {}
            try:
                for line in proc_meminfo.read_text(encoding="utf-8").splitlines():
                    key, raw_value = line.split(":", 1)
                    amount = int(raw_value.strip().split()[0]) * 1024
                    values[key] = amount
            except (OSError, ValueError, IndexError) as exc:
                raise RuntimeToolError("system memory information could not be read") from exc
            return {
                "totalBytes": values.get("MemTotal"),
                "availableBytes": values.get("MemAvailable"),
                "swapTotalBytes": values.get("SwapTotal"),
                "swapFreeBytes": values.get("SwapFree"),
            }
        if platform.system() == "Windows":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memoryLoadPercent", ctypes.c_ulong),
                    ("totalPhysical", ctypes.c_ulonglong),
                    ("availablePhysical", ctypes.c_ulonglong),
                    ("totalPageFile", ctypes.c_ulonglong),
                    ("availablePageFile", ctypes.c_ulonglong),
                    ("totalVirtual", ctypes.c_ulonglong),
                    ("availableVirtual", ctypes.c_ulonglong),
                    ("availableExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise RuntimeToolError("system memory information could not be read")
            return {
                "totalBytes": status.totalPhysical,
                "availableBytes": status.availablePhysical,
                "memoryLoadPercent": status.memoryLoadPercent,
                "pageFileTotalBytes": status.totalPageFile,
                "pageFileAvailableBytes": status.availablePageFile,
            }
        raise RuntimeToolError("memory diagnostics are unavailable on this operating system")

    @staticmethod
    def _container_likely() -> bool:
        if Path("/.dockerenv").exists():
            return True
        try:
            text = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return any(marker in text.casefold() for marker in ("docker", "containerd", "kubepods"))

    @staticmethod
    def _sanitized_env() -> dict[str, str]:
        allowed = {
            "LANG",
            "LC_ALL",
            "LOCALAPPDATA",
            "OS",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TZ",
            "WINDIR",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "NO_COLOR": "1",
            }
        )
        return env

    @staticmethod
    def _reject_unknown(arguments: dict[str, object], allowed: set[str]) -> None:
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise RuntimeToolError(
                "unsupported diagnostic parameters: " + ", ".join(unknown)
            )

    @staticmethod
    def _redact_secrets(text: str) -> str:
        result = text
        for pattern in _SECRET_PATTERNS:
            if "prefix" in pattern.groupindex:
                result = pattern.sub(lambda match: f"{match.group('prefix')}[redacted-secret]", result)
            else:
                result = pattern.sub("[redacted-secret]", result)
        return result

    @staticmethod
    def _result(
        summary: str,
        content: str,
        *,
        truncated: bool = False,
    ) -> dict[str, object]:
        return {
            "summary": summary,
            "content": content,
            "truncated": truncated,
        }

    @classmethod
    def _json_command_result(
        cls,
        command: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        return {
            "summary": f"Diagnostic command {command} completed.",
            "content": json.dumps(
                {"command": command, "exitCode": 0, "result": payload},
                ensure_ascii=False,
            ),
            "truncated": False,
        }
