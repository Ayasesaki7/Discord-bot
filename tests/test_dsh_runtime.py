from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from chat.agent.dsh_runtime import (
    DshJsonRpcProcess,
    DshRuntimeError,
    DshRuntimeTemplate,
    DshTenantRuntimePool,
    describe_dsh_error,
)
from chat.agent.privacy import ConversationScope, PrivacyBoundary


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_RUNTIME = PROJECT_ROOT / "tests" / "fixtures" / "fake_dsh_runtime.py"
SECRET = b"test-only-agent-session-secret-32-bytes-long"


def make_template() -> DshRuntimeTemplate:
    return DshRuntimeTemplate(
        launch_args=(sys.executable, str(FAKE_RUNTIME)),
        cordis_path=FAKE_RUNTIME,
        provider="test-provider",
        model="test-model",
        persona="test persona",
        request_timeout_seconds=5.0,
    )


class DshJsonRpcProcessTests(unittest.IsolatedAsyncioTestCase):
    def test_error_description_redacts_credentials(self) -> None:
        detail = describe_dsh_error(
            DshRuntimeError(
                "request failed Authorization=Bearer secret-token-value-12345 "
                "api_key=sk-private-example-key-123456789"
            )
        )

        self.assertNotIn("secret-token-value", detail)
        self.assertNotIn("sk-private-example", detail)
        self.assertIn("[redacted-secret]", detail)

    def test_code_template_requires_its_own_api_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "agent_runtime" / "dsh"
            bin_path = (
                runtime_root
                / "node_modules"
                / "@deepseek-ai"
                / "dsh-sdk-jsonrpc-demo"
                / "lib"
                / "bin.js"
            )
            bin_path.parent.mkdir(parents=True)
            bin_path.write_text("", encoding="utf-8")
            (runtime_root / "cordis-code.yml").write_text("[]\n", encoding="utf-8")
            base_env = {
                "OPENAI_BASE_URL": "https://chat.example/v1",
                "OPENAI_API_KEY": "chat-key",
                "OPENAI_MODEL": "chat-model",
                "ATRI_AGENT_CODE_BASE_URL": "",
                "ATRI_AGENT_CODE_API_KEY": "",
                "ATRI_AGENT_CODE_MODEL": "",
            }
            with patch.dict("os.environ", base_env, clear=False):
                with self.assertRaisesRegex(DshRuntimeError, "ATRI_AGENT_CODE_MODEL"):
                    DshRuntimeTemplate.from_project_env(
                        project_root=root,
                        persona="code",
                        cordis_filename="cordis-code.yml",
                        api_env_prefix="ATRI_AGENT_CODE",
                    )

                with patch.dict(
                    "os.environ",
                    {
                        "ATRI_AGENT_CODE_BASE_URL": "https://code.example/v1",
                        "ATRI_AGENT_CODE_API_KEY": "code-key",
                        "ATRI_AGENT_CODE_MODEL": "code-model",
                    },
                    clear=False,
                ):
                    template = DshRuntimeTemplate.from_project_env(
                        project_root=root,
                        persona="code",
                        cordis_filename="cordis-code.yml",
                        api_env_prefix="ATRI_AGENT_CODE",
                    )

            self.assertEqual(template.model, "code-model")

    def test_code_template_accepts_protected_file_environment_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "agent_runtime" / "dsh"
            bin_path = (
                runtime_root
                / "node_modules"
                / "@deepseek-ai"
                / "dsh-sdk-jsonrpc-demo"
                / "lib"
                / "bin.js"
            )
            bin_path.parent.mkdir(parents=True)
            bin_path.write_text("", encoding="utf-8")
            (runtime_root / "cordis-code.yml").write_text("[]\n", encoding="utf-8")

            template = DshRuntimeTemplate.from_project_env(
                project_root=root,
                persona="code",
                cordis_filename="cordis-code.yml",
                api_env_prefix="ATRI_AGENT_CODE",
                env_overrides={
                    "ATRI_AGENT_CODE_BASE_URL": "https://hot.example/v1",
                    "ATRI_AGENT_CODE_API_KEY": "protected-key",
                    "ATRI_AGENT_CODE_MODEL": "hot-model",
                },
            )

            self.assertEqual(template.model, "hot-model")
            self.assertEqual(
                template.extra_env["ATRI_AGENT_CODE_BASE_URL"],
                "https://hot.example/v1",
            )
            self.assertNotIn("protected-key", repr(template))

    async def test_process_streams_and_returns_final_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            deltas: list[str] = []
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            try:
                result = await runtime.run_turn(
                    session_id="s_0123456789abcdef0123456789abcdef01234567",
                    text="private hello",
                    on_text_delta=deltas.append,
                )
            finally:
                await runtime.close()

            self.assertEqual(result.final_response, "echo:private hello")
            self.assertEqual(result.finish_reason, "completed")
            self.assertEqual(result.input_tokens, 12)
            self.assertEqual(result.output_tokens, 4)
            self.assertEqual(deltas, ["echo:private hello"])

    async def test_idle_status_does_not_complete_before_turn_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            try:
                result = await runtime.run_turn(
                    session_id="s_0123456789abcdef0123456789abcdef01234567",
                    text="idle-before-turn-end",
                )
            finally:
                await runtime.close()

        self.assertEqual(result.final_response, "echo:idle-before-turn-end")
        self.assertEqual(result.finish_reason, "completed")

    async def test_cached_input_tokens_are_included_in_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            try:
                result = await runtime.run_turn(
                    session_id="s_0123456789abcdef0123456789abcdef01234567",
                    text="cached-usage",
                )
            finally:
                await runtime.close()

        self.assertEqual(result.input_tokens, 35)
        self.assertEqual(result.output_tokens, 4)

    async def test_tool_loop_usage_reports_context_pressure_and_visible_output_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            try:
                result = await runtime.run_turn(
                    session_id="s_0123456789abcdef0123456789abcdef01234567",
                    text="tool-loop-usage",
                )
            finally:
                await runtime.close()

        self.assertEqual(result.input_tokens, 120)
        self.assertEqual(result.output_tokens, 10)

    async def test_zero_final_output_usage_is_left_for_visible_text_estimation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            try:
                result = await runtime.run_turn(
                    session_id="s_0123456789abcdef0123456789abcdef01234567",
                    text="missing-output-usage",
                )
            finally:
                await runtime.close()

        self.assertEqual(result.input_tokens, 12)
        self.assertIsNone(result.output_tokens)

    async def test_active_turn_can_be_cancelled_and_session_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            session_id = "s_0123456789abcdef0123456789abcdef01234567"
            hanging = asyncio.create_task(
                runtime.run_turn(
                    session_id=session_id,
                    text="hang-until-cancelled",
                )
            )
            try:
                for _attempt in range(100):
                    if session_id in runtime._active_turns:
                        break
                    await asyncio.sleep(0.01)
                hanging.cancel()
                cancelled = await runtime.cancel_turn(session_id)
                await asyncio.gather(hanging, return_exceptions=True)
                resumed = await runtime.run_turn(
                    session_id=session_id,
                    text="after-cancel",
                )
            finally:
                await runtime.close()

        self.assertTrue(cancelled)
        self.assertEqual(resumed.final_response, "echo:after-cancel")

    async def test_wedged_session_is_recovered_without_rotating_history_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            session_id = "s_0123456789abcdef0123456789abcdef01234567"
            try:
                with self.assertRaisesRegex(DshRuntimeError, "simulated wedged session"):
                    await runtime.run_turn(
                        session_id=session_id,
                        text="fail-once-until-cancel",
                    )
                recovery = await runtime.recover_session(session_id)
                resumed = await runtime.run_turn(
                    session_id=session_id,
                    text="fail-once-until-cancel",
                )
            finally:
                await runtime.close()

        self.assertEqual(recovery, "session_recycled")
        self.assertEqual(resumed.session_id, session_id)
        self.assertEqual(resumed.final_response, "echo:fail-once-until-cancel")

    async def test_pool_uses_one_process_per_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            privacy = PrivacyBoundary(secret=SECRET, session_root=Path(temp_dir))
            private = privacy.bind(
                ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=200,
                    user_id=300,
                )
            )
            public = privacy.bind(
                ConversationScope.from_discord_ids(
                    guild_id=101,
                    channel_id=201,
                    user_id=300,
                )
            )
            pool = DshTenantRuntimePool(template=make_template(), privacy=privacy)
            try:
                first = await pool.run_turn(private, text="private")
                second = await pool.run_turn(public, text="public")
                self.assertEqual(len(pool._runtimes), 2)
                roots = {runtime.session_root for runtime in pool._runtimes.values()}
                self.assertEqual(len(roots), 2)
            finally:
                await pool.close()

            self.assertEqual(first.final_response, "echo:private")
            self.assertEqual(second.final_response, "echo:public")

    async def test_runtime_environment_is_frozen_after_first_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            privacy = PrivacyBoundary(secret=SECRET, session_root=Path(temp_dir))
            context = privacy.bind(
                ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=200,
                    user_id=300,
                )
            )
            pool = DshTenantRuntimePool(template=make_template(), privacy=privacy)
            pool.configure_runtime_env({"ATRI_AGENT_TOOL_ENDPOINT": "http://127.0.0.1:1"})
            try:
                await pool.run_turn(context, text="hello")
                with self.assertRaises(DshRuntimeError):
                    pool.configure_runtime_env({"ATRI_AGENT_TOOL_ENDPOINT": "http://127.0.0.1:2"})
            finally:
                await pool.close()

    async def test_maintenance_can_use_fresh_session_without_rotating_chat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            privacy = PrivacyBoundary(secret=SECRET, session_root=Path(temp_dir))
            context = privacy.bind(
                ConversationScope.from_discord_ids(
                    guild_id=100,
                    channel_id=200,
                    user_id=300,
                )
            )
            pool = DshTenantRuntimePool(template=make_template(), privacy=privacy)
            try:
                result = await pool.run_turn(
                    context,
                    text="maintenance",
                    session_id_override="s_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                )
            finally:
                await pool.close()

            self.assertEqual(result.final_response, "echo:maintenance")
            self.assertEqual(
                privacy.bind(context.scope).identity.session_key,
                context.identity.session_key,
            )


if __name__ == "__main__":
    unittest.main()
