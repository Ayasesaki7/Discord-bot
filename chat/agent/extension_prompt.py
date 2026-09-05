from __future__ import annotations

import hashlib
from pathlib import Path


EXTENSION_PROMPT_RELATIVE_PATH = Path("config/agent/tool_guidance.md")
_REQUIRED_HEADER = "ATRI extension capability guidance"
_MAX_PROMPT_BYTES = 32 * 1024


class ExtensionPromptStore:
    """Hot-load a bounded Agent-maintained capability supplement.

    The core prompt remains in read-only source. A malformed update never
    replaces the last valid in-memory value, so declarative guidance cannot
    prevent the bot from starting or serving chat.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.path = self.project_root / EXTENSION_PROMPT_RELATIVE_PATH
        self._fingerprint = ""
        self._last_valid = ""
        self._last_error_fingerprint = ""

    def load(self) -> str:
        fingerprint = self._file_fingerprint()
        if fingerprint == self._fingerprint:
            return self._last_valid
        try:
            content = self._read_validated()
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            if fingerprint != self._last_error_fingerprint:
                print(
                    "[WARN] Agent extension guidance update was ignored: "
                    f"error_type={exc.__class__.__name__}"
                )
                self._last_error_fingerprint = fingerprint
            return self._last_valid
        self._fingerprint = fingerprint
        self._last_error_fingerprint = ""
        self._last_valid = content
        return content

    def _read_validated(self) -> str:
        if not self.path.is_file():
            return ""
        raw = self.path.read_bytes()
        if len(raw) > _MAX_PROMPT_BYTES:
            raise ValueError("extension guidance exceeds 32 KiB")
        if b"\x00" in raw:
            raise ValueError("extension guidance contains a NUL byte")
        content = raw.decode("utf-8").strip()
        if content and not content.startswith(_REQUIRED_HEADER):
            raise ValueError("extension guidance header is missing")
        return content

    def _file_fingerprint(self) -> str:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            return f"error:{exc.__class__.__name__}"
        return hashlib.sha256(raw).hexdigest()
