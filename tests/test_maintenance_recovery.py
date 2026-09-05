from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chat.agent.dsh_runtime import DshRuntimeError
from chat.cog import AtriChat


class FakeMaintenanceToolServer:
    def __init__(self) -> None:
        self.handler = None

    @asynccontextmanager
    async def bind_project_turn(self, _session_id, handler):
        self.handler = handler
        try:
            yield
        finally:
            self.handler = None


class FakePrivacy:
    def bind(self, _scope):
        return SimpleNamespace(
            reply=SimpleNamespace(require_destination=lambda **_kwargs: None)
        )


def make_cog(pool, server: FakeMaintenanceToolServer) -> AtriChat:
    cog = object.__new__(AtriChat)
    cog.dsh_code_runtime_pool = pool
    cog.agent_privacy = FakePrivacy()
    cog.agent_tool_server = server
    cog.owner_user_id = 300
    cog._agent_session_locks = {}
    cog._ensure_agent_tool_bridge = AsyncMock()
    cog.runtime_tool_host = SimpleNamespace()
    cog.plugin_manager = SimpleNamespace()
    cog.project_tool_host = SimpleNamespace()
    cog.credential_store = SimpleNamespace()
    return cog


class MaintenanceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_failure_before_mutation_recovers_and_retries_once(self) -> None:
        class Pool:
            def __init__(self) -> None:
                self.calls = 0
                self.recover_session = AsyncMock(return_value="session_recycled")

            async def run_turn(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise DshRuntimeError("protocol stream ended")
                return SimpleNamespace(final_response="maintenance completed")

        pool = Pool()
        server = FakeMaintenanceToolServer()
        cog = make_cog(pool, server)

        report = await cog._run_maintenance_agent(
            guild_id=100,
            channel_id=200,
            user_id=300,
            task="inspect allowed configuration",
        )

        self.assertEqual(report, "maintenance completed")
        self.assertEqual(pool.calls, 2)
        pool.recover_session.assert_awaited_once()

    async def test_successful_credential_write_survives_summary_runtime_failure(self) -> None:
        server = FakeMaintenanceToolServer()

        class Pool:
            def __init__(self) -> None:
                self.calls = 0
                self.recover_session = AsyncMock(return_value="session_recycled")

            async def run_turn(self, *_args, **_kwargs):
                self.calls += 1
                assert server.handler is not None
                await server.handler(
                    "credential_update",
                    {"service": "qqmusic", "content": "ptcz=private-test-value"},
                )
                raise DshRuntimeError("protocol stream ended after tool result")

        pool = Pool()
        cog = make_cog(pool, server)
        cog.credential_store = SimpleNamespace(
            update=AsyncMock(
                return_value={
                    "summary": "Updated the protected QQ Music credential file.",
                    "content": "{}",
                    "truncated": False,
                }
            )
        )
        cog._reload_service_credential = AsyncMock(return_value=True)

        report = await cog._run_maintenance_agent(
            guild_id=100,
            channel_id=200,
            user_id=300,
            task="replace the supplied QQ Music credential",
        )

        self.assertEqual(pool.calls, 1)
        self.assertIn("宿主已经确认", report)
        self.assertIn("credential_update", report)
        self.assertNotIn("private-test-value", report)


if __name__ == "__main__":
    unittest.main()
