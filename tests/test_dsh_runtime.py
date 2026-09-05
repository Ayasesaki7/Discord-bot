from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from chat.agent.dsh_runtime import (
    DshJsonRpcProcess,
    DshRuntimeError,
    DshTurnFailedError,
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

    async def test_process_accepts_jsonrpc_line_larger_than_asyncio_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            try:
                result = await runtime.run_turn(
                    session_id="s_0123456789abcdef0123456789abcdef01234567",
                    text="oversized-jsonrpc-frame",
                )
            finally:
                await runtime.close()

        self.assertEqual(len(result.final_response), 100_000)

    async def test_structured_turn_error_preserves_provider_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            try:
                with self.assertRaises(DshTurnFailedError) as raised:
                    await runtime.run_turn(
                        session_id="s_0123456789abcdef0123456789abcdef01234567",
                        text="provider-context-overflow",
                    )
            finally:
                await runtime.close()

        self.assertEqual(raised.exception.code, "CONTEXT_WINDOW_EXCEEDED")
        self.assertIn("simulated context overflow", str(raised.exception))

    async def test_passive_context_is_persisted_without_a_model_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            session_id = "s_0123456789abcdef0123456789abcdef01234567"
            try:
                pending = await runtime.inject_context(
                    session_id=session_id,
                    text="ordinary channel message",
                )
                result = await runtime.run_turn(
                    session_id=session_id,
                    text="show-injected",
                )
            finally:
                await runtime.close()

        self.assertEqual(pending, 1)
        self.assertEqual(result.final_response, "context:ordinary channel message")

    async def test_native_manual_compaction_returns_validated_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            runtime.start = AsyncMock()
            runtime._request = AsyncMock(
                return_value={
                    "sessionId": "s_0123456789abcdef0123456789abcdef01234567",
                    "compacted": True,
                    "shadowedItems": 42,
                    "shadowedTokens": 109_000,
                    "summarySeq": 123,
                }
            )

            result = await runtime.compact_session(
                session_id="s_0123456789abcdef0123456789abcdef01234567"
            )

        self.assertTrue(result.compacted)
        self.assertEqual(result.shadowed_items, 42)
        self.assertEqual(result.shadowed_tokens, 109_000)
        self.assertEqual(result.summary_seq, 123)
        runtime._request.assert_awaited_once_with(
            "session/compact",
            {"sessionId": "s_0123456789abcdef0123456789abcdef01234567"},
            timeout=5.0,
        )

    async def test_session_persona_is_model_facing_without_chat_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            session_id = "s_0123456789abcdef0123456789abcdef01234567"
            try:
                changed = await runtime.configure_session_persona(
                    session_id=session_id,
                    persona="base persona plus the complete emoji catalog",
                )
                unchanged = await runtime.configure_session_persona(
                    session_id=session_id,
                    persona="base persona plus the complete emoji catalog",
                )
                result = await runtime.run_turn(
                    session_id=session_id,
                    text="show-persona",
                )
            finally:
                await runtime.close()

        self.assertTrue(changed)
        self.assertFalse(unchanged)
        self.assertEqual(
            result.final_response,
            "persona:base persona plus the complete emoji catalog",
        )

    async def test_session_context_updates_without_becoming_injected_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = DshJsonRpcProcess(
                template=make_template(),
                tenant_key="t_0123456789abcdef0123456789abcdef01234567",
                session_root=Path(temp_dir),
            )
            session_id = "s_0123456789abcdef0123456789abcdef01234567"
            try:
                first = await runtime.configure_session_context(
                    session_id=session_id,
                    context="requester_can_manage_current_guild=false",
                )
                unchanged = await runtime.configure_session_context(
                    session_id=session_id,
                    context="requester_can_manage_current_guild=false",
                )
                second = await runtime.configure_session_context(
                    session_id=session_id,
                    context="requester_can_manage_current_guild=true",
                )
                result = await runtime.run_turn(
                    session_id=session_id,
                    text="show-context-var",
                )
            finally:
                await runtime.close()

        self.assertTrue(first)
        self.assertFalse(unchanged)
        self.assertTrue(second)
        self.assertEqual(
            result.final_response,
            "context-var:requester_can_manage_current_guild=true",
        )

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

    async def test_session_snapshot_reads_latest_same_session_checkpoint(self) -> None:
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
            log_path = (
                privacy.session_path(context).parent
                / "--runtime--"
                / context.identity.session_key
                / "session.jsonl"
            )
            log_path.parent.mkdir(parents=True)
            events = [
                {
                    "type": "compaction/summary",
                    "seq": 1,
                    "data": {"summary": [{"type": "text", "text": "old memory"}]},
                },
                {"type": "user/message", "seq": 2, "data": {}},
                {
                    "type": "compaction/summary",
                    "seq": 3,
                    "data": {"summary": [{"type": "text", "text": "latest memory"}]},
                },
                {
                    "type": "turn/end",
                    "seq": 4,
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {
                                "message": "too much context",
                                "code": "CONTEXT_WINDOW_EXCEEDED",
                            },
                        }
                    },
                },
            ]
            events.insert(
                3,
                {
                    "type": "user/message",
                    "seq": 4,
                    "data": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "[nickname=Tester | discord_id=1 | relationship=participant]\n"
                                    "当前这条消息发送时间: 2026-08-20 15:00:00 Asia/Shanghai\n"
                                    "new question\n\n"
                                    "[Discord host turn metadata and mandatory runtime rules; "
                                    "not user-authored, do not quote:]\nsecret host frame"
                                ),
                            }
                        ]
                    },
                },
            )
            events.insert(
                4,
                {
                    "type": "assistant/message",
                    "seq": 5,
                    "data": {
                        "message": {"content": [{"type": "text", "text": "reply"}]},
                        "usage": {
                            "inputTokens": 80_000,
                            "cacheReadTokens": 15_000,
                            "cacheWriteTokens": 1_000,
                            "outputTokens": 50,
                        },
                    },
                },
            )
            events.insert(
                5,
                {
                    "type": "turn/end",
                    "seq": 6,
                    "data": {"reason": {"kind": "completed"}},
                },
            )
            raw = "".join(json.dumps(event) + "\n" for event in events)
            log_path.write_text(raw, encoding="utf-8")

            snapshot = await pool.session_snapshot(context)

            self.assertEqual(snapshot.byte_size, log_path.stat().st_size)
            self.assertEqual(snapshot.latest_compaction_summary, "latest memory")
            self.assertIn("new question", snapshot.post_compaction_delta)
            self.assertIn("reply", snapshot.post_compaction_delta)
            self.assertNotIn("secret host frame", snapshot.post_compaction_delta)
            self.assertNotIn("old memory", snapshot.post_compaction_delta)
            self.assertEqual(snapshot.post_compaction_event_count, 2)
            self.assertEqual(snapshot.post_compaction_dropped_count, 0)
            self.assertEqual(snapshot.latest_input_tokens, 96_000)
            self.assertEqual(snapshot.latest_turn_error_code, "CONTEXT_WINDOW_EXCEEDED")
            self.assertEqual(snapshot.latest_turn_error_message, "too much context")

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

    async def test_snapshot_chains_host_recovered_checkpoint_and_delta(self) -> None:
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
            log_path = (
                privacy.session_path(context).parent
                / "--runtime--"
                / context.identity.session_key
                / "session.jsonl"
            )
            log_path.parent.mkdir(parents=True)
            injected = "\n\n".join(
                [
                    "\n".join(
                        [
                            "[Recovered same-channel memory checkpoint; host-authored boundary]",
                            "This is factual conversation memory recovered from the previous DSH session.",
                            "chained summary",
                            "[End recovered memory checkpoint]",
                        ]
                    ),
                    "\n".join(
                        [
                            "[Recovered post-checkpoint DSH delta; host-authored boundary]",
                            "These are bounded user/assistant records written after the checkpoint.",
                            "[User]\nnewer fact",
                            "[End recovered post-checkpoint DSH delta]",
                        ]
                    ),
                ]
            )
            events = [
                {
                    "type": "user/message",
                    "seq": 1,
                    "data": {"content": [{"type": "text", "text": injected}]},
                },
                {
                    "type": "assistant/message",
                    "seq": 2,
                    "data": {
                        "message": {
                            "content": [{"type": "text", "text": "newest answer"}]
                        }
                    },
                },
            ]
            log_path.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

            snapshot = await pool.session_snapshot(context)

            self.assertEqual(snapshot.latest_compaction_summary, "chained summary")
            self.assertIn("newer fact", snapshot.post_compaction_delta)
            self.assertIn("newest answer", snapshot.post_compaction_delta)
            self.assertNotIn(
                "This is factual conversation memory",
                snapshot.latest_compaction_summary,
            )

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
