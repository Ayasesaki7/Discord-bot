from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
from discord.http import handle_message_parameters

from bilibili.cog import (
    BilibiliApiError, BilibiliCandidate, BilibiliRiskControlError,
    BilibiliVideoCog, BilibiliVideoTooLarge, DownloadedBilibiliVideo,
)

BV = 'BV1mJEj6uEAK'
URL = f'https://www.bilibili.com/video/{BV}/'


def cog():
    result = object.__new__(BilibiliVideoCog)
    result.max_height = 480
    result.download_max_bytes = 128 * 1024 * 1024
    result.download_timeout_seconds = 240
    result.max_upload_bytes_override = None
    result.compress_if_needed = False
    result.ffmpeg_executable = 'ffmpeg'
    result.cookie_file = None
    result.download_lock = asyncio.Lock()
    result.risk_blocked_until = 0.0
    result.risk_cooldown_seconds = 120
    result.title_cache = {}
    result.title_blocked_until = 0.0
    result._download_deadline = 0.0
    result._cleanup_tasks = set()
    # Isolate optional title transport from the media resolver tests below.
    result._read_archive_title = Mock(return_value='原视频投稿标题')
    return result


def pages():
    return {'code': 0, 'data': [
        {'page': 1, 'cid': 123, 'part': 'first', 'duration': 3},
        {'page': 2, 'cid': 456, 'part': 'second', 'duration': 3},
    ]}


def playback(**updates):
    data = {'format': 'mp4', 'timelength': 3000,
            'durl': [{'url': 'https://cdn.example/video.mp4', 'size': 2048}]}
    data.update(updates)
    return {'code': 0, 'data': data}


def download(_urls, destination, **_kwargs):
    destination.write_bytes(b'v' * 2048)


class BilibiliResolverTests(unittest.TestCase):
    def test_share_sentence_punctuation_not_part_of_url(self):
        bot = cog()
        self.assertEqual(bot._find_candidate(URL + '?p=2。').source_url, URL + '?p=2')
        self.assertEqual(bot._find_candidate('看看 https://b23.tv/ABC123！').source_url,
                         'https://b23.tv/ABC123')

    def test_cookie_jar_respects_domain_path_and_expiry(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            bot.cookie_file = Path(directory) / 'cookies.txt'
            bot.cookie_file.write_text(
                '# Netscape HTTP Cookie File\n'
                '.bilibili.com\tTRUE\t/\tTRUE\t4102444800\tSESSDATA\tgood\n'
                '.bilibili.com\tTRUE\t/\tTRUE\t1\texpired\tbad\n'
                '.bilibili.com\tTRUE\t/private\tTRUE\t4102444800\tprivate\tbad\n'
                '.other.com\tTRUE\t/\tTRUE\t4102444800\tsecret\tbad\n', encoding='utf-8')
            self.assertEqual(bot._cookie_header(), 'SESSDATA=good')
            bot.cookie_file.write_text('Cookie: SESSDATA=a; bili_jct=b', encoding='utf-8')
            self.assertEqual(bot._cookie_header(), 'SESSDATA=a; bili_jct=b')

    def test_slow_trickling_download_obeys_total_deadline_and_removes_partial(self):
        bot = cog()
        bot._download_deadline = 2.0
        response = MagicMock()
        response.__enter__.return_value.headers = {'Content-Type': 'video/mp4'}
        response.__enter__.return_value.read1.return_value = b'v' * 2048
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            with patch('bilibili.cog.urllib.request.urlopen', return_value=response) as fetch, \
                    patch('bilibili.cog.time.monotonic', side_effect=[1.0, 1.0, 1.0, 3.0]):
                with self.assertRaises(TimeoutError):
                    bot._download_direct_file(['https://cdn.example/a', 'https://cdn.example/b'], path, referer=URL)
            self.assertFalse(path.exists())
            fetch.assert_called_once()

    def test_direct_api_uses_pagelist_and_playurl_without_webpage_or_view(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_read_bilibili_json', side_effect=[pages(), playback()]) as read:
                with patch.object(bot, '_download_direct_file', side_effect=download):
                    video = bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)
        self.assertEqual(video.title, '原视频投稿标题')
        self.assertEqual(video.part_title, 'first')
        self.assertEqual(video.duration_seconds, 3)
        self.assertEqual(read.call_count, 2)
        self.assertIn('/x/player/pagelist?', read.call_args_list[0].args[0])
        self.assertIn('/x/player/playurl?', read.call_args_list[1].args[0])
        self.assertNotIn('/view', str(read.call_args_list))

    def test_selected_part_is_preserved_in_playback_and_result_url(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_read_bilibili_json', side_effect=[pages(), playback()]) as read:
                with patch.object(bot, '_download_direct_file', side_effect=download):
                    video = bot._download_video(BilibiliCandidate(URL + '?p=2', BV), Path(directory), 8000)
        self.assertEqual(video.title, '原视频投稿标题')
        self.assertEqual(video.part_title, 'second')
        self.assertIn('cid=456', read.call_args_list[1].args[0])
        self.assertTrue(video.webpage_url.endswith('?p=2'))

    def test_invalid_part_fails_without_guessing_first_part(self):
        for query in ('p=0', 'p=wrong', 'p=99'):
            with self.subTest(query=query):
                bot = cog()
                with patch.object(bot, '_read_bilibili_json', return_value=pages()):
                    with self.assertRaises(BilibiliApiError):
                        bot._read_page_info(BilibiliCandidate(URL + '?' + query, BV))

    def test_metadata_risk_error_does_not_repeat_per_height(self):
        bot = cog()
        with patch.object(bot, '_read_bilibili_json', side_effect=BilibiliRiskControlError('412')) as read:
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(BilibiliRiskControlError):
                bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)
        self.assertEqual(read.call_count, 1)

    def test_playback_errors_do_not_repeat_per_height(self):
        for error in (BilibiliRiskControlError('412'), BilibiliApiError('login required')):
            with self.subTest(error=type(error)):
                bot = cog()
                with patch.object(bot, '_read_bilibili_json', side_effect=[pages(), error]) as read:
                    with tempfile.TemporaryDirectory() as directory, self.assertRaises(BilibiliApiError):
                        bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)
                self.assertEqual(read.call_count, 2)

    def test_only_size_errors_try_lower_quality_without_reloading_metadata(self):
        bot = cog()
        too_large = playback(durl=[{'url': 'https://cdn.example/a', 'size': bot.download_max_bytes + 1}])
        with patch.object(bot, '_read_bilibili_json', side_effect=[pages(), too_large, playback()]) as read:
            with patch.object(bot, '_download_direct_file', side_effect=download):
                with tempfile.TemporaryDirectory() as directory:
                    bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)
        self.assertEqual(read.call_count, 3)
        self.assertIn('qn=32', read.call_args_list[1].args[0])
        self.assertIn('qn=16', read.call_args_list[2].args[0])
        bot._read_archive_title.assert_called_once()

    def test_archive_title_is_used_instead_of_upload_filename(self):
        bot = cog()
        bot._read_archive_title.return_value = '头像是美羊羊，但结局不再是灰太狼'
        data = {'code': 0, 'data': [{'page': 1, 'cid': 123, 'part': 'lv_0_20250716001911'}]}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_read_bilibili_json', side_effect=[data, playback()]):
                with patch.object(bot, '_download_direct_file', side_effect=download):
                    video = bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)
        self.assertEqual(video.title, '头像是美羊羊，但结局不再是灰太狼')
        self.assertIsNone(video.part_title)

    def test_missing_title_does_not_fall_back_to_filename_or_block_download(self):
        bot = cog()
        bot._read_archive_title.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_read_bilibili_json', side_effect=[pages(), playback()]):
                with patch.object(bot, '_download_direct_file', side_effect=download):
                    video = bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)
        self.assertEqual(video.title, f'B 站视频 · {BV}')

    def test_title_wbi_lookup_matches_bvid_and_is_cached(self):
        bot = cog()
        with patch.object(bot, '_read_bilibili_json', return_value={'code': 0, 'data': {'bvid': BV, 'title': '真正标题'}}) as read:
            for _ in range(2):
                self.assertEqual(BilibiliVideoCog._read_archive_title(bot, BilibiliCandidate(URL, BV)), '真正标题')
        read.assert_called_once()
        self.assertIn('/x/web-interface/wbi/view?', read.call_args.args[0])
        self.assertEqual(read.call_args.kwargs['timeout'], 6)

    def test_title_risk_only_cools_title_queries_not_media_downloads(self):
        bot = cog()
        with patch.object(bot, '_read_bilibili_json', side_effect=BilibiliRiskControlError('412')) as read:
            self.assertIsNone(BilibiliVideoCog._read_archive_title(bot, BilibiliCandidate(URL, BV)))
            self.assertIsNone(BilibiliVideoCog._read_archive_title(bot, BilibiliCandidate(URL, 'BV1XkupzsEAR')))
        read.assert_called_once()
        self.assertEqual(bot.risk_blocked_until, 0)

    def test_invalid_title_data_is_not_used_and_failure_is_cached(self):
        for data in ({'bvid': 'wrong', 'title': '错误视频'}, {'bvid': BV, 'title': {}}, None):
            bot = cog()
            with patch.object(bot, '_read_bilibili_json', return_value={'code': 0, 'data': data}) as read:
                for _ in range(2):
                    self.assertIsNone(BilibiliVideoCog._read_archive_title(bot, BilibiliCandidate(URL, BV)))
            read.assert_called_once()

    def test_optional_title_connection_reset_does_not_block_video(self):
        bot = cog()
        with patch.object(bot, '_read_bilibili_json', side_effect=ConnectionResetError()):
            self.assertIsNone(BilibiliVideoCog._read_archive_title(bot, BilibiliCandidate(URL, BV)))
        self.assertEqual(bot.risk_blocked_until, 0)

    def test_all_segments_are_downloaded_and_merged(self):
        bot = cog()
        data = playback(durl=[
            {'url': 'https://cdn.example/1', 'size': 2048},
            {'url': 'https://cdn.example/2', 'size': 2048},
        ])
        def merge(paths, output, **kwargs):
            self.assertEqual(len(paths), 2)
            self.assertTrue(kwargs['concatenate'])
            output.write_bytes(b''.join(path.read_bytes() for path in paths))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_read_bilibili_json', side_effect=[pages(), data]):
                with patch.object(bot, '_download_direct_file', side_effect=download) as fetch:
                    with patch.object(bot, '_merge_media', side_effect=merge):
                        video = bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)
                        self.assertEqual(video.file_path.stat().st_size, 4096)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[1].kwargs['limit'], bot.download_max_bytes - 2048)

    def test_preview_is_not_presented_as_complete_video(self):
        bot = cog()
        with patch.object(bot, '_read_bilibili_json', side_effect=[pages(), playback(is_preview=1)]):
            with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(BilibiliApiError, '试看'):
                bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)

    def test_dash_downloads_video_and_audio_tracks(self):
        bot = cog()
        data = playback(durl=None, dash={
            'video': [
                {'baseUrl': 'https://cdn.example/hd', 'height': 1080, 'codecs': 'avc1'},
                {'base_url': 'https://cdn.example/sd', 'height': 480, 'codecs': 'avc1'},
            ],
            'audio': [{'baseUrl': 'https://cdn.example/audio', 'codecs': 'mp4a', 'bandwidth': 64000}],
        })
        def merge(paths, output, **kwargs):
            self.assertEqual(len(paths), 2)
            self.assertFalse(kwargs['concatenate'])
            output.write_bytes(b'm' * 3000)
        with patch.object(bot, '_read_bilibili_json', side_effect=[pages(), data]):
            with patch.object(bot, '_download_direct_file', side_effect=download) as fetch:
                with patch.object(bot, '_merge_media', side_effect=merge):
                    with tempfile.TemporaryDirectory() as directory:
                        bot._download_video(BilibiliCandidate(URL, BV), Path(directory), 8000)
        self.assertEqual(fetch.call_args_list[0].args[0], ['https://cdn.example/sd'])
        self.assertEqual(fetch.call_args_list[1].args[0], ['https://cdn.example/audio'])

    def test_shortlink_reads_location_without_fetching_destination_html(self):
        bot = cog()
        error = urllib.error.HTTPError('https://b23.tv/abc', 302, 'Found', {'Location': URL + '?p=2'}, io.BytesIO())
        opener = Mock()
        opener.open.side_effect = error
        with patch('bilibili.cog.urllib.request.build_opener', return_value=opener):
            candidate = bot._resolve_candidate(BilibiliCandidate('https://b23.tv/abc', None))
        self.assertEqual(candidate.bvid, BV)
        self.assertTrue(candidate.source_url.endswith('?p=2'))
        self.assertEqual(opener.open.call_count, 1)

    def test_shortlink_refuses_external_redirect(self):
        bot = cog()
        error = urllib.error.HTTPError('https://b23.tv/abc', 302, 'Found', {'Location': 'http://127.0.0.1/private'}, io.BytesIO())
        opener = Mock()
        opener.open.side_effect = error
        with patch('bilibili.cog.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(BilibiliApiError, '非 B 站'):
                bot._resolve_candidate(BilibiliCandidate('https://b23.tv/abc', None))
        self.assertEqual(opener.open.call_count, 1)

    def test_json_http_and_api_risk_errors_are_classified(self):
        bot = cog()
        for status in (412, 429):
            error = urllib.error.HTTPError(URL, status, 'blocked', {}, io.BytesIO())
            with patch('bilibili.cog.urllib.request.urlopen', side_effect=error):
                with self.assertRaises(BilibiliRiskControlError):
                    bot._read_bilibili_json('https://api.bilibili.com/x/player/pagelist', referer=URL)
        response = MagicMock()
        for code in (-352, -412):
            response.__enter__.return_value.read.return_value = json.dumps({'code': code}).encode()
            with patch('bilibili.cog.urllib.request.urlopen', return_value=response):
                with self.assertRaises(BilibiliRiskControlError):
                    bot._read_bilibili_json('https://api.bilibili.com/x/player/playurl', referer=URL)

    def test_download_does_not_send_account_cookies_to_cdn(self):
        bot = cog()
        response = MagicMock()
        response.__enter__.return_value.headers = {'Content-Type': 'video/mp4'}
        response.__enter__.return_value.read1.side_effect = [b'a' * 2048, b'']
        with patch.object(bot, '_cookie_header', return_value='SESSDATA=private'):
            self.assertIn('Cookie', bot._bilibili_headers(URL))
            with patch('bilibili.cog.urllib.request.urlopen', return_value=response) as fetch:
                with tempfile.TemporaryDirectory() as directory:
                    bot._download_direct_file(['https://cdn.example/video'], Path(directory) / 'video.mp4', referer=URL)
        self.assertNotIn('Cookie', fetch.call_args.args[0].headers)

    def test_download_limits_are_shared_and_do_not_retry_same_oversize_file(self):
        bot = cog()
        response = MagicMock()
        response.__enter__.return_value.headers = {'Content-Type': 'video/mp4', 'Content-Length': '4096'}
        with patch('bilibili.cog.urllib.request.urlopen', return_value=response) as fetch:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'video.mp4'
                with self.assertRaises(BilibiliVideoTooLarge):
                    bot._download_direct_file(['https://cdn.example/a', 'https://cdn.example/b'], path, referer=URL, limit=2048)
                self.assertFalse(path.exists())
        self.assertEqual(fetch.call_count, 1)

    def test_truncated_download_cannot_be_sent_as_complete_video(self):
        bot = cog()
        response = MagicMock()
        response.__enter__.return_value.headers = {'Content-Type': 'video/mp4', 'Content-Length': '4096'}
        response.__enter__.return_value.read1.side_effect = [b'a' * 2048, b'']
        with patch('bilibili.cog.urllib.request.urlopen', return_value=response):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'video.mp4'
                with self.assertRaises(BilibiliApiError):
                    bot._download_direct_file(['https://cdn.example/a'], path, referer=URL)
                self.assertFalse(path.exists())

    def test_error_html_cannot_be_sent_as_mp4(self):
        bot = cog()
        response = MagicMock()
        response.__enter__.return_value.headers = {'Content-Type': 'text/html'}
        with patch('bilibili.cog.urllib.request.urlopen', return_value=response):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'video.mp4'
                with self.assertRaises(BilibiliApiError):
                    bot._download_direct_file(['https://cdn.example/a'], path, referer=URL)
                self.assertFalse(path.exists())


class BilibiliCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_is_inside_container_with_title_metadata_and_source_link(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'v' * 2048)
            video = DownloadedBilibiliVideo('示例 **视频** @everyone', URL + '?p=2', BV, path, 198.966)
            view = bot._build_video_card(video, f'{BV}.mp4')
            components = view.to_components()
            self.assertEqual(len(components), 1)
            container = components[0]
            self.assertEqual(container['type'], 17)
            self.assertEqual(container['accent_color'], 0xFB7299)
            children = container['components']
            media = next(item for item in children if item['type'] == 12)
            self.assertEqual(media['items'][0]['media']['url'], f'attachment://{BV}.mp4')
            texts = '\n'.join(item['content'] for item in children if item['type'] == 10)
            self.assertIn('03:19', texts)
            self.assertIn('2.0 KB', texts)
            self.assertIn('P2', texts)
            self.assertNotIn('@everyone', texts)
            button = next(item for item in children if item['type'] == 1)['components'][0]
            self.assertEqual(button['url'], URL + '?p=2')
            self.assertEqual(button['style'], 5)
            view.stop()

    async def test_card_serializes_as_components_v2_with_video_attachment(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'v' * 2048)
            video = DownloadedBilibiliVideo('title', URL, BV, path, 3)
            view = bot._build_video_card(video, f'{BV}.mp4')
            with handle_message_parameters(
                view=view, file=discord.File(path, filename=f'{BV}.mp4'),
                allowed_mentions=discord.AllowedMentions.none(),
            ) as parameters:
                payload = json.loads(next(item['value'] for item in parameters.multipart if item['name'] == 'payload_json'))
                self.assertTrue(payload['flags'] & 32768)
                self.assertNotIn('content', payload)
                self.assertNotIn('embeds', payload)
                self.assertEqual(payload['attachments'][0]['filename'], f'{BV}.mp4')
                self.assertEqual(payload['allowed_mentions']['parse'], [])
            view.stop()

    async def test_both_command_entrypoints_send_video_cards(self):
        for slash in (True, False):
            with self.subTest(slash=slash), tempfile.TemporaryDirectory() as directory:
                bot = cog()
                path = Path(directory) / 'video.mp4'
                path.write_bytes(b'v' * 2048)
                video = DownloadedBilibiliVideo('title', URL, BV, path, 3)
                send = AsyncMock()

                async def resolve(**kwargs):
                    await kwargs['send_video'](video, f'{BV}.mp4')

                with patch.object(bot, '_resolve_and_send_video', side_effect=resolve):
                    if slash:
                        interaction = SimpleNamespace(
                            channel=SimpleNamespace(send=AsyncMock()), guild=None,
                            response=SimpleNamespace(defer=AsyncMock()),
                            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=send),
                        )
                        await BilibiliVideoCog.send_bilibili_video.callback(bot, interaction, BV)
                    else:
                        context = SimpleNamespace(
                            channel=SimpleNamespace(send=send), guild=None,
                            author=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
                        )
                        await BilibiliVideoCog.send_bilibili_video_prefix.callback(bot, context, link_or_bv=BV)
                send.assert_awaited_once()
                self.assertIsInstance(send.call_args.kwargs['view'], discord.ui.LayoutView)
                self.assertNotIn('content', send.call_args.kwargs)
                self.assertEqual(send.call_args.kwargs['file'].filename, f'{BV}.mp4')
                send.call_args.kwargs['file'].close()
                send.call_args.kwargs['view'].stop()


class BilibiliCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_and_cancel_keep_lock_and_files_until_thread_exits(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                bot = cog()
                bot.download_timeout_seconds = 1 if cancel else 0.05
                entered = threading.Event()
                finish = threading.Event()
                paths = []

                def work(_candidate, directory, _limit):
                    paths.append(directory)
                    (directory / 'pending').write_text('active')
                    entered.set()
                    finish.wait(5)
                    raise TimeoutError('finished')

                with tempfile.TemporaryDirectory() as directory:
                    with patch.object(bot, '_temp_root', return_value=Path(directory)), \
                            patch.object(bot, '_download_video', side_effect=work):
                        task = asyncio.create_task(bot._resolve_and_send_video(
                            guild=None, candidate=BilibiliCandidate(URL, BV),
                            update_status=AsyncMock(), send_video=AsyncMock()))
                        try:
                            if cancel:
                                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                                task.cancel()
                            with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                                await task
                            self.assertTrue(paths[0].exists())
                            self.assertTrue(bot.download_lock.locked())
                            with self.assertRaisesRegex(BilibiliApiError, '清理'):
                                await bot._resolve_and_send_video(
                                    guild=None, candidate=BilibiliCandidate(URL, BV),
                                    update_status=AsyncMock(), send_video=AsyncMock())
                        finally:
                            finish.set()
                            await asyncio.gather(*bot._cleanup_tasks)
                self.assertFalse(paths[0].exists())
                self.assertFalse(bot.download_lock.locked())

    async def test_new_commands_do_not_hit_server_during_risk_cooldown(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_temp_root', return_value=Path(directory)):
                with patch.object(bot, '_download_video', side_effect=BilibiliRiskControlError('412')) as fetch:
                    for _ in range(2):
                        with self.assertRaises(BilibiliRiskControlError):
                            await bot._resolve_and_send_video(
                                guild=None, candidate=BilibiliCandidate(URL, BV),
                                update_status=AsyncMock(), send_video=AsyncMock(),
                            )
        self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
