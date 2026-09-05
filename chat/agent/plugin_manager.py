from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp

from ..tls import build_verified_connector


_PACKAGE_RE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$",
    re.IGNORECASE,
)
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 60 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 2500
_LIFECYCLE_SCRIPTS = {
    "preinstall",
    "install",
    "postinstall",
    "prepare",
    "prepack",
    "postpack",
}
_BLOCKED_NATIVE_PLUGIN_PARTS = {
    "bash",
    "cordis",
    "fs",
    "jobs",
    "pwsh",
    "ralph",
    "skill",
    "str-replace",
    "subagent",
}
_SENSITIVE_CONFIG_KEY_RE = re.compile(
    r"(?:^|[_-])(api[_-]?key|authorization|cookie|credential|password|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_MAX_NATIVE_CONFIG_BYTES = 16 * 1024
_DEFAULT_NATIVE_CONFIGS: dict[str, dict[str, object]] = {
    # This plugin intentionally has no library default. ATRI executes one task
    # at a time per DSH session, so the single-in-progress policy is correct.
    "@deepseek-ai/dsh-tool-todo": {"allowParallelInProgress": False},
}


class PluginManagerError(RuntimeError):
    pass


class PluginManager:
    """Search and unpack npm plugins without executing third-party code.

    Downloads are installed into a quarantine directory outside all Agent
    readable/writable roots. Activation is deliberately a separate,
    owner-reviewed operation because a Cordis plugin runs in the DSH process
    and can access that process's environment.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.quarantine_root = self.project_root / "plugin_staging" / "installed"
        self.runtime_root = self.project_root / "agent_runtime" / "dsh"
        self.native_manifest_path = self.runtime_root / "native-plugins.json"
        self._activation_lock = asyncio.Lock()

    async def search(self, query: str, *, limit: int = 8) -> dict[str, object]:
        normalized = " ".join(str(query or "").split())
        if not normalized or len(normalized) > 160:
            raise PluginManagerError("plugin search query must contain 1 to 160 characters")
        safe_limit = min(max(int(limit), 1), 12)
        results: list[dict[str, object]] = []
        seen_names: set[str] = set()
        if self._valid_package_name(normalized):
            try:
                exact_metadata = await self._fetch_metadata(normalized)
            except PluginManagerError:
                exact_metadata = None
            if isinstance(exact_metadata, dict):
                dist_tags = exact_metadata.get("dist-tags")
                exact_version = (
                    str(dist_tags.get("latest") or "")
                    if isinstance(dist_tags, dict)
                    else ""
                )
                if exact_version:
                    results.append(
                        {
                            "name": normalized,
                            "version": exact_version,
                            "description": str(exact_metadata.get("description") or "")[:300],
                            "officialDeepSeek": normalized.startswith("@deepseek-ai/dsh-"),
                            "npmUrl": f"https://www.npmjs.com/package/{quote(normalized, safe='@/')}",
                            "exactMatch": True,
                        }
                    )
                    seen_names.add(normalized.casefold())

        timeout = aiohttp.ClientTimeout(total=20)
        params = {"text": normalized, "size": str(safe_limit * 2)}
        connector = build_verified_connector()
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(
                "https://registry.npmjs.org/-/v1/search",
                params=params,
                headers={"User-Agent": "ATRI-Plugin-Discovery/1.0"},
            ) as response:
                if response.status >= 400:
                    raise PluginManagerError(
                        f"npm plugin search failed with status {response.status}"
                    )
                payload = await response.json(content_type=None)
        objects = payload.get("objects") if isinstance(payload, dict) else None
        if isinstance(objects, list):
            for item in objects:
                package = item.get("package") if isinstance(item, dict) else None
                if not isinstance(package, dict):
                    continue
                name = str(package.get("name") or "").strip()
                version = str(package.get("version") or "").strip()
                if (
                    not self._valid_package_name(name)
                    or not version
                    or name.casefold() in seen_names
                ):
                    continue
                results.append(
                    {
                        "name": name,
                        "version": version,
                        "description": str(package.get("description") or "")[:300],
                        "officialDeepSeek": name.startswith("@deepseek-ai/dsh-"),
                        "npmUrl": f"https://www.npmjs.com/package/{quote(name, safe='@/')}",
                        "exactMatch": False,
                    }
                )
                seen_names.add(name.casefold())
                if len(results) >= safe_limit:
                    break
        results.sort(
            key=lambda item: (
                not bool(item.get("exactMatch")),
                not bool(item["officialDeepSeek"]),
                str(item["name"]),
            )
        )
        results = results[:safe_limit]
        return {
            "summary": f"Found {len(results)} npm plugin candidates; official DSH packages are first.",
            "content": json.dumps(results, ensure_ascii=False),
            "truncated": len(results) >= safe_limit,
        }

    async def install_quarantined(
        self,
        package_name: str,
        *,
        version: str = "latest",
    ) -> dict[str, object]:
        name = str(package_name or "").strip()
        requested_version = str(version or "latest").strip() or "latest"
        if not self._valid_package_name(name):
            raise PluginManagerError("invalid npm package name")
        metadata = await self._fetch_metadata(name)
        versions = metadata.get("versions") if isinstance(metadata, dict) else None
        dist_tags = metadata.get("dist-tags") if isinstance(metadata, dict) else None
        resolved_version = requested_version
        if requested_version == "latest":
            resolved_version = (
                str(dist_tags.get("latest") or "")
                if isinstance(dist_tags, dict)
                else ""
            )
        version_payload = (
            versions.get(resolved_version)
            if isinstance(versions, dict)
            else None
        )
        if not resolved_version or not isinstance(version_payload, dict):
            raise PluginManagerError("requested npm plugin version does not exist")
        dist = version_payload.get("dist")
        if not isinstance(dist, dict):
            raise PluginManagerError("npm plugin metadata has no distribution archive")
        tarball_url = str(dist.get("tarball") or "")
        parsed_url = urlparse(tarball_url)
        if parsed_url.scheme != "https" or parsed_url.hostname not in {
            "registry.npmjs.org",
            "registry.npmjs.com",
        }:
            raise PluginManagerError("npm plugin archive host is not trusted")
        archive = await self._download_archive(tarball_url)
        self._verify_integrity(archive, str(dist.get("integrity") or ""))

        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
        target = self.quarantine_root / safe_name / resolved_version
        if target.is_dir():
            audit = self._read_audit(target)
            return self._result(name, resolved_version, target, audit, already_installed=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix="plugin-", dir=str(target.parent))
        )
        try:
            audit = self._extract_and_audit(archive, temporary)
            os.replace(temporary, target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return self._result(name, resolved_version, target, audit, already_installed=False)

    async def activate_native_plugin(
        self,
        package_name: str,
        *,
        version: str,
        config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        name = str(package_name or "").strip()
        exact_version = str(version or "").strip()
        if not name.startswith("@deepseek-ai/dsh-") or not self._valid_package_name(name):
            raise PluginManagerError("only official @deepseek-ai/dsh-* plugins can be activated")
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]*", exact_version):
            raise PluginManagerError("an exact audited plugin version is required")
        lowered = name.casefold()
        blocked_part = next(
            (part for part in _BLOCKED_NATIVE_PLUGIN_PARTS if part in lowered),
            None,
        )
        if blocked_part is not None:
            raise PluginManagerError(
                f"native plugin {name} requests elevated capability ({blocked_part}); "
                "automatic activation is blocked"
            )
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
        quarantine = self.quarantine_root / safe_name / exact_version
        audit = self._read_audit(quarantine)
        if audit.get("lifecycleScripts"):
            raise PluginManagerError(
                "plugin declares install lifecycle scripts; automatic activation is blocked"
            )
        safe_config = self._validate_native_config(
            config if config is not None else _DEFAULT_NATIVE_CONFIGS.get(name, {})
        )
        npm = shutil.which("npm")
        if not npm:
            raise PluginManagerError("npm is required to activate a DSH plugin")

        async with self._activation_lock:
            process = await asyncio.create_subprocess_exec(
                npm,
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--save-exact",
                f"{name}@{exact_version}",
                cwd=str(self.runtime_root),
                env=self._sanitized_npm_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[-1200:]
                raise PluginManagerError(f"npm plugin activation failed: {detail}")
            entries = self._load_native_manifest()
            entry_id = "native-" + re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
            replacement = {
                "id": entry_id[:80],
                "name": name,
                "config": safe_config,
            }
            entries = [entry for entry in entries if entry.get("id") != replacement["id"]]
            entries.append(replacement)
            self._save_native_manifest(entries)
        output_tail = stdout.decode("utf-8", errors="replace")[-500:].strip()
        return {
            "summary": (
                f"Activated official DSH plugin {name}@{exact_version} in the native "
                "plugin manifest. The loader may hot-load it; otherwise restart the bot."
            ),
            "content": json.dumps(
                {
                    "name": name,
                    "version": exact_version,
                    "config": safe_config,
                    "manifest": self.native_manifest_path.relative_to(self.project_root).as_posix(),
                    "npm": output_tail,
                },
                ensure_ascii=False,
            ),
            "truncated": False,
        }

    def _load_native_manifest(self) -> list[dict[str, object]]:
        if not self.native_manifest_path.is_file():
            return []
        try:
            payload = json.loads(self.native_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginManagerError("native DSH plugin manifest is invalid") from exc
        if not isinstance(payload, list):
            raise PluginManagerError("native DSH plugin manifest must be an array")
        return [dict(item) for item in payload if isinstance(item, dict)]

    @classmethod
    def _validate_native_config(cls, config: dict[str, object]) -> dict[str, object]:
        if not isinstance(config, dict):
            raise PluginManagerError("native plugin config must be a JSON object")

        def check_keys(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key, str):
                        raise PluginManagerError("native plugin config keys must be strings")
                    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
                    if _SENSITIVE_CONFIG_KEY_RE.search(normalized):
                        raise PluginManagerError(
                            f"native plugin config may not contain secret-like key {key!r}"
                        )
                    check_keys(child)
            elif isinstance(value, list):
                for child in value:
                    check_keys(child)

        check_keys(config)
        try:
            serialized = json.dumps(
                config,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise PluginManagerError("native plugin config must contain JSON values") from exc
        if len(serialized.encode("utf-8")) > _MAX_NATIVE_CONFIG_BYTES:
            raise PluginManagerError("native plugin config exceeds 16 KiB")
        # Detach the caller's data before storing it in the loader manifest.
        return json.loads(serialized)

    def _save_native_manifest(self, entries: list[dict[str, object]]) -> None:
        self.native_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.native_manifest_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.native_manifest_path)

    async def _fetch_metadata(self, package_name: str) -> dict[str, object]:
        timeout = aiohttp.ClientTimeout(total=20)
        url = f"https://registry.npmjs.org/{quote(package_name, safe='')}"
        connector = build_verified_connector()
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(
                url,
                headers={"User-Agent": "ATRI-Plugin-Discovery/1.0"},
            ) as response:
                if response.status == 404:
                    raise PluginManagerError("npm plugin package was not found")
                if response.status >= 400:
                    raise PluginManagerError(
                        f"npm plugin metadata failed with status {response.status}"
                    )
                payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise PluginManagerError("npm plugin metadata is invalid")
        return payload

    async def _download_archive(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=60)
        chunks: list[bytes] = []
        size = 0
        connector = build_verified_connector()
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(
                url,
                headers={"User-Agent": "ATRI-Plugin-Discovery/1.0"},
            ) as response:
                if response.status >= 400:
                    raise PluginManagerError(
                        f"npm plugin download failed with status {response.status}"
                    )
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > _MAX_ARCHIVE_BYTES:
                        raise PluginManagerError("npm plugin archive exceeds 20 MiB")
                    chunks.append(chunk)
        return b"".join(chunks)

    def _extract_and_audit(self, archive: bytes, destination: Path) -> dict[str, object]:
        total_size = 0
        file_count = 0
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise PluginManagerError("npm plugin archive contains too many files")
            for member in members:
                parts = Path(member.name).parts
                if not parts or parts[0] != "package" or any(part in {"", ".", ".."} for part in parts):
                    raise PluginManagerError("npm plugin archive contains an unsafe path")
                if member.issym() or member.islnk() or member.isdev():
                    raise PluginManagerError("npm plugin archive contains an unsafe link or device")
                relative_parts = parts[1:]
                if not relative_parts:
                    continue
                output = destination.joinpath(*relative_parts)
                resolved_output = output.resolve(strict=False)
                try:
                    resolved_output.relative_to(destination.resolve())
                except ValueError as exc:
                    raise PluginManagerError("npm plugin archive escaped quarantine") from exc
                if member.isdir():
                    resolved_output.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                total_size += max(member.size, 0)
                file_count += 1
                if total_size > _MAX_EXTRACTED_BYTES:
                    raise PluginManagerError("npm plugin expands beyond 60 MiB")
                source = bundle.extractfile(member)
                if source is None:
                    raise PluginManagerError("npm plugin archive contains an unreadable file")
                resolved_output.parent.mkdir(parents=True, exist_ok=True)
                resolved_output.write_bytes(source.read())

        package_json_path = destination / "package.json"
        if not package_json_path.is_file():
            raise PluginManagerError("npm plugin archive has no package.json")
        try:
            package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginManagerError("npm plugin package.json is invalid") from exc
        scripts = package_json.get("scripts") if isinstance(package_json, dict) else None
        lifecycle_scripts = sorted(
            key
            for key in (scripts.keys() if isinstance(scripts, dict) else [])
            if key.casefold() in _LIFECYCLE_SCRIPTS
        )
        audit = {
            "fileCount": file_count,
            "expandedBytes": total_size,
            "lifecycleScripts": lifecycle_scripts,
            "requiresOwnerActivation": True,
            "executed": False,
        }
        (destination / "ATRI_QUARANTINE_AUDIT.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return audit

    def _read_audit(self, target: Path) -> dict[str, object]:
        try:
            payload = json.loads(
                (target / "ATRI_QUARANTINE_AUDIT.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginManagerError("quarantined plugin audit is unreadable") from exc
        if not isinstance(payload, dict):
            raise PluginManagerError("quarantined plugin audit is invalid")
        return payload

    def _result(
        self,
        name: str,
        version: str,
        target: Path,
        audit: dict[str, object],
        *,
        already_installed: bool,
    ) -> dict[str, object]:
        relative = target.relative_to(self.project_root).as_posix()
        content = {
            "name": name,
            "version": version,
            "officialDeepSeek": name.startswith("@deepseek-ai/dsh-"),
            "quarantinePath": relative,
            "alreadyInstalled": already_installed,
            **audit,
        }
        return {
            "summary": (
                f"Downloaded and unpacked {name}@{version} into quarantine; "
                "nothing was executed and owner activation is still required."
            ),
            "content": json.dumps(content, ensure_ascii=False),
            "truncated": False,
        }

    @staticmethod
    def _verify_integrity(archive: bytes, integrity: str) -> None:
        if not integrity:
            raise PluginManagerError("npm plugin has no integrity hash")
        algorithm, separator, expected = integrity.partition("-")
        if not separator or algorithm.casefold() != "sha512":
            raise PluginManagerError("npm plugin does not provide sha512 integrity")
        actual = base64.b64encode(hashlib.sha512(archive).digest()).decode("ascii")
        if actual != expected:
            raise PluginManagerError("npm plugin integrity verification failed")

    @staticmethod
    def _valid_package_name(name: str) -> bool:
        return bool(name and len(name) <= 214 and _PACKAGE_RE.fullmatch(name))

    @staticmethod
    def _sanitized_npm_env() -> dict[str, str]:
        allowed = {
            "APPDATA",
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
