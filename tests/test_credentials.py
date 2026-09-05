from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat.agent.credentials import CredentialUpdateError, ServiceCredentialStore
from chat.agent.project_tools import ProjectToolError, ProjectToolHost


class ServiceCredentialStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_fixed_protected_file_without_read_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ServiceCredentialStore(root)

            result = await store.update(
                "qqmusic",
                "uin=fake-user; qm_keyst=fake-key",
            )

            target = root / "config" / "credentials" / "qqmusic_cookie.txt"
            self.assertTrue(target.is_file())
            self.assertNotIn("fake-key", str(result))
            self.assertFalse(hasattr(store, "read"))

    async def test_cookie_header_is_converted_for_netscape_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ServiceCredentialStore(root)

            await store.update("douyin", "sessionid=fake-session; sid_tt=fake-sid")

            text = (
                root / "config" / "credentials" / "douyin_cookies.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("# Netscape HTTP Cookie File", text)
            self.assertIn(".douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\t", text)

    async def test_rejects_unknown_service_and_empty_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ServiceCredentialStore(Path(temp_dir))

            with self.assertRaises(CredentialUpdateError):
                await store.update("unknown", "a=b")
            with self.assertRaises(CredentialUpdateError):
                await store.update("bilibili", "")

    async def test_general_project_tools_cannot_read_or_edit_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "config" / "credentials" / "douyin_cookies.txt"
            target.parent.mkdir(parents=True)
            target.write_text("sessionid=fake-private", encoding="utf-8")
            host = ProjectToolHost(root)

            with self.assertRaises(ProjectToolError):
                await host.execute(
                    "read",
                    {"file_path": "config/credentials/douyin_cookies.txt"},
                )
            with self.assertRaises(ProjectToolError):
                await host.execute(
                    "edit",
                    {
                        "file_path": "config/credentials/douyin_cookies.txt",
                        "old_string": "fake-private",
                        "new_string": "replacement",
                    },
                )

            api_settings = target.parent / "agent_code_api.json"
            api_settings.write_text(
                '{"api_key": "fake-private-api-key"}',
                encoding="utf-8",
            )
            with self.assertRaises(ProjectToolError):
                await host.execute(
                    "read",
                    {"file_path": "config/credentials/agent_code_api.json"},
                )


if __name__ == "__main__":
    unittest.main()
