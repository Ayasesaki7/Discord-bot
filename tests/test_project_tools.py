from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat.agent.project_tools import ProjectToolError, ProjectToolHost


class ProjectToolHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_credentials_and_paths_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            (root / ".env").write_text("TOKEN=private", encoding="utf-8")
            outside = Path(temp_dir) / "outside.py"
            outside.write_text("print('outside')", encoding="utf-8")
            host = ProjectToolHost(root)

            with self.assertRaises(ProjectToolError):
                await host.execute("read", {"file_path": ".env"})
            with self.assertRaises(ProjectToolError):
                await host.execute("read", {"file_path": str(outside)})

    async def test_read_redacts_secret_but_unrelated_edit_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "config" / "agent" / "settings.txt"
            source.parent.mkdir(parents=True)
            secret = "sk-this-is-a-private-example-key"
            source.write_text(
                f'API_KEY = "{secret}"\nVALUE = "before"\n',
                encoding="utf-8",
            )
            host = ProjectToolHost(root)

            read_result = await host.execute(
                "read",
                {"file_path": "config/agent/settings.txt"},
            )
            await host.execute(
                "edit",
                {
                    "file_path": "config/agent/settings.txt",
                    "old_string": 'VALUE = "before"',
                    "new_string": 'VALUE = "after"',
                },
            )

            self.assertNotIn(secret, str(read_result["content"]))
            updated = source.read_text(encoding="utf-8")
            self.assertIn(secret, updated)
            self.assertIn('VALUE = "after"', updated)

    async def test_create_refuses_overwrite_and_python_check_does_not_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            host = ProjectToolHost(root)
            marker = root / "executed.txt"
            source_text = (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('bad')\n"
            )

            await host.execute(
                "create",
                {
                    "file_path": "tools/agent/new_module.py",
                    "content": source_text,
                },
                source_creation_approved=True,
            )
            check_result = await host.execute(
                "check",
                {"file_paths": ["tools/agent/new_module.py"]},
            )

            self.assertIn("passed", str(check_result["summary"]).casefold())
            self.assertFalse(marker.exists())
            with self.assertRaises(ProjectToolError):
                await host.execute(
                    "create",
                    {
                        "file_path": "tools/agent/new_module.py",
                        "content": "replacement",
                    },
                    source_creation_approved=True,
                )

    async def test_core_is_readable_but_writes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            core = root / "chat" / "cog.py"
            core.parent.mkdir(parents=True)
            core.write_text("CORE = True\n", encoding="utf-8")
            host = ProjectToolHost(root)

            read_result = await host.execute("read", {"file_path": "chat/cog.py"})

            self.assertIn("CORE = True", str(read_result["content"]))
            with self.assertRaisesRegex(ProjectToolError, "core project files are read-only"):
                await host.execute(
                    "edit",
                    {
                        "file_path": "chat/cog.py",
                        "old_string": "True",
                        "new_string": "False",
                    },
                )
            with self.assertRaisesRegex(ProjectToolError, "core project files are read-only"):
                await host.execute(
                    "create",
                    {"file_path": "chat/new_tool.py", "content": "VALUE = 1\n"},
                )
            self.assertEqual(core.read_text(encoding="utf-8"), "CORE = True\n")

    async def test_sensitive_named_source_is_readable_but_inline_values_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "chat" / "credentials.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                'API_KEY = "sk-this-is-a-private-example-key"\nVALUE = "safe"\n',
                encoding="utf-8",
            )
            host = ProjectToolHost(root)

            result = await host.execute(
                "read",
                {"file_path": "chat/credentials.py"},
            )

            self.assertIn("VALUE", str(result["content"]))
            self.assertIn("[redacted-secret]", str(result["content"]))
            self.assertNotIn("sk-this-is-a-private-example-key", str(result["content"]))

    async def test_config_zone_rejects_executable_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = ProjectToolHost(Path(temp_dir))

            with self.assertRaisesRegex(ProjectToolError, "require one of"):
                await host.execute(
                    "create",
                    {
                        "file_path": "config/agent/unsafe.py",
                        "content": "print('should not be executable config')\n",
                    },
                )

    async def test_extension_guidance_edit_is_reported_as_hot_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            guidance = root / "config" / "agent" / "tool_guidance.md"
            guidance.parent.mkdir(parents=True)
            guidance.write_text(
                "ATRI extension capability guidance:\n- before\n",
                encoding="utf-8",
            )
            host = ProjectToolHost(root)

            result = await host.execute(
                "edit",
                {
                    "file_path": "config/agent/tool_guidance.md",
                    "old_string": "before",
                    "new_string": "after",
                },
            )

            self.assertIn("hot-load", str(result["content"]))
            self.assertIn("after", guidance.read_text(encoding="utf-8"))

    async def test_live_draw_and_fortune_tool_folders_are_writable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            draw_file = root / "tools" / "draw" / "settings.py"
            draw_file.parent.mkdir(parents=True)
            draw_file.write_text("VALUE = 'before'\n", encoding="utf-8")
            host = ProjectToolHost(root)

            await host.execute(
                "edit",
                {
                    "file_path": "tools/draw/settings.py",
                    "old_string": "before",
                    "new_string": "after",
                },
            )
            await host.execute(
                "create",
                {
                    "file_path": "tools/fortune/new_rule.json",
                    "content": '{"enabled": true}\n',
                },
            )

            self.assertIn("after", draw_file.read_text(encoding="utf-8"))
            self.assertTrue((root / "tools" / "fortune" / "new_rule.json").is_file())

    async def test_new_executable_tool_source_requires_owner_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = ProjectToolHost(Path(temp_dir))

            with self.assertRaisesRegex(ProjectToolError, "owner confirmation"):
                await host.execute(
                    "create",
                    {
                        "file_path": "tools/agent/authored.py",
                        "content": "VALUE = 1\n",
                    },
                )

            await host.execute(
                "create",
                {
                    "file_path": "tools/agent/authored.py",
                    "content": "VALUE = 1\n",
                },
                source_creation_approved=True,
            )

    async def test_search_skips_generated_and_sensitive_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "chat").mkdir()
            (root / "chat" / "main.py").write_text("privacy_marker = True\n", encoding="utf-8")
            (root / "node_modules" / "package").mkdir(parents=True)
            (root / "node_modules" / "package" / "index.js").write_text(
                "privacy_marker\n",
                encoding="utf-8",
            )
            host = ProjectToolHost(root)

            result = await host.execute("search", {"query": "privacy_marker"})

            self.assertIn("chat/main.py", str(result["content"]))
            self.assertNotIn("node_modules", str(result["content"]))

    async def test_bounded_list_shows_structure_without_sensitive_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "tools" / "draw").mkdir(parents=True)
            (root / "tools" / "draw" / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "node_modules" / "package").mkdir(parents=True)
            (root / "node_modules" / "package" / "secret.js").write_text("secret\n", encoding="utf-8")
            host = ProjectToolHost(root)

            result = await host.execute("list", {"directory": ".", "max_depth": 3})

            self.assertIn("tools/draw/agent.py", str(result["content"]))
            self.assertNotIn("node_modules", str(result["content"]))


if __name__ == "__main__":
    unittest.main()
