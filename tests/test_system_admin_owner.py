from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from system_admin import (
    DEFAULT_OWNER_DISCORD_ID,
    SystemAdmin,
    _read_bounded_int,
    _read_owner_discord_id,
)


class OwnerConfigurationTests(unittest.TestCase):
    def test_configured_owner_is_returned_without_snowflake_precision_loss(self):
        with patch.dict(os.environ, {'ATRI_OWNER_DISCORD_ID': ' 1234567890123456789 '}):
            self.assertEqual(_read_owner_discord_id(), 1234567890123456789)

    def test_missing_owner_uses_existing_default(self):
        with patch.dict(os.environ):
            os.environ.pop('ATRI_OWNER_DISCORD_ID', None)
            self.assertEqual(_read_owner_discord_id(), DEFAULT_OWNER_DISCORD_ID)

    def test_blank_owner_uses_existing_default(self):
        for raw in ('', ' \t '):
            with self.subTest(raw=raw), patch.dict(os.environ, {'ATRI_OWNER_DISCORD_ID': raw}):
                self.assertEqual(_read_owner_discord_id(), DEFAULT_OWNER_DISCORD_ID)

    def test_invalid_owner_uses_existing_default(self):
        for raw in ('not-an-id', '1.25', '1e18', '<@1234567890123456789>'):
            with self.subTest(raw=raw), patch.dict(os.environ, {'ATRI_OWNER_DISCORD_ID': raw}):
                self.assertEqual(_read_owner_discord_id(), DEFAULT_OWNER_DISCORD_ID)

    def test_nonpositive_owner_uses_existing_default(self):
        for raw in ('0', '-123'):
            with self.subTest(raw=raw), patch.dict(os.environ, {'ATRI_OWNER_DISCORD_ID': raw}):
                self.assertEqual(_read_owner_discord_id(), DEFAULT_OWNER_DISCORD_ID)

    def test_adjacent_bounded_integer_reader_still_clamps_values(self):
        for raw, expected in (('', 10), ('bad', 10), ('-5', 1), ('15', 15), ('999', 20)):
            with self.subTest(raw=raw), patch.dict(os.environ, {'ATRI_OWNER_TEST_BOUND': raw}):
                self.assertEqual(_read_bounded_int('ATRI_OWNER_TEST_BOUND', 10, 1, 20), expected)


class SystemAdminOwnerPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner_id = 1234567890123456789
        with patch.dict(os.environ, {'ATRI_OWNER_DISCORD_ID': str(self.owner_id)}):
            with patch.object(SystemAdmin, '_periodic_generated_cleanup', AsyncMock()):
                self.cog = SystemAdmin(SimpleNamespace())

    async def asyncTearDown(self):
        self.cog.cog_unload()
        await asyncio.gather(self.cog._cleanup_task, return_exceptions=True)

    def interaction(self, user_id, *, responded=False):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(administrator=True)),
            response=SimpleNamespace(is_done=Mock(return_value=responded), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_configured_owner_passes_developer_check(self):
        interaction = self.interaction(self.owner_id)
        self.assertTrue(await self.cog._ensure_owner(interaction))
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    async def test_server_administrator_or_old_default_is_not_developer(self):
        for user_id in (self.owner_id + 1, DEFAULT_OWNER_DISCORD_ID):
            with self.subTest(user_id=user_id):
                interaction = self.interaction(user_id)
                self.assertFalse(await self.cog._ensure_owner(interaction))
                interaction.response.send_message.assert_awaited_once_with(
                    '这个命令只允许开发者使用。', ephemeral=True,
                )

    async def test_denial_uses_private_followup_after_response(self):
        interaction = self.interaction(self.owner_id + 1, responded=True)
        self.assertFalse(await self.cog._ensure_owner(interaction))
        interaction.followup.send.assert_awaited_once_with('这个命令只允许开发者使用。', ephemeral=True)
        interaction.response.send_message.assert_not_awaited()
