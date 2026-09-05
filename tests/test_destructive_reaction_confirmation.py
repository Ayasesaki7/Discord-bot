from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from chat.cog import AtriChat


class DestructiveReactionConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, *, timeout: bool = False, requester_user_id: int = 10):
        emoji = discord.PartialEmoji(name='atri_maozhua', id=555)
        message = SimpleNamespace(
            id=900,
            guild=SimpleNamespace(id=100),
            author=SimpleNamespace(id=requester_user_id),
            add_reaction=AsyncMock(),
            clear_reaction=AsyncMock(),
            remove_reaction=AsyncMock(),
        )

        async def wait_for(event: str, *, timeout: float, check):
            self.assertEqual(event, 'raw_reaction_add')
            self.assertEqual(timeout, 60)
            if timeout_requested:
                raise TimeoutError
            self.assertFalse(
                check(
                    SimpleNamespace(
                        message_id=900,
                        user_id=999,
                        guild_id=100,
                        emoji=emoji,
                    )
                )
            )
            self.assertFalse(
                check(
                    SimpleNamespace(
                        message_id=901,
                        user_id=requester_user_id,
                        guild_id=100,
                        emoji=emoji,
                    )
                )
            )
            self.assertFalse(
                check(
                    SimpleNamespace(
                        message_id=900,
                        user_id=requester_user_id,
                        guild_id=100,
                        emoji=discord.PartialEmoji(name='other', id=556),
                    )
                )
            )
            payload = SimpleNamespace(
                message_id=900,
                user_id=requester_user_id,
                guild_id=100,
                emoji=emoji,
            )
            self.assertTrue(check(payload))
            return payload

        timeout_requested = timeout
        cog = SimpleNamespace(
            owner_user_id=10,
            destructive_confirm_timeout_seconds=60,
            bot=SimpleNamespace(wait_for=wait_for),
            _resolve_task_reaction=(
                lambda name, _guild=None: emoji if name == 'atri_maozhua' else None
            ),
            _chat_allowed_mentions=lambda: discord.AllowedMentions.none(),
        )
        return cog, message, emoji

    async def test_only_exact_requester_message_and_emoji_confirm(self) -> None:
        cog, message, emoji = self.make_context(requester_user_id=11)

        confirmed = await AtriChat._await_destructive_reaction_confirmation(
            cog,
            message=message,
            action='delete_message',
            arguments={'channel_id': '200', 'message_id': '300'},
        )

        self.assertTrue(confirmed)
        message.add_reaction.assert_awaited_once_with(emoji)
        message.clear_reaction.assert_awaited_once_with(emoji)

    async def test_timeout_cancels_and_removes_request_reaction(self) -> None:
        cog, message, emoji = self.make_context(timeout=True)

        confirmed = await AtriChat._await_destructive_reaction_confirmation(
            cog,
            message=message,
            action='delete_channel',
            arguments={'channel_id': '200'},
        )

        self.assertFalse(confirmed)
        message.clear_reaction.assert_awaited_once_with(emoji)
