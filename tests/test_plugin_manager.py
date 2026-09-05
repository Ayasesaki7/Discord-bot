from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from chat.agent.plugin_manager import PluginManager, PluginManagerError


def npm_archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
    return output.getvalue()


class PluginManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_official_package_is_included_before_search_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = PluginManager(Path(temp_dir))
            manager._fetch_metadata = AsyncMock(
                return_value={
                    "dist-tags": {"latest": "1.2.3"},
                    "description": "official tool",
                }
            )
            class FakeResponse:
                status = 200

                async def json(self, **_kwargs):
                    return {"objects": []}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

            class FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

                def get(self, *_args, **_kwargs):
                    return FakeResponse()

            with patch(
                "chat.agent.plugin_manager.aiohttp.ClientSession",
                return_value=FakeSession(),
            ):
                result = await manager.search("@deepseek-ai/dsh-tool-example", limit=5)

            payload = json.loads(result["content"])
            self.assertEqual(payload[0]["name"], "@deepseek-ai/dsh-tool-example")
            self.assertTrue(payload[0]["exactMatch"])
    def test_quarantine_audit_detects_lifecycle_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = PluginManager(root)
            archive = npm_archive(
                {
                    "package/package.json": json.dumps(
                        {
                            "name": "example-plugin",
                            "version": "1.0.0",
                            "scripts": {"postinstall": "node setup.js", "test": "noop"},
                        }
                    ).encode(),
                    "package/index.js": b"export const name = 'example'\n",
                }
            )

            audit = manager._extract_and_audit(archive, root / "unpacked")

            self.assertEqual(audit["lifecycleScripts"], ["postinstall"])
            self.assertFalse(audit["executed"])
            self.assertTrue((root / "unpacked" / "index.js").is_file())

    def test_quarantine_rejects_archive_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = PluginManager(Path(temp_dir))
            archive = npm_archive({"package/../escape.js": b"bad"})

            with self.assertRaisesRegex(PluginManagerError, "unsafe path"):
                manager._extract_and_audit(archive, Path(temp_dir) / "unpacked")

    async def test_elevated_native_plugin_is_never_activated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = PluginManager(Path(temp_dir))

            with self.assertRaisesRegex(PluginManagerError, "elevated capability"):
                await manager.activate_native_plugin(
                    "@deepseek-ai/dsh-tool-bash",
                    version="0.1.0-rc.6",
                )

    def test_native_config_is_bounded_and_cannot_store_secrets(self) -> None:
        safe = PluginManager._validate_native_config(
            {"allowParallelInProgress": False, "nested": {"limit": 2}}
        )

        self.assertEqual(safe["nested"], {"limit": 2})
        with self.assertRaisesRegex(PluginManagerError, "secret-like key"):
            PluginManager._validate_native_config({"apiKey": "private"})
        with self.assertRaisesRegex(PluginManagerError, "16 KiB"):
            PluginManager._validate_native_config({"description": "x" * 17000})


if __name__ == "__main__":
    unittest.main()
