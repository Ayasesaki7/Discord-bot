import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from music.lyrics import parse_lrc, lyric_window, next_refresh_delay
from test_music_resilience import music, song


class LrcTests(unittest.TestCase):
    def test_multitimestamps_offset_and_entities(self):
        lines = parse_lrc('[offset:500]\n[00:02.00][01:03.5]A &amp; B\n[00:01.250]第一行')
        self.assertEqual(lines, [(0.75, '第一行'), (1.5, 'A & B'), (63.0, 'A & B')])

    def test_base64_and_invalid_or_untimed_text(self):
        self.assertEqual(parse_lrc(base64.b64encode('[00:01]一行'.encode()).decode()), [(1.0, '一行')])
        for value in (None, {}, '没有时间轴', 'error=expired', 'x' * 600_000):
            self.assertEqual(parse_lrc(value), [])

    def test_position_handles_intro_exact_timestamp_and_end(self):
        lines = [(5, 'A'), (10, 'B'), (15, 'C')]
        self.assertEqual(lyric_window(lines, 0), ('', '♪ 前奏', 'A'))
        self.assertEqual(lyric_window(lines, 10), ('A', 'B', 'C'))
        self.assertEqual(lyric_window(lines, 100), ('B', 'C', ''))

    def test_refresh_follows_next_timestamp_and_coalesces_dense_lines(self):
        self.assertAlmostEqual(next_refresh_delay([(1, 'A'), (2.5, 'B')], 1), 1.54)
        self.assertEqual(next_refresh_delay([(1, 'A'), (1.2, 'B')], 1), 1.0)
        self.assertEqual(next_refresh_delay([(100, 'A')], 1), 10.0)
        self.assertEqual(next_refresh_delay([], 1), 10.0)


class LyricPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_lyrics_follow_elapsed_and_freeze_when_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            data = bot.queues[1]
            current = song()
            current['lyrics'] = [(0, 'A'), (10, 'B'), (20, 'C')]
            data.update(current=current, start_time=100, paused_elapsed=0)
            with patch('music.ui.time.time', return_value=115):
                self.assertIn('**B**', data['view']._lyric_text(data, current))
            data.update(start_time=None, paused_elapsed=15)
            with patch('music.ui.time.time', return_value=999):
                self.assertIn('**B**', data['view']._lyric_text(data, current))
            data['lyrics_enabled'] = False
            self.assertEqual(data['view']._lyric_text(data, current), '')
            await bot.cog_unload()

    async def test_same_track_across_guilds_shares_one_lyric_request(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            a, b = song('a'), song('b')
            a['mid'] = b['mid'] = 'shared'
            bot.queues[1]['current'] = a
            bot._get_or_create_queue(2)['current'] = b
            with patch.object(bot, '_qq_request_json', return_value={'code': 0, 'lyric': '[00:01]A'}) as request:
                with patch.object(bot, 'update_player_ui', AsyncMock()):
                    await asyncio.gather(bot._load_song_lyrics(1, a), bot._load_song_lyrics(2, b))
                    await bot._load_song_lyrics(1, a)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(a['lyrics'], b['lyrics'])
            await bot.cog_unload()

    async def test_slow_lyrics_cannot_update_a_different_current_track(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            old, new = song('old'), song('new')
            old['mid'] = 'old'
            data = bot.queues[1]
            data['current'] = old
            entered, release = asyncio.Event(), asyncio.Event()
            async def fetch(*args):
                entered.set()
                await release.wait()
                return [(1, 'old line')], 'ready'
            with patch.object(bot, '_fetch_lyrics', side_effect=fetch), patch.object(bot, 'update_player_ui', AsyncMock()) as update:
                task = asyncio.create_task(bot._load_song_lyrics(1, old))
                await entered.wait()
                data['current'] = new
                release.set()
                await task
                update.assert_not_awaited()
            self.assertNotIn('lyrics', new)
            await bot.cog_unload()

    async def test_lyric_error_is_cached_and_does_not_change_playback_state(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            current = song()
            current['mid'] = 'failed'
            data = bot.queues[1]
            data.update(current=current, agent_phase='playing')
            with patch.object(bot, '_qq_request_json', side_effect=TimeoutError()) as request:
                with patch.object(bot, 'update_player_ui', AsyncMock()):
                    await bot._load_song_lyrics(1, current)
                    await bot._load_song_lyrics(1, current)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(current['lyrics_state'], 'error')
            self.assertEqual(data['agent_phase'], 'playing')
            await bot.cog_unload()

    async def test_compact_card_has_two_small_control_rows_and_no_dummy_album(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            current = song()
            current['lyrics'] = [(0, '歌词第一行'), (5, '歌词第二行')]
            data = bot.queues[1]
            data.update(current=current, agent_phase='playing')
            guild.voice_client.playing = True
            data['view'].update_container()
            payload = data['view'].to_components()
            wire = json.dumps(payload, ensure_ascii=False)
            rows = [item for item in payload[0]['components'] if item['type'] == 1]
            self.assertEqual([len(row['components']) for row in rows], [5, 3])
            self.assertNotIn('专辑 · 未知', wire)
            self.assertNotIn('清空待播', wire)
            self.assertIn('歌词第一行', wire)
            self.assertLess(data['view'].total_children_count, 30)
            await bot.cog_unload()


if __name__ == '__main__':
    unittest.main()
