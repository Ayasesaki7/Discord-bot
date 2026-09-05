from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any


_BLOCKED_PARTS = {
    ".git",
    ".idea",
    ".venv",
    ".vscode",
    "__pycache__",
    "data",
    "credentials",
    "node_modules",
    "venv",
}
_BLOCKED_FILE_PATTERNS = (
    re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE),
    # Sensitive data files stay blocked, while source files with honest names
    # such as credentials.py or token_meter.js remain readable and are still
    # passed through line-level secret redaction.
    re.compile(
        r".*(?:cookie|credential|password|secret|token).*"
        r"\.(?:json|txt|bak|env|ini|yaml|yml|toml)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^[^.]*(?:cookie|credential|password|secret|token)[^.]*$",
        re.IGNORECASE,
    ),
    re.compile(r".*\.(?:key|pem|pfx|p12)$", re.IGNORECASE),
    re.compile(r".*\.log$", re.IGNORECASE),
)
_ALLOWED_DOTENV_FILES = {".env.example"}
_WRITABLE_CONFIG_ROOT = Path("config/agent")
_WRITABLE_AGENT_TOOL_ROOT = Path("tools/agent")
_WRITABLE_DRAW_TOOL_ROOT = Path("tools/draw")
_WRITABLE_FORTUNE_TOOL_ROOT = Path("tools/fortune")
_WRITABLE_SUFFIXES_BY_ROOT = {
    _WRITABLE_CONFIG_ROOT: {".json", ".yaml", ".yml", ".toml", ".txt", ".md"},
    _WRITABLE_AGENT_TOOL_ROOT: {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".txt",
        ".md",
    },
    _WRITABLE_DRAW_TOOL_ROOT: {
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".txt",
        ".md",
    },
    _WRITABLE_FORTUNE_TOOL_ROOT: {
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".txt",
        ".md",
    },
}
_TEXT_FILE_MAX_BYTES = 1024 * 1024
_READ_MAX_LINES = 400
_SEARCH_MAX_FILES = 5000
_SEARCH_MAX_RESULTS = 80
_LIST_MAX_ENTRIES = 300
_EXECUTABLE_SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs"}
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:Bearer\s+)[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*"
        r"(?P<quote>['\"])[^'\"\r\n]{8,}(?P=quote)"
    ),
)


class ProjectToolError(RuntimeError):
    pass


class ProjectToolHost:
    """Owner-only, project-root-confined tools for ATRI's coding agent.

    The host intentionally exposes no arbitrary shell. Non-sensitive project
    source is readable, but writes are confined to dedicated configuration and
    tool-authoring zones. The only executable check is syntax validation of an
    explicitly named source file.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._mutation_lock = asyncio.Lock()

    async def execute(
        self,
        action: str,
        arguments: dict[str, object],
        *,
        source_creation_approved: bool = False,
    ) -> dict[str, object]:
        if action == "read":
            return self._read(arguments)
        if action == "search":
            return self._search(arguments)
        if action == "list":
            return self._list(arguments)
        if action == "status":
            return await self._status()
        if action == "check":
            return await self._check(arguments)
        if action == "edit":
            async with self._mutation_lock:
                return self._edit(arguments)
        if action == "create":
            async with self._mutation_lock:
                return self._create(
                    arguments,
                    source_creation_approved=source_creation_approved,
                )
        raise ProjectToolError(f"unsupported project action: {action}")

    def _read(self, arguments: dict[str, object]) -> dict[str, object]:
        path = self._resolve_file(self._required_string(arguments, "file_path"), must_exist=True)
        offset = self._positive_int(arguments.get("offset"), default=1)
        limit = min(self._positive_int(arguments.get("limit"), default=200), _READ_MAX_LINES)
        text = self._read_text(path)
        lines = text.splitlines()
        start = min(offset - 1, len(lines))
        selected = lines[start : start + limit]
        rendered = "\n".join(
            f"{number}: {self._redact_secrets(line)[:2000]}"
            for number, line in enumerate(selected, start=start + 1)
        )
        end = start + len(selected)
        if end < len(lines):
            rendered += f"\n... ({len(lines) - end} more lines; continue with offset={end + 1})"
        return self._result(
            f"Read {self._display_path(path)} lines {start + 1}-{end} of {len(lines)}.",
            rendered,
            truncated=end < len(lines),
        )

    def _search(self, arguments: dict[str, object]) -> dict[str, object]:
        query = self._required_string(arguments, "query")
        if len(query) > 200:
            raise ProjectToolError("search query is too long")
        file_glob = str(arguments.get("file_glob") or "*").strip() or "*"
        needle = query.casefold()
        results: list[str] = []
        inspected = 0
        truncated = False
        for directory, directory_names, file_names in os.walk(self.project_root):
            directory_path = Path(directory)
            directory_names[:] = [
                name
                for name in directory_names
                if not self._is_blocked(directory_path / name / "placeholder")
            ]
            for file_name in file_names:
                if len(results) >= _SEARCH_MAX_RESULTS or inspected >= _SEARCH_MAX_FILES:
                    truncated = True
                    break
                path = directory_path / file_name
                if self._is_blocked(path):
                    continue
                relative = path.relative_to(self.project_root)
                if not relative.match(file_glob) and not path.match(file_glob):
                    continue
                inspected += 1
                try:
                    text = self._read_text(path)
                except ProjectToolError:
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if needle not in line.casefold():
                        continue
                    safe_line = self._redact_secrets(line.strip())[:500]
                    results.append(f"{relative.as_posix()}:{line_number}: {safe_line}")
                    if len(results) >= _SEARCH_MAX_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        content = "\n".join(results) if results else "No matches."
        return self._result(
            f"Found {len(results)} literal matches in {inspected} inspected files.",
            content,
            truncated=truncated,
        )

    def _list(self, arguments: dict[str, object]) -> dict[str, object]:
        raw_directory = str(arguments.get("directory") or ".").strip() or "."
        max_depth = min(self._positive_int(arguments.get("max_depth"), default=2), 4)
        candidate = Path(raw_directory)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        try:
            directory = candidate.resolve(strict=True)
            directory.relative_to(self.project_root)
        except (OSError, ValueError) as exc:
            raise ProjectToolError("directory escapes or does not exist in the project") from exc
        if not directory.is_dir() or self._directory_is_blocked(directory):
            raise ProjectToolError("directory is blocked or is not a directory")

        entries: list[str] = []
        truncated = False
        base_depth = len(directory.relative_to(self.project_root).parts)
        for current, directory_names, file_names in os.walk(directory):
            current_path = Path(current)
            current_depth = len(current_path.relative_to(self.project_root).parts) - base_depth
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not self._directory_is_blocked(current_path / name)
            )
            if current_depth >= max_depth:
                directory_names[:] = []
            for name in directory_names:
                entries.append(f"{self._display_path(current_path / name)}/")
                if len(entries) >= _LIST_MAX_ENTRIES:
                    truncated = True
                    break
            if truncated:
                break
            for name in sorted(file_names):
                path = current_path / name
                if self._is_blocked(path):
                    continue
                entries.append(self._display_path(path))
                if len(entries) >= _LIST_MAX_ENTRIES:
                    truncated = True
                    break
            if truncated:
                break
        return self._result(
            f"Listed {len(entries)} project paths with max_depth={max_depth}.",
            "\n".join(entries) if entries else "Directory is empty.",
            truncated=truncated,
        )

    def _edit(self, arguments: dict[str, object]) -> dict[str, object]:
        path = self._resolve_file(self._required_string(arguments, "file_path"), must_exist=True)
        self._require_writable_path(path)
        old_string = self._required_string(arguments, "old_string", strip=False)
        new_string = self._required_string(arguments, "new_string", strip=False, allow_empty=True)
        if old_string == new_string:
            raise ProjectToolError("old_string and new_string must differ")
        text = self._read_text(path)
        occurrences = text.count(old_string)
        if occurrences == 0:
            raise ProjectToolError("old_string was not found; read the file again")
        replace_all = bool(arguments.get("replace_all"))
        if occurrences > 1 and not replace_all:
            raise ProjectToolError(
                f"old_string occurs {occurrences} times; make it unique or set replace_all"
            )
        updated = text.replace(old_string, new_string, -1 if replace_all else 1)
        self._write_text(path, updated)
        replaced = occurrences if replace_all else 1
        return self._result(
            f"Updated {self._display_path(path)} ({replaced} replacement(s)).",
            self._write_effect_message(path),
        )

    def _create(
        self,
        arguments: dict[str, object],
        *,
        source_creation_approved: bool,
    ) -> dict[str, object]:
        path = self._resolve_file(self._required_string(arguments, "file_path"), must_exist=False)
        self._require_writable_path(path)
        if path.suffix.casefold() in _EXECUTABLE_SOURCE_SUFFIXES and not source_creation_approved:
            raise ProjectToolError(
                "creating executable tool source requires explicit owner confirmation "
                "in the current maintenance request"
            )
        content = self._required_string(arguments, "content", strip=False, allow_empty=True)
        if path.exists():
            raise ProjectToolError("target already exists; use project_edit after reading it")
        if len(content.encode("utf-8")) > _TEXT_FILE_MAX_BYTES:
            raise ProjectToolError("new file exceeds the 1 MiB limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_text(path, content)
        return self._result(
            f"Created {self._display_path(path)}.",
            self._write_effect_message(path),
        )

    def _write_effect_message(self, path: Path) -> str:
        relative = path.relative_to(self.project_root).as_posix()
        if relative == "config/agent/tool_guidance.md":
            return (
                "The extension capability supplement was changed. The read-only chat host "
                "will validate and hot-load it on the next Agent turn; no core source edit "
                "or bot restart is required."
            )
        return (
            "The file was changed in the live project. Executable behavior will not affect "
            "the running bot until its owning module reloads or the bot restarts."
        )

    async def _status(self) -> dict[str, object]:
        process = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--short",
            "--untracked-files=all",
            cwd=str(self.project_root),
            env=self._sanitized_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[:500]
            raise ProjectToolError(f"git status failed: {detail}")
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        safe_lines = [line for line in lines if not self._status_line_is_sensitive(line)]
        hidden_count = len(lines) - len(safe_lines)
        content = "\n".join(safe_lines[:200]) or "Working tree is clean (excluding sensitive files)."
        if hidden_count:
            content += f"\n[sensitive paths hidden: {hidden_count}]"
        return self._result(
            f"Git reports {len(safe_lines)} visible changed paths.",
            content,
            truncated=len(safe_lines) > 200,
        )

    async def _check(self, arguments: dict[str, object]) -> dict[str, object]:
        raw_paths = arguments.get("file_paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ProjectToolError("file_paths must be a non-empty array")
        if len(raw_paths) > 20:
            raise ProjectToolError("at most 20 files can be checked at once")
        checked: list[str] = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                raise ProjectToolError("every file path must be a string")
            path = self._resolve_file(raw_path, must_exist=True)
            suffix = path.suffix.casefold()
            if suffix == ".py":
                source = self._read_text(path)
                try:
                    compile(source, str(path), "exec")
                except SyntaxError as exc:
                    raise ProjectToolError(
                        f"Python syntax error in {self._display_path(path)}:{exc.lineno}: {exc.msg}"
                    ) from exc
            elif suffix == ".json":
                try:
                    json.loads(self._read_text(path))
                except json.JSONDecodeError as exc:
                    raise ProjectToolError(
                        f"JSON syntax error in {self._display_path(path)}:{exc.lineno}: {exc.msg}"
                    ) from exc
            elif suffix in {".js", ".mjs", ".cjs"}:
                await self._node_syntax_check(path)
            else:
                raise ProjectToolError(f"no safe syntax checker for {path.suffix or 'this file type'}")
            checked.append(self._display_path(path))
        return self._result(
            f"Syntax checks passed for {len(checked)} file(s).",
            "\n".join(checked),
        )

    async def _node_syntax_check(self, path: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "node",
            "--check",
            str(path),
            cwd=str(self.project_root),
            env=self._sanitized_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode != 0:
            detail = self._redact_secrets(stderr.decode("utf-8", errors="replace"))[:1000]
            raise ProjectToolError(f"JavaScript syntax check failed: {detail}")

    def _resolve_file(self, raw_path: str, *, must_exist: bool) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        try:
            resolved = candidate.resolve(strict=must_exist)
        except OSError as exc:
            raise ProjectToolError("file path could not be resolved") from exc
        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise ProjectToolError("file path escapes the project root") from exc
        if self._is_blocked(resolved):
            raise ProjectToolError("access to sensitive or generated project data is blocked")
        if must_exist and not resolved.is_file():
            raise ProjectToolError("target is not a regular file")
        return resolved

    def _is_blocked(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.project_root)
        except ValueError:
            return True
        if any(part.casefold() in _BLOCKED_PARTS for part in relative.parts[:-1]):
            return True
        name = relative.name
        if name.casefold() in _ALLOWED_DOTENV_FILES:
            return False
        return any(pattern.fullmatch(name) for pattern in _BLOCKED_FILE_PATTERNS)

    def _directory_is_blocked(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.project_root)
        except ValueError:
            return True
        return any(part.casefold() in _BLOCKED_PARTS for part in relative.parts)

    def _require_writable_path(self, path: Path) -> None:
        relative = path.relative_to(self.project_root)
        for writable_root, allowed_suffixes in _WRITABLE_SUFFIXES_BY_ROOT.items():
            try:
                nested = relative.relative_to(writable_root)
            except ValueError:
                continue
            if not nested.parts:
                break
            if path.suffix.casefold() not in allowed_suffixes:
                allowed = ", ".join(sorted(allowed_suffixes))
                raise ProjectToolError(
                    f"writes in {writable_root.as_posix()} require one of: {allowed}"
                )
            return
        raise ProjectToolError(
            "core project files are read-only; writes are allowed only under "
            "config/agent, tools/agent, tools/draw, or tools/fortune"
        )

    def _read_text(self, path: Path) -> str:
        try:
            size = path.stat().st_size
            if size > _TEXT_FILE_MAX_BYTES:
                raise ProjectToolError("file exceeds the 1 MiB text limit")
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectToolError("file is not UTF-8 text") from exc
        except OSError as exc:
            raise ProjectToolError("file could not be read") from exc
        if "\x00" in text:
            raise ProjectToolError("binary files cannot be read")
        return text

    def _write_text(self, path: Path, content: str) -> None:
        encoded = content.encode("utf-8")
        if len(encoded) > _TEXT_FILE_MAX_BYTES:
            raise ProjectToolError("updated file exceeds the 1 MiB limit")
        try:
            path.write_bytes(encoded)
        except OSError as exc:
            raise ProjectToolError("file could not be written") from exc

    @staticmethod
    def _required_string(
        arguments: dict[str, object],
        key: str,
        *,
        strip: bool = True,
        allow_empty: bool = False,
    ) -> str:
        value = arguments.get(key)
        if not isinstance(value, str):
            raise ProjectToolError(f"{key} must be a string")
        normalized = value.strip() if strip else value
        if not normalized and not allow_empty:
            raise ProjectToolError(f"{key} cannot be empty")
        return normalized

    @staticmethod
    def _positive_int(value: object, *, default: int) -> int:
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ProjectToolError("offset and limit must be positive integers")
        return value

    def _display_path(self, path: Path) -> str:
        return path.relative_to(self.project_root).as_posix()

    @staticmethod
    def _redact_secrets(text: str) -> str:
        result = text
        for pattern in _SECRET_PATTERNS:
            result = pattern.sub("[redacted-secret]", result)
        return result

    def _status_line_is_sensitive(self, line: str) -> bool:
        path_text = line[3:].strip().strip('"') if len(line) > 3 else ""
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        try:
            return self._is_blocked((self.project_root / path_text).resolve(strict=False))
        except OSError:
            return True

    @staticmethod
    def _sanitized_subprocess_env() -> dict[str, str]:
        allowed = {
            "COMSPEC",
            "LOCALAPPDATA",
            "NUMBER_OF_PROCESSORS",
            "OS",
            "PATH",
            "PATHEXT",
            "PROCESSOR_ARCHITECTURE",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
        }
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    @staticmethod
    def _result(summary: str, content: str, *, truncated: bool = False) -> dict[str, object]:
        return {
            "summary": summary,
            "content": content,
            "truncated": truncated,
        }
