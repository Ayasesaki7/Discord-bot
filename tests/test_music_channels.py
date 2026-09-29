from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from chat.agent.music_tools import MusicToolError
from music.access import panel_destination
from music.cog import Music
from music.ui import BaseView
from test_music_resilience import music, song
import test_music_tools as tool_tests


def interaction(bot, guild, *, text=False, deferred=False):
    voice = guild.voice_client.channel
    channel = SimpleNamespace(id=40, type=discord.ChannelType.text, send=AsyncMock()) if text else voice
    return SimpleNamespace(client=bot.bot, guild=guild, guild_id=guild.id, channel=channel,
        user=SimpleNamespace(id=4, display_name='user', voice=SimpleNamespace(channel=voice)),
        response=SimpleNamespace(is_done=lambda: deferred, send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


class MusicChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.bot, self.guild = music(Path(self.directory.name))

    async def asyncTearDown(self):
        await self.bot.cog_unload()
        self.directory.cleanup()

    async def test_open_panel_rejected_from_text_even_while_joined_to_voice(self):
        ctx = interaction(self.bot, self.guild, text=True)
        await Music.panel.callback(self.bot, ctx)
        ctx.response.send_message.assert_awaited_once()
        ctx.response.defer.assert_not_awaited()
        ctx.channel.send.assert_not_awaited()
        self.assertIsNone(self.bot.queues[1]['channel'])

    async def test_all_search_import_entrypoints_reject_text_before_network(self):
        for method, args in [(self.bot.process_batch_request, (['Song'],)),
                             (self.bot.process_direct_link_request, (['https://example.com/song.mp3'],)),
                             (self.bot.process_collection_request, ('https://y.qq.com/album/123', 'album'))]:
            ctx = interaction(self.bot, self.guild, text=True)
            with patch.object(self.bot, '_qq_request_json', Mock(side_effect=AssertionError('No network'))) as request:
                await method(ctx, *args)
            ctx.response.send_message.assert_awaited_once()
            request.assert_not_called()

    async def test_old_button_in_text_cannot_control_voice_player(self):
        ctx = interaction(self.bot, self.guild, text=True)
        self.assertFalse(await BaseView.interaction_check(self.bot.queues[1]['view'], ctx))
        self.assertIn('普通文字频道', ctx.response.send_message.await_args.args[0])

    async def test_deferred_selection_is_rejected_after_changing_voice_channel(self):
        ctx = interaction(self.bot, self.guild, deferred=True)
        ctx.user.voice.channel = SimpleNamespace(id=99, type=discord.ChannelType.voice)
        with patch.object(self.bot, 'enqueue_songs') as enqueue:
            await self.bot._add_songs_to_queue(ctx, [song()])
        ctx.followup.send.assert_awaited_once()
        enqueue.assert_not_called()

    async def test_recheck_after_connect_before_enqueue(self):
        ctx = interaction(self.bot, self.guild, deferred=True)
        async def connect(_ctx):
            ctx.user.voice = None
            return self.guild.voice_client
        with patch.object(self.bot, '_ensure_voice_client', side_effect=connect), patch.object(self.bot, 'enqueue_songs') as enqueue:
            await self.bot._add_songs_to_queue(ctx, [song()])
        enqueue.assert_not_called()
        ctx.followup.send.assert_awaited_once()

    async def test_valid_voice_chat_can_open_panel(self):
        ctx = interaction(self.bot, self.guild)
        await Music.panel.callback(self.bot, ctx)
        self.assertIs(self.bot.queues[1]['channel'], self.guild.voice_client.channel)
        self.guild.voice_client.channel.send.assert_awaited_once()
        ctx.response.send_message.assert_not_awaited()

    async def test_connection_wait_cannot_retarget_to_new_voice(self):
        member = SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(
            id=99, type=discord.ChannelType.voice, connect=AsyncMock())))
        self.guild.voice_client = None
        result = await self.bot._ensure_voice_client_for(self.guild, member, expected_channel_id=3)
        self.assertIsNone(result)
        member.voice.channel.connect.assert_not_awaited()

    async def test_stale_text_destination_and_panel_migrate_to_connected_voice(self):
        ctx = interaction(self.bot, self.guild, text=True)
        old = SimpleNamespace(channel=ctx.channel, delete=AsyncMock(), edit=AsyncMock())
        data = self.bot.queues[1]
        data.update(channel=ctx.channel, message=old)
        await asyncio.gather(self.bot.update_player_ui(1), self.bot.update_player_ui(1))
        old.delete.assert_awaited_once()
        old.edit.assert_not_awaited()
        ctx.channel.send.assert_not_awaited()
        self.guild.voice_client.channel.send.assert_awaited_once()
        self.assertIs(data['channel'], self.guild.voice_client.channel)

    async def test_low_level_panel_sender_rejects_text_destination(self):
        ctx = interaction(self.bot, self.guild, text=True)
        with self.assertRaises(ValueError):
            await self.bot._send_player_panel(ctx.channel, self.bot.queues[1]['view'])
        ctx.channel.send.assert_not_awaited()

    async def test_no_voice_fallback_to_stale_text_destination(self):
        ctx = interaction(self.bot, self.guild, text=True)
        self.guild.voice_client.connected = False
        self.bot.queues[1]['channel'] = ctx.channel
        await self.bot.update_player_ui(1)
        ctx.channel.send.assert_not_awaited()
        self.assertIsNone(panel_destination(self.guild, self.bot.queues[1]))

    async def test_playback_failure_notice_goes_to_voice_not_text(self):
        ctx = interaction(self.bot, self.guild, text=True)
        data = self.bot.queues[1]
        current = song()
        data.update(current=current, channel=ctx.channel)
        with patch.object(self.bot, 'update_player_ui', AsyncMock()):
            await self.bot._play_failed(1, current, data['voice_generation'], 'failed')
        ctx.channel.send.assert_not_awaited()
        self.guild.voice_client.channel.send.assert_awaited_once()


class AgentChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_move_during_search_is_rejected_before_connect(self):
        host, music_cog, guild = tool_tests.MusicToolHostTests().make_host()
        def search(query):
            host.message.author.voice.channel = SimpleNamespace(id=11, type=discord.ChannelType.voice)
            return song()
        music_cog._search_song = search
        music_cog._ensure_voice_client_for = AsyncMock()
        with self.assertRaises(MusicToolError):
            await host.execute('add', {'query': 'Song'})
        music_cog._ensure_voice_client_for.assert_not_awaited()

    async def test_text_channel_agent_add_rejected_before_search(self):
        host, music_cog, guild = tool_tests.MusicToolHostTests().make_host()
        host.message.channel = SimpleNamespace(id=40, type=discord.ChannelType.text)
        music_cog._search_song = Mock(side_effect=AssertionError('No search in text channel'))
        with self.assertRaisesRegex(MusicToolError, '普通文字频道'):
            await host.execute('add', {'query': 'Song'})
        music_cog._search_song.assert_not_called()
        self.assertIsNone(guild.voice_client)

    async def test_text_status_is_read_only_and_does_not_advertise_add(self):
        host, music_cog, guild = tool_tests.MusicToolHostTests().make_host()
        host.message.channel = SimpleNamespace(id=40, type=discord.ChannelType.text)
        payload = json.loads((await host.execute('status', {}))['content'])
        self.assertEqual(payload['allowedActions'], ['status', 'queue'])

    async def test_another_voice_chat_is_not_the_joined_voice(self):
        host, music_cog, guild = tool_tests.MusicToolHostTests().make_host()
        host.message.channel = SimpleNamespace(id=11, type=discord.ChannelType.voice)
        with self.assertRaises(MusicToolError):
            await host.execute('add', {'query': 'Song'})

    async def test_voice_agent_add_binds_panel_to_actual_connection(self):
        host, music_cog, guild = tool_tests.MusicToolHostTests().make_host()
        await host.execute('add', {'query': 'Song'})
        self.assertIs(music_cog.queues[guild.id]['channel'], guild.voice_client.channel)
