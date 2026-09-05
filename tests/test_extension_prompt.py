from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chat.agent.extension_prompt import ExtensionPromptStore
from chat.cog import AtriChat


class ExtensionPromptStoreTests(unittest.TestCase):
    def test_valid_file_is_hot_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "config" / "agent" / "tool_guidance.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "ATRI extension capability guidance (host-loaded):\n- tool one\n",
                encoding="utf-8",
            )
            store = ExtensionPromptStore(root)

            first = store.load()
            path.write_text(
                "ATRI extension capability guidance (host-loaded):\n- tool two\n",
                encoding="utf-8",
            )
            second = store.load()

            self.assertIn("tool one", first)
            self.assertIn("tool two", second)

    def test_malformed_update_falls_back_to_last_valid_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "config" / "agent" / "tool_guidance.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "ATRI extension capability guidance:\n- safe tool\n",
                encoding="utf-8",
            )
            store = ExtensionPromptStore(root)
            valid = store.load()
            path.write_text("missing required header\n", encoding="utf-8")

            with patch("builtins.print"):
                fallback = store.load()

            self.assertEqual(fallback, valid)

    def test_cog_wraps_extension_as_host_content(self) -> None:
        cog = object.__new__(AtriChat)
        cog.extension_prompt_store = SimpleNamespace(
            load=lambda: "ATRI extension capability guidance:\n- test_tool"
        )

        prompt = cog._extension_capability_prompt()

        self.assertIn("host-loaded", prompt)
        self.assertIn("test_tool", prompt)


class MaintenanceFallbackReportTests(unittest.TestCase):
    def test_successful_credential_write_is_not_misreported_as_total_failure(self) -> None:
        report = AtriChat._maintenance_host_fallback_report(
            [
                {
                    "action": "credential_update",
                    "summary": "Updated the protected QQ Music credential file.",
                }
            ],
            dsh_detail="dsh protocol stream ended unexpectedly",
            guidance_pending=False,
        )

        self.assertIn("宿主已经确认", report)
        self.assertIn("credential_update", report)
        self.assertNotIn("Cookie=", report)


if __name__ == "__main__":
    unittest.main()
