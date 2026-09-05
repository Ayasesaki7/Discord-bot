from __future__ import annotations

import asyncio
import os
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from system_admin import SystemAdmin


class SystemAdminRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_exits_process_after_graceful_bot_close(self) -> None:
        cog = object.__new__(SystemAdmin)
        cog.bot = SimpleNamespace(close=AsyncMock())
        cog._cleanup_task = Mock()
        cog._shutdown_task = None

        with patch('system_admin.os.kill') as kill:
            task = asyncio.create_task(cog._shutdown_after_delay(0))
            cog._shutdown_task = task
            await task

        cog.bot.close.assert_awaited_once_with()
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    async def test_cog_unload_does_not_cancel_active_shutdown_task_itself(self) -> None:
        cog = object.__new__(SystemAdmin)
        cog._cleanup_task = Mock()
        release = asyncio.Event()

        async def shutdown() -> None:
            release.set()
            await asyncio.sleep(0)
            cog.cog_unload()

        task = asyncio.create_task(shutdown())
        cog._shutdown_task = task
        await release.wait()
        await task

        self.assertFalse(task.cancelled())
