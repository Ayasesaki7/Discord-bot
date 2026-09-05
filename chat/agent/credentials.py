from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path


_MAX_CREDENTIAL_BYTES = 1024 * 1024


class CredentialUpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CredentialSpec:
    filename: str
    domain: str | None
    label: str


_SPECS = {
    "qqmusic": CredentialSpec("qqmusic_cookie.txt", None, "QQ Music"),
    "bilibili": CredentialSpec("bilibili_cookies.txt", ".bilibili.com", "Bilibili"),
    "douyin": CredentialSpec("douyin_cookies.txt", ".douyin.com", "Douyin"),
}


class ServiceCredentialStore:
    """Owner-maintenance write path for service cookies.

    This service deliberately exposes no read operation. General project tools
    also block the directory and secret-like filenames, so an Agent can replace
    an owner-supplied value but cannot retrieve the existing credential.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.root = self.project_root / "config" / "credentials"
        self._lock = asyncio.Lock()

    async def update(self, service: str, content: str) -> dict[str, object]:
        normalized_service = str(service or "").strip().casefold()
        spec = _SPECS.get(normalized_service)
        if spec is None:
            raise CredentialUpdateError(
                "service must be one of: qqmusic, bilibili, douyin"
            )
        normalized = self._normalize_content(spec, content)
        encoded = normalized.encode("utf-8")
        if len(encoded) > _MAX_CREDENTIAL_BYTES:
            raise CredentialUpdateError("credential content exceeds 1 MiB")

        target = self.root / spec.filename
        async with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            try:
                temporary.write_bytes(encoded)
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        return {
            "summary": f"Updated the protected {spec.label} credential file.",
            "content": json.dumps(
                {
                    "service": normalized_service,
                    "bytesWritten": len(encoded),
                    "readbackAllowed": False,
                    "runtimeReloadRequested": True,
                },
                ensure_ascii=False,
            ),
            "truncated": False,
        }

    @classmethod
    def _normalize_content(cls, spec: CredentialSpec, content: str) -> str:
        raw = str(content or "").lstrip("\ufeff").strip()
        if not raw:
            raise CredentialUpdateError("credential content cannot be empty")
        if "\x00" in raw:
            raise CredentialUpdateError("credential content contains a NUL byte")
        if spec.domain is None:
            if "=" not in raw:
                raise CredentialUpdateError("QQ Music cookie must contain name=value pairs")
            return raw + "\n"

        lines = raw.splitlines()
        if any(len(line.split("\t")) >= 7 for line in lines):
            return raw + "\n"

        converted = cls._cookie_header_to_netscape(raw, domain=spec.domain)
        if converted is None:
            raise CredentialUpdateError(
                f"{spec.label} credential must be a Netscape cookies.txt export "
                "or a Cookie header containing name=value pairs"
            )
        return converted

    @staticmethod
    def _cookie_header_to_netscape(raw: str, *, domain: str) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        if not cookie:
            return None
        lines = ["# Netscape HTTP Cookie File"]
        for name, morsel in cookie.items():
            value = morsel.value
            if not name or not value:
                continue
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
        if len(lines) == 1:
            return None
        return "\n".join(lines) + "\n"
