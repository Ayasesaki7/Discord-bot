from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chat.agent.discord_tools import DiscordToolHost


class DiscordAgentSemanticToolsTests(unittest.IsolatedAsyncioTestCase):
    def make_host(self):
        confirmation = AsyncMock(return_value=True)
        message = SimpleNamespace(
            id=900,
            content="This text is not interpreted by the host keyword layer.",
            author=SimpleNamespace(id=10),
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
        )
        host = DiscordToolHost(
            bot=SimpleNamespace(),
            message=message,
            owner_user_id=10,
            confirmation_handler=confirmation,
        )
        return host, confirmation

    async def test_range_delete_is_strictly_after_anchor_by_default(self) -> None:
        host, confirmation = self.make_host()
        deleted = [
            SimpleNamespace(id=301, author=SimpleNamespace(id=20)),
            SimpleNamespace(id=302, author=SimpleNamespace(id=21)),
        ]
        channel = SimpleNamespace(
            id=200,
            purge=AsyncMock(return_value=deleted),
            fetch_message=AsyncMock(),
        )
        host._message_channel = AsyncMock(return_value=channel)

        result = await host.execute(
            "delete_messages",
            {"channel_id": "200", "after_message_id": "300"},
        )

        confirmation.assert_awaited_once()
        channel.fetch_message.assert_not_awaited()
        kwargs = channel.purge.await_args.kwargs
        self.assertEqual(kwargs["after"].id, 300)
        self.assertNotIn("before", kwargs)
        self.assertTrue(kwargs["oldest_first"])
        payload = json.loads(result["content"])
        self.assertEqual(payload["deletedCount"], 2)
        self.assertFalse(payload["includedAfter"])

    async def test_range_delete_can_include_explicit_boundary_and_filter_author(self) -> None:
        host, _confirmation = self.make_host()
        boundary = SimpleNamespace(
            id=300,
            author=SimpleNamespace(id=20),
            delete=AsyncMock(),
        )
        deleted = [SimpleNamespace(id=301, author=SimpleNamespace(id=20))]
        channel = SimpleNamespace(
            id=200,
            purge=AsyncMock(return_value=deleted),
            fetch_message=AsyncMock(return_value=boundary),
        )
        host._message_channel = AsyncMock(return_value=channel)

        result = await host.execute(
            "delete_messages",
            {
                "channel_id": "200",
                "after_message_id": "300",
                "include_after": True,
                "author_id": "20",
                "limit": 100,
            },
        )

        kwargs = channel.purge.await_args.kwargs
        self.assertTrue(kwargs["check"](deleted[0]))
        self.assertFalse(
            kwargs["check"](SimpleNamespace(author=SimpleNamespace(id=99)))
        )
        boundary.delete.assert_awaited_once()
        payload = json.loads(result["content"])
        self.assertEqual(payload["deletedCount"], 2)
        self.assertTrue(payload["includedAfter"])
        self.assertEqual(payload["authorId"], "20")

    async def test_range_delete_requires_a_bound(self) -> None:
        host, confirmation = self.make_host()
        host._message_channel = AsyncMock(
            return_value=SimpleNamespace(id=200, purge=AsyncMock())
        )

        with self.assertRaisesRegex(RuntimeError, "requires after_message_id"):
            await host.execute("delete_messages", {"channel_id": "200"})

        confirmation.assert_not_awaited()

    async def test_identical_destructive_retry_reuses_one_reaction(self) -> None:
        host, confirmation = self.make_host()
        arguments = {
            "before_message_id": "400",
            "author_id": "20",
        }

        await host._confirm_destructive_action("delete_messages", arguments)
        await host._confirm_destructive_action(
            "delete_messages",
            {
                **arguments,
                "channel_id": "200",
                "limit": 100,
                "reason": "retry after a zero-result response",
            },
        )

        confirmation.assert_awaited_once()

    async def test_changed_destructive_scope_requires_another_reaction(self) -> None:
        host, confirmation = self.make_host()

        await host._confirm_destructive_action(
            "delete_messages",
            {"channel_id": "200", "before_message_id": "400"},
        )
        await host._confirm_destructive_action(
            "delete_messages",
            {"channel_id": "200", "before_message_id": "500"},
        )

        self.assertEqual(confirmation.await_count, 2)
