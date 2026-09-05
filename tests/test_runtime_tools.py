from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from chat.agent.runtime_tools import RuntimeDiagnosticHost, RuntimeToolError


class RuntimeDiagnosticHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_system_info_advertises_sandbox_without_environment_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = RuntimeDiagnosticHost(Path(temp_dir))

            result = host.system_info({})
            payload = json.loads(str(result["content"]))

            self.assertTrue(payload["system"])
            self.assertIn("python_version", payload["availableCommands"])
            self.assertFalse(payload["security"]["shell"])
            self.assertFalse(payload["security"]["customArguments"])
            self.assertFalse(payload["security"]["arbitraryPaths"])
            self.assertFalse(payload["security"]["environmentVariables"])

    async def test_log_reader_is_fixed_bounded_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            secret = "sk-this-is-a-private-runtime-key"
            (root / "bot.log").write_text(
                "old line\n"
                f"API_KEY={secret}\n"
                "new line\n",
                encoding="utf-8",
            )
            host = RuntimeDiagnosticHost(root)

            result = host.read_log({"stream": "bot", "tail_lines": 2})
            content = str(result["content"])

            self.assertNotIn("old line", content)
            self.assertNotIn(secret, content)
            self.assertIn("[redacted-secret]", content)
            self.assertIn("new line", content)

    async def test_log_reader_rejects_arbitrary_paths_and_excess_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = RuntimeDiagnosticHost(Path(temp_dir))

            with self.assertRaisesRegex(RuntimeToolError, "unsupported diagnostic parameters"):
                host.read_log(
                    {
                        "stream": "bot",
                        "file_path": "config/credentials/agent_code_api.json",
                    }
                )
            with self.assertRaisesRegex(RuntimeToolError, "bot, error, or both"):
                host.read_log({"stream": "../../outside"})

    async def test_command_rejects_shell_text_custom_argv_and_interpreter_eval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = RuntimeDiagnosticHost(Path(temp_dir))

            for command in (
                "cmd /c set",
                "bash -lc env",
                "python -c import os",
                "curl https://example.com",
            ):
                with self.assertRaisesRegex(RuntimeToolError, "unsupported or unavailable"):
                    await host.run_command({"command": command})
            with self.assertRaisesRegex(RuntimeToolError, "unsupported diagnostic parameters"):
                await host.run_command(
                    {
                        "command": "python_version",
                        "args": ["-c", "import os"],
                    }
                )

    async def test_fixed_python_version_probe_runs_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = RuntimeDiagnosticHost(Path(temp_dir))

            result = await host.run_command({"command": "python_version"})
            payload = json.loads(str(result["content"]))

            self.assertEqual(payload["command"], "python_version")
            self.assertEqual(payload["exitCode"], 0)
            self.assertIn(f"Python {sys.version_info.major}", payload["output"])

    async def test_internal_disk_memory_and_process_probes_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = RuntimeDiagnosticHost(Path(temp_dir))

            for command in ("disk", "memory", "process"):
                result = await host.run_command({"command": command})
                payload = json.loads(str(result["content"]))
                self.assertEqual(payload["command"], command)
                self.assertEqual(payload["exitCode"], 0)
                self.assertIsInstance(payload["result"], dict)


if __name__ == "__main__":
    unittest.main()
