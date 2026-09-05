from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from chat.draw.agent import AtriDrawAgent


class DrawStructuredOutputRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_json_is_retried_before_drawing(self) -> None:
        agent = object.__new__(AtriDrawAgent)
        agent._structured_output_retry_count = 2
        client = SimpleNamespace(
            create_chat_completion=AsyncMock(
                side_effect=["not json", '{"prompt":"valid tags"}']
            )
        )

        with patch("tools.draw.agent.asyncio.sleep", new=AsyncMock()), patch(
            "builtins.print"
        ):
            result = await agent._request_json_object(
                [{"role": "user", "content": "draw"}],
                client=client,
                stage="tag_writer",
                temperature=0.35,
            )

        self.assertEqual(result, {"prompt": "valid tags"})
        self.assertEqual(client.create_chat_completion.await_count, 2)

    async def test_repeated_invalid_json_becomes_stage_specific_safe_error(self) -> None:
        agent = object.__new__(AtriDrawAgent)
        agent._structured_output_retry_count = 1
        client = SimpleNamespace(
            create_chat_completion=AsyncMock(side_effect=["bad", "still bad"])
        )

        with patch("tools.draw.agent.asyncio.sleep", new=AsyncMock()), patch(
            "builtins.print"
        ):
            with self.assertRaisesRegex(RuntimeError, "stage=tag_writer"):
                await agent._request_json_object(
                    [{"role": "user", "content": "draw"}],
                    client=client,
                    stage="tag_writer",
                    temperature=0.35,
                )

        message = agent._safe_draw_failure_text(
            RuntimeError("draw structured stage failed: stage=tag_writer"),
            stage="tag_writer",
        )
        self.assertIn("没有调用 NAI", message)


class NovelAITransportRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_transport_failure_retries_generation(self) -> None:
        agent = object.__new__(AtriDrawAgent)
        agent._nai_generation_lock = asyncio.Lock()
        agent._nai_waiting_count = 0
        agent._nai_transport_retry_count = 1
        expected = ([SimpleNamespace(data=b"image")], {"model": "test"})
        agent.nai = SimpleNamespace(
            generate_image=AsyncMock(
                side_effect=[aiohttp.ClientOSError(54, "connection reset"), expected]
            )
        )
        agent._edit_status_persona = AsyncMock()

        with patch("tools.draw.agent.asyncio.sleep", new=AsyncMock()), patch(
            "builtins.print"
        ):
            result = await agent._run_queued_nai_generation(
                message=SimpleNamespace(),
                status=None,
                progress=AsyncMock(),
                prompt="tags",
                negative_prompt="bad",
                params={},
            )

        self.assertEqual(result, expected)
        self.assertEqual(agent.nai.generate_image.await_count, 2)
        self.assertEqual(agent._edit_status_persona.await_count, 2)


if __name__ == "__main__":
    unittest.main()
