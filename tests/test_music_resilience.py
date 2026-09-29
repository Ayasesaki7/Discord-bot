from __future__ import annotations

import asyncio
import math
import os
import shutil
import socket
import struct
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from music.audio import AudioPipeline, PublicResolver, public_http_url
from music.cog import Music
from music.ui import (BaseView, MusicInterface, ClearQueueView, QueueListView,
                      DEFAULT_COVER_FILENAME, DEFAULT_COVER_URL, DEFAULT_COVER_PATH)


def song(identity="1"):
    return {"id": identity, "name": "Test", "ar": [{"name": "Artist"}],
            "al": {}, "dt": 1000, "requester": "tester"}


def cached(path):
    path.write_bytes(b'OggS' + b'\x00' * 24 + b'OpusHead' + b'x' * 2048)
    return str(path)


class Voice:
    def __init__(self):
        self.channel = SimpleNamespace(id=3, type=discord.ChannelType.voice)
        self.channel.send = AsyncMock(side_effect=lambda *args, **kwargs: SimpleNamespace(
            channel=self.channel, edit=AsyncMock(), delete=AsyncMock(), attachments=[]))
        self.connected = True
        self.playing = False
        self.paused = False
        self.play = Mock(side_effect=self._play)
        self.stop = Mock()
        self.disconnect = AsyncMock(side_effect=self._disconnect)

    def _play(self, source, **kwargs):
        self.playing = True

    async def _disconnect(self, **kwargs):
        self.connected = False

    def is_connected(self): return self.connected
    def is_playing(self): return self.playing
    def is_paused(self): return self.paused


def music(root):
    bot = object.__new__(Music)
    guild = SimpleNamespace(id=1, voice_client=Voice())
    bot.bot = SimpleNamespace(get_guild=lambda _id: guild, get_cog=lambda _: bot)
    bot.loop = asyncio.get_running_loop()
    bot.queues = {}
    bot.agent_locks = {}
    bot.voice_locks = {}
    bot.voice_reconnect_after = {}
    bot.download_locks = {}
    bot.download_jobs = {}
    bot.download_failures = {}
    bot.download_slots = asyncio.Semaphore(4)
    bot.prefetch_slots = asyncio.Semaphore(2)
    bot.prefetch_keys = set()
    bot.background_tasks = set()
    bot.lyric_cache = {}
    bot.lyric_jobs = {}
    bot.lyric_slots = asyncio.Semaphore(4)
    bot.qq_audio_blocked_until = 0.0
    bot.qq_download_403_count = 0
    bot.qqmusic_cookie = ""
    bot.qqmusic_quality = "auto"
    bot.qqmusic_refresh_task = None
    bot.cleaner_task = None
    bot.max_queue_length = 200
    bot.cache_root = root
    bot.ffmpeg_executable = 'ffmpeg'
    bot.audio = AudioPipeline('ffmpeg', max_bytes=200 * 1024 * 1024)
    bot._get_or_create_queue(1)
    return bot, guild


class Content:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start+size]


class Response:
    def __init__(self, status=200, body=b'x' * 4096, mime='audio/mpeg', length=None, location=None):
        self.status = status
        self.content = Content(body)
        self.content_length = length
        self.headers = {'Content-Type': mime}
        if location:
            self.headers['Location'] = location
        self.read = Mock(side_effect=AssertionError('Do not buffer the entire audio'))

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


class AudioPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_mislabeled_qq_mp3_is_accepted_but_form_error_is_rejected(self):
        for signature in (b'ID3', b'fLaC', b'OggS', b'RIFFxxxxWAVE', b'xxxxftyp', b'\xff\xfb'):
            body = signature + b'\x00' * 4096
            pipeline = AudioPipeline('ffmpeg', max_bytes=8000)
            pipeline.session = SimpleNamespace(closed=False, get=Mock(return_value=Response(
                body=body, mime='application/x-www-form-urlencoded', length=len(body))))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'source.audio'
                self.assertEqual(await pipeline.download('https://stream.qq.com/audio', path), 200)
                self.assertEqual(path.read_bytes(), body)
        pipeline.session.get = Mock(return_value=Response(
            body=b'error=login_required&message=expired' * 200, mime='application/x-www-form-urlencoded'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.audio'
            with self.assertRaises(RuntimeError):
                await pipeline.download('https://stream.qq.com/audio', path)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name('source.audio.part').exists())

    async def test_mislabeled_media_signature_split_across_chunks(self):
        class TinyContent:
            async def iter_chunked(self, size):
                for byte in b'ID3' + b'x' * 2048:
                    yield bytes([byte])
        response = Response(mime='text/plain')
        response.content = TinyContent()
        pipeline = AudioPipeline('ffmpeg', max_bytes=8000)
        pipeline.session = SimpleNamespace(closed=False, get=Mock(return_value=response))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(await pipeline.download('https://stream.qq.com/audio', Path(directory) / 'track'), 200)

    async def test_streamed_download_and_shared_session(self):
        pipeline = AudioPipeline('ffmpeg', max_bytes=8000)
        pipeline.session = SimpleNamespace(closed=False, get=Mock(return_value=Response(length=4096)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'track.audio'
            result = await pipeline.download('https://example.com/audio', path)
            self.assertEqual(result, 200)
            self.assertEqual(path.stat().st_size, 4096)
            self.assertFalse(path.with_name('track.audio.part').exists())
            self.assertIs(await pipeline._session(), pipeline.session)

    async def test_qq_cookie_not_forwarded_to_foreign_redirect(self):
        pipeline = AudioPipeline('ffmpeg', max_bytes=8000)
        pipeline.session = SimpleNamespace(closed=False, get=Mock(side_effect=[
            Response(status=302, location='https://cdn.example/audio'), Response(),
        ]))
        with tempfile.TemporaryDirectory() as directory:
            await pipeline.download('https://stream.qq.com/audio', Path(directory) / 'track', cookie='private=value')
        calls = pipeline.session.get.call_args_list
        self.assertEqual(calls[0].kwargs['headers']['Cookie'], 'private=value')
        self.assertNotIn('Cookie', calls[1].kwargs['headers'])

    async def test_html_partial_and_large_downloads_are_not_cached(self):
        for response in (
            Response(mime='text/html'), Response(length=9000),
            Response(body=b'x' * 9000), Response(length=7000),
        ):
            pipeline = AudioPipeline('ffmpeg', max_bytes=8000)
            pipeline.session = SimpleNamespace(closed=False, get=Mock(return_value=response))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'audio'
                with self.assertRaises(RuntimeError):
                    await pipeline.download('https://example.com/audio', path)
                self.assertFalse(path.exists())
                self.assertFalse(path.with_name('audio.part').exists())

    async def test_redirect_to_private_address_is_rejected_before_connect(self):
        pipeline = AudioPipeline('ffmpeg', max_bytes=8000)
        pipeline.session = SimpleNamespace(closed=False, get=Mock(return_value=Response(
            status=302, location='http://169.254.169.254/secret',
        )))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                await pipeline.download('https://example.com/audio', Path(directory) / 'audio')
        pipeline.session.get.assert_called_once()

    async def test_hostname_resolving_to_private_address_is_rejected(self):
        resolver = PublicResolver()
        with patch('music.audio.DefaultResolver.resolve', AsyncMock(return_value=[{'host': '127.0.0.1'}])):
            with self.assertRaises(OSError):
                await resolver.resolve('test.example', 443, socket.AF_INET)
        await resolver.close()
        for url in ('http://localhost/x', 'http://10.1.1.1/x', 'file:///tmp/x', 'https://u:p@example.com/x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_http_url(url)

    async def test_subprocess_timeout_terminates_child(self):
        pipeline = AudioPipeline(None, max_bytes=8000)
        process = Mock()
        process.returncode = None
        process.stderr.read = AsyncMock(return_value=b'')
        async def wait():
            if process.returncode is None:
                await asyncio.sleep(10)
            return process.returncode
        process.wait = wait
        process.kill.side_effect = lambda: setattr(process, 'returncode', -9)
        with patch('music.audio.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaises(TimeoutError):
                await pipeline._run(['unused'], timeout=0.01)
        process.kill.assert_called_once()

    async def test_ffmpeg_real_audio_preparation_and_corrupt_input(self):
        executable = shutil.which('ffmpeg')
        if not executable:
            try:
                import imageio_ffmpeg
                executable = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError:
                self.skipTest('ffmpeg unavailable')
        pipeline = AudioPipeline(executable, max_bytes=800000)
        with tempfile.TemporaryDirectory() as directory:
            raw, out = Path(directory) / 'sample.wav', Path(directory) / 'ready.opus'
            with wave.open(str(raw), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b''.join(struct.pack('<h', round(15000 * math.sin(i * math.tau * 440 / 16000)))
                                           for i in range(32000)))
            await pipeline.prepare(raw, out)
            self.assertTrue(pipeline.valid_cache(out))
            source = discord.FFmpegOpusAudio(str(out), codec='opus', executable=executable)
            try:
                self.assertTrue(source.is_opus())
                self.assertTrue(await asyncio.to_thread(source.read))
            finally:
                source.cleanup()
            # A valid silent/short Opus stream may be smaller than 1 KiB.
            with wave.open(str(raw), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b'\x00' * 3200)
            await pipeline.prepare(raw, out)
            self.assertTrue(pipeline.valid_cache(out))
            raw.write_bytes(b'fLaC' + b'bad audio' * 4096)
            with self.assertRaises(RuntimeError):
                await pipeline.prepare(raw, out)
            self.assertFalse(out.exists())


class MusicDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_song_download_is_shared_and_cancelled_waiter_does_not_cancel_job(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            started, release = asyncio.Event(), asyncio.Event()
            async def acquire(_song, _work, path):
                started.set()
                await release.wait()
                return cached(path)
            with patch.object(bot, '_acquire_audio', AsyncMock(side_effect=acquire)) as download:
                a, b = song(), song()
                first = asyncio.create_task(bot._try_download_song(a))
                await started.wait()
                second = asyncio.create_task(bot._try_download_song(b))
                await asyncio.sleep(0)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertTrue(next(iter(bot.download_locks.values())).locked())
                release.set()
                path = await second
                await asyncio.sleep(0)
                download.assert_awaited_once()
            self.assertEqual(b['local_path'], path)
            self.assertFalse(bot.download_jobs)
            self.assertFalse(bot.download_locks)
            await bot.cog_unload()

    async def test_failed_download_is_cooled_down_and_locks_are_released(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            with patch.object(bot, '_acquire_audio', AsyncMock(return_value=None)) as download:
                self.assertIsNone(await bot._try_download_song(song()))
                self.assertIsNone(await bot._try_download_song(song()))
            download.assert_awaited_once()
            self.assertFalse(bot.download_locks)
            self.assertEqual(list(Path(directory).iterdir()), [])
            await bot.cog_unload()

    async def test_distinct_downloads_are_capped_at_four(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            in_flight, peak = 0, 0
            async def acquire(_song, _work, path):
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1
                return cached(path)
            with patch.object(bot, '_acquire_audio', AsyncMock(side_effect=acquire)):
                await asyncio.gather(*(bot._try_download_song(song(str(n))) for n in range(6)))
            self.assertEqual(peak, 4)
            await bot.cog_unload()

    async def test_cache_key_cannot_escape_cache_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            self.assertRegex(bot._cache_key(song('../../.env')), r'^v2_[0-9a-f]{32}$')
            await bot.cog_unload()

    async def test_cleaner_preserves_current_and_queued_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            current, next_song = song(), song('2')
            old = Path(directory) / 'expired.opus'
            for item in (current, next_song):
                path = Path(directory) / (bot._cache_key(item) + '.opus')
                item['local_path'] = cached(path)
                os.utime(path, (0, 0))
            cached(old)
            os.utime(old, (0, 0))
            bot.queues[1]['current'], bot.queues[1]['queue'] = current, [next_song]
            self.assertEqual(bot._clean_audio_cache(), 1)
            self.assertFalse(old.exists())
            self.assertTrue(Path(current['local_path']).exists())
            self.assertTrue(Path(next_song['local_path']).exists())
            await bot.cog_unload()

    async def test_broken_flac_falls_back_once_to_other_official_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            value = dict(song(), mid='sample')
            def get_url(value):
                value['qq_filename'] = bot._qq_quality_candidates(value)[0][0]
                return 'https://stream.qq.com/' + value['qq_filename']
            with patch.object(bot, '_get_song_url', side_effect=get_url) as get:
                with patch.object(bot, '_download_qq_audio_url', AsyncMock(return_value=('raw', 200))):
                    with patch.object(bot.audio, 'prepare', AsyncMock(side_effect=[
                        RuntimeError('invalid flac frame'), None,
                    ])) as transcode:
                        cached(Path(directory) / 'prepared.opus')
                        result = await bot._acquire_audio(value, Path(directory), Path(directory) / 'done.opus')
            self.assertEqual(value['_qq_failed_files'], {'F000sample.flac'})
            self.assertEqual(value['qq_filename'], 'M800sample.mp3')
            self.assertEqual(get.call_count, 2)
            self.assertEqual(transcode.await_count, 2)
            self.assertTrue(Path(result).exists())
            await bot.cog_unload()


class OfficialQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.bot, _ = music(self.root)
        self.bot.qqmusic_guid = 'test-guid'
        self.bot._qq_login_uin = Mock(return_value='0')
        self.value = dict(song(), mid='songmid', media_mid='mediamid')
        self.requested = []

        def request(_url, **kwargs):
            data = kwargs.get('data')
            if not data:
                self.requested.append('preview')
                return {'url': {'1': 'https://stream.qq.com/preview.m4a'}}
            name = data['req_0']['param']['filename'][0]
            self.requested.append(name)
            return {'req_0': {'data': {'midurlinfo': [{'purl': name + '?vkey=secret'}],
                                      'sip': ['https://stream.qq.com/']}}}
        self.bot._qq_request_json = Mock(side_effect=request)
        self.bot._download_qq_audio_url = AsyncMock(return_value=('raw', 200))
        self.bot.audio.fallback = AsyncMock(return_value=None)
        self.bot._refresh_qqmusic_cookie_if_needed = AsyncMock(return_value=False)

    async def asyncTearDown(self):
        await self.bot.cog_unload()
        self.folder.cleanup()

    async def acquire(self):
        return await self.bot._acquire_audio(self.value, self.root, self.root / 'done.opus')

    def decoder(self, errors):
        failures = iter(errors)
        def prepare(source, out):
            error = next(failures, None)
            if error:
                raise error
            cached(out)
        self.bot.audio.prepare = AsyncMock(side_effect=prepare)

    async def test_flac_then_320_failure_reaches_128_without_youtube(self):
        self.decoder([RuntimeError('bad flac'), RuntimeError('bad mp3')])
        self.assertTrue(await self.acquire())
        self.assertEqual(self.requested, ['F000mediamid.flac', 'M800mediamid.mp3', 'M500mediamid.mp3'])
        self.bot.audio.fallback.assert_not_awaited()

    async def test_cookie_refresh_does_not_consume_quality_fallback(self):
        self.bot.qqmusic_cookie = 'configured'
        self.bot._download_qq_audio_url.side_effect = [(None, 403), ('raw', 200), ('raw', 200), ('raw', 200)]
        self.bot._refresh_qqmusic_cookie_if_needed.return_value = True
        self.decoder([RuntimeError('bad flac'), RuntimeError('bad mp3')])
        self.assertTrue(await self.acquire())
        self.assertEqual(self.requested, ['F000mediamid.flac', 'F000mediamid.flac',
                                         'M800mediamid.mp3', 'M500mediamid.mp3'])
        self.bot._refresh_qqmusic_cookie_if_needed.assert_awaited_once()
        self.bot.audio.fallback.assert_not_awaited()

    async def test_missing_download_and_truncated_download_both_advance(self):
        self.bot._download_qq_audio_url.side_effect = [(None, 404), RuntimeError('incomplete'), ('raw', 200)]
        self.decoder([])
        self.assertTrue(await self.acquire())
        self.assertEqual(len(self.requested), 3)
        self.bot.audio.fallback.assert_not_awaited()

    async def test_all_qualities_are_bounded_and_preview_clears_stale_filename(self):
        self.decoder([RuntimeError('corrupt')] * 5)
        with patch('music.cog.yt_dlp', object()):
            self.assertIsNone(await self.acquire())
        self.assertEqual(self.requested, ['F000mediamid.flac', 'M800mediamid.mp3',
                                         'M500mediamid.mp3', 'C400songmid.m4a', 'preview'])
        self.assertNotIn('qq_filename', self.value)
        self.assertFalse((self.root / 'done.opus').exists())
        self.assertEqual([c.kwargs['source'] for c in self.bot.audio.fallback.await_args_list],
                         ['bilibili', 'youtube'])

    async def test_explicit_quality_is_respected(self):
        self.bot.qqmusic_quality = '320'
        self.decoder([RuntimeError('corrupt')])
        self.assertTrue(await self.acquire())
        self.assertEqual(self.requested, ['M800mediamid.mp3', 'preview'])

    async def test_429_never_refreshes_or_tries_other_qualities(self):
        self.bot.qqmusic_cookie = 'configured'
        self.bot._download_qq_audio_url.return_value = (None, 429)
        with patch('music.cog.yt_dlp', None):
            self.assertIsNone(await self.acquire())
        self.assertEqual(len(self.requested), 1)
        self.bot._refresh_qqmusic_cookie_if_needed.assert_not_awaited()
        self.assertGreater(self.bot.qq_audio_blocked_until, time.monotonic())

    async def test_same_bad_file_under_new_signatures_is_not_redownloaded(self):
        index = 0
        def same_file(_url, **kwargs):
            nonlocal index
            index += 1
            return {'req_0': {'data': {'midurlinfo': [{'purl': f'broken.flac?vkey={index}'}],
                                      'sip': ['https://stream.qq.com/']}}}
        self.bot._qq_request_json.side_effect = same_file
        self.decoder([RuntimeError('corrupt')])
        with patch('music.cog.yt_dlp', None):
            self.assertIsNone(await self.acquire())
        self.assertEqual(index, 5)
        self.bot._download_qq_audio_url.assert_awaited_once()

    async def test_unavailable_qualities_are_not_requeried_after_decode_failure(self):
        original = self.bot._qq_request_json.side_effect
        def request(url, **kwargs):
            result = original(url, **kwargs)
            if self.requested[-1] == 'F000mediamid.flac':
                result['req_0']['data']['midurlinfo'][0]['purl'] = ''
            return result
        self.bot._qq_request_json.side_effect = request
        self.decoder([RuntimeError('bad mp3')])
        self.assertTrue(await self.acquire())
        self.assertEqual(self.requested, ['F000mediamid.flac', 'M800mediamid.mp3', 'M500mediamid.mp3'])

    async def test_refresh_allows_previously_unavailable_quality_again(self):
        self.bot.qqmusic_cookie = 'configured'
        original = self.bot._qq_request_json.side_effect
        def request(url, **kwargs):
            result = original(url, **kwargs)
            if self.bot._refresh_qqmusic_cookie_if_needed.await_count == 0:
                if kwargs.get('data'):
                    result['req_0']['data']['midurlinfo'][0]['purl'] = ''
                else:
                    result['url'] = {}
            return result
        self.bot._qq_request_json.side_effect = request
        self.bot._refresh_qqmusic_cookie_if_needed.return_value = True
        self.decoder([])
        self.assertTrue(await self.acquire())
        self.bot._refresh_qqmusic_cookie_if_needed.assert_awaited_once()
        self.assertEqual(self.requested[-1], 'F000mediamid.flac')

    async def test_cancellation_does_not_start_next_quality_or_youtube(self):
        self.bot._download_qq_audio_url.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.acquire()
        self.assertEqual(len(self.requested), 1)
        self.bot.audio.fallback.assert_not_awaited()

    async def test_total_official_deadline_stops_quality_attempts(self):
        async def wait(*args):
            await asyncio.sleep(10)
        self.bot._download_qq_audio_url.side_effect = wait
        timeout = asyncio.timeout
        with patch('music.cog.asyncio.timeout', side_effect=lambda seconds: timeout(0.05)) as deadline:
            with patch('music.cog.yt_dlp', None):
                self.assertIsNone(await self.acquire())
        deadline.assert_called_once_with(180)
        self.assertEqual(len(self.requested), 1)
        self.assertFalse((self.root / 'done.opus').exists())

    async def test_unexpected_refresh_failure_keeps_existing_fallback(self):
        self.bot.qqmusic_cookie = 'configured'
        self.bot._download_qq_audio_url.return_value = (None, 403)
        self.bot._refresh_qqmusic_cookie_if_needed.side_effect = RuntimeError('refresh failed')
        with patch('music.cog.yt_dlp', object()):
            self.assertIsNone(await self.acquire())
        self.assertEqual(self.bot.audio.fallback.await_count, len(self.bot.fallback_sources))

    async def test_repeated_403_sets_cooldown_and_refreshes_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            bot.qqmusic_cookie = 'configured'
            with patch.object(bot, '_get_song_url', return_value='https://stream.qq.com/audio') as get:
                with patch.object(bot, '_download_qq_audio_url', AsyncMock(return_value=(None, 403))) as download:
                    with patch.object(bot, '_refresh_qqmusic_cookie_if_needed', AsyncMock(return_value=True)) as refresh:
                        with patch('music.cog.yt_dlp', None):
                            await bot._acquire_audio(song(), Path(directory), Path(directory) / 'a.opus')
                            await bot._acquire_audio(song('2'), Path(directory), Path(directory) / 'b.opus')
            self.assertEqual(get.call_count, 2)
            self.assertEqual(download.await_count, 2)
            refresh.assert_awaited_once()
            self.assertGreater(bot.qq_audio_blocked_until, time.monotonic())
            await bot.cog_unload()


class MusicPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_add_after_failure_starts_new_song(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            old, new = song('old'), song('new')
            data = bot.queues[1]
            data.update(current=old, agent_phase='error', playback_failed=True, error_count=3)
            interaction = SimpleNamespace(guild=guild, guild_id=1, channel=guild.voice_client.channel, client=bot.bot,
                user=SimpleNamespace(id=4, voice=SimpleNamespace(channel=guild.voice_client.channel)),
                followup=SimpleNamespace(send=AsyncMock()))
            with patch.object(bot, '_play_music_task', AsyncMock()) as start, patch.object(bot, 'update_player_ui', AsyncMock()):
                await bot._add_songs_to_queue(interaction, [new])
                await asyncio.gather(*bot.background_tasks)
            self.assertIs(data['current'], new)
            self.assertEqual(data['agent_phase'], 'loading')
            self.assertEqual(data['error_count'], 0)
            start.assert_awaited_once_with(1, new)
            await bot.cog_unload()

    async def test_recovery_preserves_pending_queue_and_cancels_old_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            old, pending, new = song('old'), song('pending'), song('new')
            data = bot.queues[1]
            task = asyncio.create_task(asyncio.Event().wait())
            data.update(current=old, queue=[pending], agent_phase='error', play_task=task)
            with patch.object(bot, '_play_music_task', AsyncMock()) as start, patch.object(bot, 'update_player_ui', AsyncMock()):
                result = bot.enqueue_songs(1, [new])
                await asyncio.gather(*bot.background_tasks)
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(result['recovered'])
            self.assertTrue(task.cancelled())
            self.assertIs(data['current'], pending)
            self.assertEqual(data['queue'], [new])
            start.assert_awaited_once_with(1, pending)
            await bot.cog_unload()

    async def test_add_does_not_interrupt_loading_playing_or_paused_track(self):
        for phase in ('loading', 'playing', 'paused'):
            with tempfile.TemporaryDirectory() as directory:
                bot, guild = music(Path(directory))
                old, new = song('old'), song('new')
                data = bot.queues[1]
                data.update(current=old, agent_phase=phase)
                guild.voice_client.playing = phase == 'playing'
                guild.voice_client.paused = phase == 'paused'
                with patch.object(bot, '_preload_next', AsyncMock()), patch.object(bot, '_play_music_task', AsyncMock()) as start:
                    result = bot.enqueue_songs(1, [new])
                    await asyncio.gather(*bot.background_tasks)
                self.assertFalse(result['started'])
                self.assertIs(data['current'], old)
                self.assertEqual(data['queue'], [new])
                start.assert_not_awaited()
                await bot.cog_unload()

    async def test_stop_and_new_song_cannot_be_undone_by_old_download(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            old, new = song(), song('2')
            bot.queues[1]['current'] = old
            started, release = asyncio.Event(), asyncio.Event()
            async def slow(_song):
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return '/unused/old.opus'
            with patch.object(bot, '_try_download_song', side_effect=slow):
                with patch.object(bot, 'update_player_ui', AsyncMock()):
                    with patch('music.cog.discord.FFmpegOpusAudio') as source:
                        task = asyncio.create_task(bot._play_music_task(1, old))
                        await started.wait()
                        await bot.stop_handling(1)
                        bot.queues[1]['current'] = new
                        guild.voice_client = Voice()
                        release.set()
                        await task
                        source.assert_not_called()
            self.assertIs(bot.queues[1]['current'], new)
            await bot.cog_unload()

    async def test_voice_disconnect_during_download_does_not_attempt_play(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            value = song()
            bot.queues[1]['current'] = value
            async def download(_song):
                guild.voice_client.connected = False
                return '/unused/track.opus'
            with patch.object(bot, '_try_download_song', side_effect=download):
                with patch.object(bot, 'update_player_ui', AsyncMock()):
                    await bot._play_music_task(1, value)
            guild.voice_client.play.assert_not_called()
            self.assertEqual(bot.queues[1]['agent_phase'], 'error')
            await bot.cog_unload()

    async def test_source_cleaned_if_discord_play_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            value = song()
            bot.queues[1]['current'] = value
            guild.voice_client.play.side_effect = RuntimeError('not connected')
            source = Mock()
            with patch.object(bot, '_try_download_song', AsyncMock(return_value='/unused/track.opus')):
                with patch.object(bot, 'update_player_ui', AsyncMock()):
                    with patch('music.cog.discord.FFmpegOpusAudio', return_value=source):
                        await bot._play_music_task(1, value)
            source.cleanup.assert_called_once()
            self.assertEqual(bot.queues[1]['error_count'], 1)
            await bot.cog_unload()

    async def test_duplicate_or_stale_after_callbacks_do_not_skip_multiple_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            bot.queues[1]['voice_generation'] = 4
            with patch.object(bot, 'play_next') as next_song:
                bot._handle_track_finished(1, 3, None)
                bot._handle_track_finished(1, 4, None)
                bot._handle_track_finished(1, 4, None)
            next_song.assert_called_once()
            await bot.cog_unload()

    async def test_shuffle_preview_matches_the_song_that_will_play(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            queue = bot.queues[1]
            queue['play_mode'] = 'shuffle'
            queue['queue'] = [song(str(i)) for i in range(6)]
            first = bot._preview_next_song(1)
            for _ in range(8):
                self.assertIs(bot._preview_next_song(1), first)
                self.assertIs(queue['queue'][bot._select_next_song_index(queue)], first)
            await bot.cog_unload()

    async def test_preload_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            bot.queues[1]['queue'] = [song()]
            release = asyncio.Event()
            async def download(_song):
                await release.wait()
            with patch.object(bot, '_try_download_song', side_effect=download) as download_mock:
                await bot._preload_next(1)
                await asyncio.sleep(0)
                await bot._preload_next(1)
                release.set()
                await asyncio.gather(*bot.background_tasks)
            self.assertEqual(download_mock.call_count, 1)
            await bot.cog_unload()

    async def test_same_channel_voice_connection_is_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            guild.voice_client = None
            channel = SimpleNamespace(id=3)
            async def connect(**kwargs):
                await asyncio.sleep(0.01)
                voice = Voice()
                voice.channel = channel
                guild.voice_client = voice
                return voice
            channel.connect = AsyncMock(side_effect=connect)
            member = SimpleNamespace(voice=SimpleNamespace(channel=channel))
            await asyncio.gather(*(bot._ensure_voice_client_for(guild, member) for _ in range(3)))
            channel.connect.assert_awaited_once()
            await bot.cog_unload()

    async def test_transient_panel_failure_does_not_spawn_duplicate_message(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            response = SimpleNamespace(status=500, reason='transient')
            data = bot.queues[1]
            message = SimpleNamespace(channel=bot.bot.get_guild(1).voice_client.channel,
                edit=AsyncMock(side_effect=discord.HTTPException(response, 'temporary')))
            data['message'] = message
            data['channel'] = bot.bot.get_guild(1).voice_client.channel
            await bot.update_player_ui(1)
            self.assertIs(data['message'], message)
            data['channel'].send.assert_not_awaited()
            await bot.cog_unload()

    async def test_concurrent_panel_creation_sends_only_one_message(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            data = bot.queues[1]
            data['channel'] = bot.bot.get_guild(1).voice_client.channel
            await asyncio.gather(bot.update_player_ui(1), bot.update_player_ui(1))
            data['channel'].send.assert_awaited_once()
            await bot.cog_unload()

    async def test_old_queue_selection_uses_song_identity_not_stale_index(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            a, b, c = song('a'), song('b'), song('c')
            bot.queues[1]['queue'] = [b, c]
            interaction = SimpleNamespace(guild_id=1)
            with patch.object(bot, 'update_player_ui', AsyncMock()):
                with patch.object(bot, '_preload_next', AsyncMock()):
                    name = await bot.prioritize_song(interaction, 2, expected_song=c)
                    missing = await bot.prioritize_song(interaction, 0, expected_song=a)
                    await asyncio.gather(*bot.background_tasks)
            self.assertEqual(name, 'Test')
            self.assertFalse(missing)
            self.assertIs(bot.queues[1]['queue'][0], c)
            await bot.cog_unload()

    async def test_panel_rejects_other_voice_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            interaction = SimpleNamespace(
                client=SimpleNamespace(), guild=guild, guild_id=1,
                user=SimpleNamespace(id=4, voice=SimpleNamespace(channel=SimpleNamespace(id=99))),
                response=SimpleNamespace(send_message=AsyncMock()),
            )
            self.assertFalse(await BaseView.interaction_check(SimpleNamespace(guild_id=1), interaction))
            interaction.response.send_message.assert_awaited_once()
            await bot.cog_unload()

    async def test_stop_button_acknowledges_before_waiting_for_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            events = []
            interaction = SimpleNamespace(
                guild=guild, guild_id=1,
                response=SimpleNamespace(defer=AsyncMock(side_effect=lambda: events.append('ack'))),
            )
            view = bot.queues[1]['view']
            with patch.object(bot, 'stop_handling', AsyncMock(side_effect=lambda _: events.append('stop'))):
                await MusicInterface.callback_stop(view, interaction)
            self.assertEqual(events, ['ack', 'stop'])
            await bot.cog_unload()


class MusicPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_cover_is_used_when_idle_or_song_cover_missing(self):
        self.assertTrue(DEFAULT_COVER_PATH.is_file())
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            data = bot.queues[1]
            for current in (None, song()):
                data['current'] = current
                data['view'].update_container()
                self.assertTrue(data['view'].uses_default_cover)
                self.assertIn(DEFAULT_COVER_URL, str(data['view'].to_components()))
            current = song()
            current['al']['picUrl'] = 'https://example.com/album.jpg?size=300'
            data['current'] = current
            data['view'].update_container()
            self.assertFalse(data['view'].uses_default_cover)
            self.assertIn(current['al']['picUrl'], str(data['view'].to_components()))
            self.assertNotIn(DEFAULT_COVER_URL, str(data['view'].to_components()))
            await bot.cog_unload()

    async def test_existing_default_attachment_is_not_uploaded_on_lyric_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            data = bot.queues[1]
            message = SimpleNamespace(channel=bot.bot.get_guild(1).voice_client.channel,
                attachments=[SimpleNamespace(filename=DEFAULT_COVER_FILENAME)], edit=AsyncMock())
            data['message'] = message
            await bot.update_player_ui(1)
            await bot.update_player_ui(1)
            self.assertEqual(message.edit.await_count, 2)
            for call in message.edit.await_args_list:
                self.assertNotIn('attachments', call.kwargs)
            await bot.cog_unload()

    async def test_returning_to_default_cover_adds_attachment_once_and_closes_file(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            data = bot.queues[1]
            preserved = SimpleNamespace(filename='another-file.txt')
            message = SimpleNamespace(channel=bot.bot.get_guild(1).voice_client.channel, attachments=[preserved])
            async def edit(**kwargs):
                if 'attachments' in kwargs:
                    message.attachments = [SimpleNamespace(filename=item.filename) for item in kwargs['attachments']]
            message.edit = AsyncMock(side_effect=edit)
            data['message'] = message
            await bot.update_player_ui(1)
            uploaded = message.edit.await_args.kwargs['attachments'][1]
            self.assertEqual(uploaded.filename, DEFAULT_COVER_FILENAME)
            self.assertTrue(uploaded.fp.closed)
            await bot.update_player_ui(1)
            self.assertNotIn('attachments', message.edit.await_args.kwargs)
            await bot.cog_unload()

    async def test_new_default_panel_uploads_supplied_image_and_closes_file(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            data = bot.queues[1]
            data['channel'] = bot.bot.get_guild(1).voice_client.channel
            await bot.update_player_ui(1)
            upload = data['channel'].send.await_args.kwargs['files'][0]
            self.assertEqual(upload.filename, DEFAULT_COVER_FILENAME)
            self.assertTrue(upload.fp.closed)
            await bot.cog_unload()

    async def test_edit_returned_message_is_kept_for_attachment_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            data = bot.queues[1]
            original = Mock(spec=discord.Message)
            original.channel = bot.bot.get_guild(1).voice_client.channel
            original.attachments = []
            returned = Mock(spec=discord.Message)
            returned.channel = original.channel
            returned.attachments = [SimpleNamespace(filename=DEFAULT_COVER_FILENAME)]
            original.edit = AsyncMock(return_value=returned)
            returned.edit = AsyncMock(return_value=returned)
            data['message'] = original
            await bot.update_player_ui(1)
            self.assertIs(data['message'], returned)
            await bot.update_player_ui(1)
            self.assertNotIn('attachments', returned.edit.await_args.kwargs)
            original.edit.assert_awaited_once()
            await bot.cog_unload()

    async def test_card_states_and_button_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            data = bot.queues[1]
            view = data['view']
            for phase, playing, paused, label in [
                ('loading', False, False, '正在加载'),
                ('playing', True, False, '正在播放'),
                ('paused', False, True, '已暂停'),
                ('error', False, False, '播放遇到问题'),
            ]:
                data.update(current=song(), agent_phase=phase)
                guild.voice_client.playing, guild.voice_client.paused = playing, paused
                view.update_container()
                payload = str(view.to_components())
                self.assertIn(label, payload)
                buttons = {item.custom_id: item for item in view.walk_children() if isinstance(item, discord.ui.Button)}
                self.assertEqual(buttons['music:pause'].disabled, not (playing or paused))
                self.assertFalse(buttons['music:skip'].disabled)
                self.assertNotIn('music:clear', buttons)  # Low-frequency controls live in More.
                self.assertIn('music:more', buttons)
                self.assertEqual(buttons['music:lyrics'].label, '歌词 · 开')
                self.assertLessEqual(view.total_children_count, 40)
            guild.voice_client.connected = False
            view.update_container()
            self.assertIn('语音已断开', str(view.to_components()))
            await bot.cog_unload()

    async def test_card_preserves_cover_url_and_bounds_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            value = song()
            url = 'https://example.com/cover.jpg?signature=sample&size=400'
            value.update(name='@everyone\n```' + '标题' * 4000, al={'picUrl': url, 'name': 'Album'})
            data = bot.queues[1]
            data.update(current=value, queue=[song('b'), song('c')])
            data['view'].update_container()
            payload = str(data['view'].to_components())
            self.assertIn(url, payload)
            self.assertNotIn('?param=', payload)
            self.assertNotIn('@everyone', payload)
            self.assertLess(len(payload), 6000)
            self.assertIn('待播 2 首', payload)
            self.assertIn('0:02', payload)
            await bot.cog_unload()

    async def test_clear_snapshot_keeps_current_and_later_additions(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            a, b, c, added = [song(i) for i in ('a', 'b', 'c', 'new')]
            snapshot = [a, b, c]
            data = bot.queues[1]
            data.update(current=a, queue=[b, c, added], priority_next=b, shuffle_next=c)
            with patch.object(bot, 'update_player_ui', AsyncMock()), patch.object(bot, '_preload_next', AsyncMock()):
                count = await bot.remove_pending_songs(1, snapshot)
                await asyncio.gather(*bot.background_tasks)
            self.assertEqual(count, 2)
            self.assertIs(data['current'], a)
            self.assertEqual(data['queue'], [added])
            self.assertIsNone(data['priority_next'])
            self.assertIsNone(data['shuffle_next'])
            await bot.cog_unload()

    async def test_remove_stale_song_does_not_remove_equal_new_object(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            old, new = song(), song()
            bot.queues[1]['queue'] = [new]
            count = await bot.remove_pending_songs(1, [old])
            self.assertEqual(count, 0)
            self.assertIs(bot.queues[1]['queue'][0], new)
            await bot.cog_unload()

    async def test_clear_confirmation_is_single_use_and_checks_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, guild = music(Path(directory))
            view = ClearQueueView(bot, 1, 4, [song()])
            interaction = SimpleNamespace(
                client=SimpleNamespace(), guild=guild, guild_id=1,
                channel=guild.voice_client.channel,
                user=SimpleNamespace(id=4, voice=SimpleNamespace(channel=guild.voice_client.channel)),
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                edit_original_response=AsyncMock(),
            )
            self.assertTrue(await view.interaction_check(interaction))
            interaction.user.voice.channel = SimpleNamespace(id=99)
            self.assertFalse(await view.interaction_check(interaction))
            with patch.object(bot, 'remove_pending_songs', AsyncMock(return_value=1)) as remove:
                await asyncio.gather(view.confirm(interaction), view.confirm(interaction))
            remove.assert_awaited_once()
            await bot.cog_unload()

    async def test_skip_loading_invalidates_old_work_and_does_not_double_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            old, new = song('a'), song('b')
            data = bot.queues[1]
            task = asyncio.create_task(asyncio.Event().wait())
            data.update(current=old, queue=[new], agent_phase='loading', play_task=task)
            old_generation = data['voice_generation']
            with patch.object(bot, 'update_player_ui', AsyncMock()), patch.object(bot, '_play_music_task', AsyncMock()):
                self.assertTrue(await bot.skip_current(1, old))
                self.assertFalse(await bot.skip_current(1, old))
                bot._handle_track_finished(1, old_generation, None)
                await asyncio.gather(*bot.background_tasks)
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
            self.assertIs(data['current'], new)
            await bot.cog_unload()

    async def test_queue_remove_button_handles_changed_order(self):
        with tempfile.TemporaryDirectory() as directory:
            bot, _ = music(Path(directory))
            a, b, c = song('a'), song('b'), song('c')
            view = QueueListView(bot, 1, [a, b, c])
            view.selected_index = 1
            bot.queues[1]['queue'] = [c, b]
            interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()))
            with patch.object(bot, 'update_player_ui', AsyncMock()), patch.object(bot, '_preload_next', AsyncMock()):
                await view.on_remove(interaction)
                await asyncio.gather(*bot.background_tasks)
            self.assertEqual(bot.queues[1]['queue'], [c])
            self.assertEqual(view.queue, [c])
            await bot.cog_unload()


class MusicCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.bot, _ = music(self.root)
        self.downloaded = []

        async def download(value):
            self.downloaded.append(value['id'])
        self.download = patch.object(self.bot, '_try_download_song', side_effect=download)
        self.download.start()

    async def asyncTearDown(self):
        self.download.stop()
        await self.bot.cog_unload()
        self.folder.cleanup()

    async def test_sequential_preload_warms_following_tracks_once(self):
        self.bot.queues[1]['queue'] = [song(str(i)) for i in range(5)]
        await self.bot._preload_next(1)
        await self.bot._preload_next(1)
        await asyncio.gather(*self.bot.background_tasks)
        self.assertEqual(sorted(self.downloaded), ['0', '1', '2'])
        self.assertEqual(self.bot.prefetch_keys, set())

    async def test_shuffle_preloads_only_the_chosen_next_track(self):
        queue = self.bot.queues[1]
        queue['play_mode'] = 'shuffle'
        queue['queue'] = [song(str(i)) for i in range(5)]
        await self.bot._preload_next(1)
        await asyncio.gather(*self.bot.background_tasks)
        self.assertEqual(self.downloaded, [queue['shuffle_next']['id']])

    async def test_prefetch_pauses_while_qq_audio_cools_down(self):
        self.bot.queues[1]['queue'] = [song(str(i)) for i in range(3)]
        self.bot.qq_audio_blocked_until = time.monotonic() + 60
        await self.bot._preload_next(1)
        await asyncio.gather(*self.bot.background_tasks)
        self.assertEqual(self.downloaded, ['0'])

    async def test_cleaner_keeps_recent_cache_until_size_cap(self):
        paths = [self.root / f'v2_{i}.opus' for i in range(3)]
        for age, path in zip((300, 200, 100), paths):
            cached(path)
            os.utime(path, (time.time() - age, time.time() - age))
        self.assertEqual(self.bot._clean_audio_cache(), 0)
        self.bot.cache_max_bytes = paths[0].stat().st_size * 2
        self.assertEqual(self.bot._clean_audio_cache(), 1)
        self.assertEqual([p.exists() for p in paths], [False, True, True])


class MusicFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.bot, _ = music(self.root)
        self.bot.qq_audio_blocked_until = time.monotonic() + 60
        self.value = dict(song(), mid='songmid', dt=200_000)

    async def asyncTearDown(self):
        await self.bot.cog_unload()
        self.folder.cleanup()

    async def test_next_source_is_tried_after_blocked_search_or_bad_file(self):
        found = self.root / 'fallback-youtube.m4a'
        self.bot.audio.fallback = AsyncMock(side_effect=[RuntimeError('HTTP Error 412'), found])
        self.bot.audio.prepare = AsyncMock(side_effect=lambda _source, out: cached(out))
        with patch('music.cog.yt_dlp', object()):
            result = await self.bot._acquire_audio(self.value, self.root, self.root / 'done.opus')
        self.assertEqual(result, str(self.root / 'done.opus'))
        calls = self.bot.audio.fallback.await_args_list
        self.assertEqual([c.kwargs['source'] for c in calls], ['bilibili', 'youtube'])
        self.assertEqual([c.args[0] for c in calls], ['Test Artist', 'Test Artist audio'])
        self.assertEqual(calls[0].kwargs['duration'], 200)

    async def test_configured_sources_are_validated_and_ordered(self):
        with patch.dict(os.environ, {'MUSIC_FALLBACK_SOURCES': 'youtube, nope bilibili youtube'}):
            self.assertEqual(self.bot._resolve_fallback_sources(), ('youtube', 'bilibili'))
        with patch.dict(os.environ, {'MUSIC_FALLBACK_SOURCES': ''}):
            self.assertEqual(self.bot._resolve_fallback_sources(), ())

    async def test_pipeline_filters_search_hits_by_song_length(self):
        pipeline = AudioPipeline('ffmpeg', max_bytes=1024 * 1024)
        pipeline._run = AsyncMock()
        await pipeline.fallback('Song Artist', self.root, source='bilibili', duration=200)
        command = pipeline._run.await_args.args[0]
        self.assertEqual(command[-1], 'bilisearch3:Song Artist')
        self.assertIn('duration>=180 & duration<=220', command)
        self.assertEqual(pipeline._run.await_args.kwargs['ok_codes'], (0, 101))
        await pipeline.fallback('Song Artist audio', self.root, source='youtube')
        command = pipeline._run.await_args.args[0]
        self.assertEqual(command[-1], 'ytsearch1:Song Artist audio')
        self.assertNotIn('--match-filters', command)

    async def test_cookie_file_is_copied_only_when_yt_dlp_can_read_it(self):
        cookie = self.root / 'cookies.txt'
        cookie.write_text('# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tx\n')
        work = self.root / 'work'
        work.mkdir()
        with patch.dict(os.environ, {'BILIBILI_COOKIE_FILE': str(cookie)}):
            copied = self.bot._fallback_cookie_file('bilibili', work)
            self.assertEqual(copied.read_text(), cookie.read_text())
            self.assertNotEqual(copied, cookie)
            self.assertIsNone(self.bot._fallback_cookie_file('youtube', work))
            cookie.write_text('SESSDATA=x; bili_jct=y')
            self.assertIsNone(self.bot._fallback_cookie_file('bilibili', work))


if __name__ == '__main__':
    unittest.main()
