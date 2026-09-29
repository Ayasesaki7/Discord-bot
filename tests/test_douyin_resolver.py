from __future__ import annotations

import asyncio
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from http.cookiejar import CookieJar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.http import handle_message_parameters

from douyin.cog import DouyinCandidate, DouyinVideoCog, DownloadedDouyinVideo
from douyin.public import (
    DOUYIN_HOSTS, DouyinAccessError, DouyinPublicError, open_public_url,
    DouyinWatermarkError, is_watermarked_url, unwatermarked_url,
    parse_public_video, validate_url, video_id_from_url,
)

VIDEO_ID = '6961737553342991651'
URL = f'https://www.douyin.com/video/{VIDEO_ID}'


def cog():
    result = object.__new__(DouyinVideoCog)
    result.max_height = 1080
    result.compress_width = 1080
    result.download_timeout_seconds = 240
    result.download_max_bytes = 192 * 1024 * 1024
    result.max_upload_bytes_override = None
    result.compress_if_needed = True
    result.ffmpeg_executable = 'ffmpeg'
    result.cookie_file = None
    result.cookies_from_browser = None
    result.public_share_enabled = True
    result.use_f2 = False
    result.f2_available = False
    result.parse_api_urls = []
    result.parse_api_timeout_seconds = 30
    result.download_lock = asyncio.Lock()
    result._cleanup_tasks = set()
    result._download_deadline = 0.0
    result.risk_blocked_until = 0.0
    result.risk_cooldown_seconds = 120
    return result


def metadata(video_id=VIDEO_ID):
    return {'aweme_id': video_id, 'desc': '公开样例', 'video': {
        'duration': 31500, 'cover': {'url_list': ['https://p.example/cover.jpg']},
        'play_addr': {'url_list': ['https://v.example/video.mp4']},
    }}


def page():
    return 'window._ROUTER_DATA = ' + json.dumps({
        'loaderData': {'video_(id)/page': {'videoInfoRes': {'item_list': [metadata()]}}},
    }) + ';</script>'


class Response(io.BytesIO):
    def __init__(self, body=b'v' * 4096, content_type='video/mp4', length=None, url=URL):
        super().__init__(body)
        self.headers = {'Content-Type': content_type}
        if length is not None:
            self.headers['Content-Length'] = str(length)
        self.url = url

    def geturl(self):
        return self.url


class PublicPageTests(unittest.TestCase):
    def test_official_share_watermark_url_selects_normal_playback(self):
        original = ('https://aweme.snssdk.com/aweme/v1/playwm/?line=0&logo_name=aweme'
                    '&ratio=720p&video_id=long-video-id&watermark=1')
        item = metadata()
        item['video']['play_addr']['url_list'] = [original]
        result = parse_public_video('window._ROUTER_DATA=' + json.dumps(item), VIDEO_ID)
        parsed = urllib.parse.urlsplit(result.video_url)
        self.assertEqual(parsed.path, '/aweme/v1/play/')
        self.assertEqual(urllib.parse.parse_qs(parsed.query),
                         {'line': ['0'], 'ratio': ['720p'], 'video_id': ['long-video-id']})
        self.assertFalse(is_watermarked_url(result.video_url))

    def test_supplied_direct_playback_preferred_over_watermarked_share_url(self):
        item = metadata()
        item['video']['play_addr']['url_list'] = ['https://aweme.snssdk.com/aweme/v1/playwm/?video_id=original']
        item['video']['bit_rate'] = [{'play_addr': {'url_list': ['https://v.example/original.mp4']}}]
        item['video']['download_addr'] = {'url_list': ['https://v.example/watermark/download.mp4']}
        self.assertEqual(parse_public_video('window._ROUTER_DATA=' + json.dumps(item), VIDEO_ID).video_url,
                         'https://v.example/original.mp4')

    def test_only_unknown_watermark_source_is_rejected_without_rewriting(self):
        item = metadata()
        for url in ('https://other.example/aweme/v1/playwm/?video_id=abc',
                    'https://aweme.snssdk.com.attacker.example/aweme/v1/playwm/?video_id=abc',
                    'https://aweme.snssdk.com/aweme/v1/playwm/?line=0',
                    'https://v.example/a.mp4?watermark=1'):
            with self.subTest(url=url):
                item['video']['play_addr']['url_list'] = [url]
                with self.assertRaises(DouyinWatermarkError):
                    parse_public_video('window._ROUTER_DATA=' + json.dumps(item), VIDEO_ID)

    def test_ordinary_signed_cdn_url_is_not_modified(self):
        url = 'https://v.example/original.mp4?signature=a%2Fb%3D&ratio=1080p'
        self.assertEqual(unwatermarked_url(url), url)

    def test_router_data_extracts_matching_video_not_cover(self):
        item = parse_public_video(page(), VIDEO_ID)
        self.assertEqual(item.video_id, VIDEO_ID)
        self.assertEqual(item.duration_seconds, 31.5)
        self.assertTrue(item.video_url.endswith('video.mp4'))

    def test_render_data_and_camel_case(self):
        item = {'awemeId': VIDEO_ID, 'desc': 'test', 'video': {
            'playAddr': {'urlList': ['https://v.example/v.mp4']},
        }}
        body = urllib.parse.quote(json.dumps({'item': item}))
        result = parse_public_video(f'<script id="RENDER_DATA" type="application/json">{body}</script>', VIDEO_ID)
        self.assertIsNone(result.duration_seconds)

    def test_empty_challenge_malformed_and_unrelated_video_fail_closed(self):
        for body in (
            '<html>captcha</html>',
            'window._ROUTER_DATA = {"loaderData": {"video_(id)/page":null}}',
            'window._ROUTER_DATA = bad javascript;',
            'window._ROUTER_DATA = ' + json.dumps(metadata('7777777777777777777')),
        ):
            with self.subTest(body=body), self.assertRaises(DouyinAccessError):
                parse_public_video(body, VIDEO_ID)

    def test_urls_preserve_full_snowflake_as_text(self):
        self.assertEqual(video_id_from_url(URL), VIDEO_ID)
        self.assertEqual(video_id_from_url(f'https://www.iesdouyin.com/share/video/{VIDEO_ID}/'), VIDEO_ID)
        self.assertEqual(video_id_from_url(f'https://www.douyin.com/?modal_id={VIDEO_ID}'), VIDEO_ID)
        self.assertIsNone(video_id_from_url(f'https://evil.example/video/{VIDEO_ID}'))

    def test_public_path_uses_no_credentials_and_no_paid_parser(self):
        bot = cog()
        bot.cookie_file = Path('secret-cookie.txt')
        with tempfile.TemporaryDirectory() as directory:
            with patch('douyin.cog.open_public_url', return_value=Response(page().encode())) as fetch:
                with patch.object(bot, '_download_direct_video') as download:
                    result = bot._download_public_video(DouyinCandidate(URL), Path(directory), 8000)
        self.assertEqual(result.title, '公开样例')
        self.assertEqual(result.webpage_url, URL)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('/share/video/', fetch.call_args.args[0])
        self.assertEqual(fetch.call_args.args[0], f'https://www.douyin.com/share/video/{VIDEO_ID}/')
        self.assertEqual(fetch.call_args.kwargs['allowed_hosts'], DOUYIN_HOSTS)
        self.assertNotIn('secret-cookie', str(fetch.call_args))
        self.assertNotIn('cookie_jar', fetch.call_args.kwargs)
        download.assert_called_once()

    def test_cookie_share_uses_current_host_without_leaking_cookies_to_media(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            bot.cookie_file = Path(directory) / 'source-cookies.txt'
            original = (
                '# Netscape HTTP Cookie File\n'
                '.douyin.com\tTRUE\t/\tTRUE\t4102444800\tsessionid\tprivate\n'
                '.douyin.com\tTRUE\t/\tTRUE\t1\texpired\told\n'
                '.other.com\tTRUE\t/\tTRUE\t4102444800\tother\tsecret\n')
            bot.cookie_file.write_text(original, encoding='utf-8')
            with patch('douyin.cog.open_public_url', side_effect=[Response(page().encode()), Response()]) as fetch:
                result = bot._download_cookie_video(DouyinCandidate(URL), Path(directory), 8000)
            page_call, media_call = fetch.call_args_list
            self.assertEqual(page_call.args[0], f'https://www.douyin.com/share/video/{VIDEO_ID}/')
            jar = page_call.kwargs['cookie_jar']
            self.assertEqual([c.name for c in jar], ['sessionid'])
            self.assertEqual(page_call.kwargs['allowed_hosts'], DOUYIN_HOSTS)
            self.assertNotIn('cookie_jar', media_call.kwargs)
            self.assertEqual(result.file_path.stat().st_size, 4096)
            self.assertEqual(bot.cookie_file.read_text(encoding='utf-8'), original)

    def test_empty_or_expired_cookie_file_does_not_make_authenticated_request(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            bot.cookie_file = Path(directory) / 'cookies.txt'
            bot.cookie_file.write_text('# Netscape HTTP Cookie File\n'
                '.douyin.com\tTRUE\t/\tTRUE\t1\texpired\told\n', encoding='utf-8')
            with patch('douyin.cog.open_public_url') as fetch:
                with self.assertRaisesRegex(RuntimeError, '未过期'):
                    bot._download_cookie_video(DouyinCandidate(URL), Path(directory), 8000)
            fetch.assert_not_called()

    def test_authenticated_transport_requires_explicit_douyin_host_scope(self):
        for allowed in (None, {'attacker.example'}, DOUYIN_HOSTS | {'attacker.example'}):
            with self.subTest(allowed=allowed), patch('douyin.public.urllib.request.build_opener') as build:
                with self.assertRaises(DouyinPublicError):
                    open_public_url(URL, timeout=10, allowed_hosts=allowed, cookie_jar=CookieJar())
                build.assert_not_called()

    def test_authenticated_redirect_rejects_http_or_foreign_host_before_request(self):
        for target in (URL.replace('https:', 'http:'), 'https://attacker.example/video'):
            with self.subTest(target=target):
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError(URL, 302, 'redirect', {'Location': target}, None)
                with patch('douyin.public.urllib.request.build_opener', return_value=opener):
                    with patch('douyin.public.socket.getaddrinfo', return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]):
                        with self.assertRaises(DouyinPublicError):
                            open_public_url(URL, timeout=10, allowed_hosts=DOUYIN_HOSTS, cookie_jar=CookieJar())
                opener.open.assert_called_once()

    def test_redirects_share_one_deadline(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(URL, 302, 'redirect', {'Location': URL}, None)
        with patch('douyin.public.urllib.request.build_opener', return_value=opener), \
                patch('douyin.public.validate_url'), \
                patch('douyin.public.time.monotonic', side_effect=[0.0, 0.2, 2.0]):
            with self.assertRaises(TimeoutError):
                open_public_url(URL, timeout=1, allowed_hosts=DOUYIN_HOSTS)
        opener.open.assert_called_once()
        self.assertAlmostEqual(opener.open.call_args.kwargs['timeout'], 0.8)

    def test_short_url_is_resolved_before_share_page(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            with patch('douyin.cog.open_public_url', side_effect=[
                Response(url=URL), Response(page().encode()),
            ]) as fetch, patch.object(bot, '_download_direct_video'):
                bot._download_public_video(DouyinCandidate('https://v.douyin.com/abc/'), Path(directory), 8000)
        self.assertEqual(fetch.call_count, 2)

    def test_private_addresses_and_non_douyin_share_redirect_are_rejected(self):
        for url in ('http://127.0.0.1/video', 'http://169.254.169.254/latest/meta-data'):
            address = urllib.parse.urlsplit(url).hostname
            with patch('douyin.public.socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80)),
            ]), self.assertRaises(DouyinPublicError):
                validate_url(url)
        with self.assertRaises(DouyinPublicError):
            validate_url('https://unrelated.example/video', DOUYIN_HOSTS)

    def test_redirect_target_is_validated_before_following(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            URL, 302, 'redirect', {'Location': 'http://127.0.0.1/private'}, None,
        )
        with patch('douyin.public.urllib.request.build_opener', return_value=opener):
            with patch('douyin.public.validate_url', side_effect=[None, DouyinPublicError('private')]) as check:
                with self.assertRaises(DouyinPublicError):
                    open_public_url(URL, timeout=10)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(check.call_args.args[0], 'http://127.0.0.1/private')


class ResolverTests(unittest.TestCase):
    def test_external_parser_prefers_no_watermark_fields_regardless_of_order(self):
        bot = cog()
        self.assertEqual(bot._extract_video_url({
            'wmplay': 'https://v.example/wm.mp4',
            'video_url': 'https://v.example/unknown.mp4',
            'nwmplay': 'https://v.example/clean.mp4',
        }), 'https://v.example/clean.mp4')
        self.assertIsNone(bot._extract_video_url({'wmplay': {'url': 'https://v.example/wm.mp4'}}))
        self.assertIsNone(bot._extract_video_url({'video_url': 'https://v.example/a.mp4?watermark=1'}))

    def test_direct_download_normalizes_official_playwm_and_never_falls_back(self):
        bot = cog()
        original = 'https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc&logo_name=aweme'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            with patch('douyin.cog.open_public_url', side_effect=DouyinAccessError('403')) as fetch:
                with self.assertRaises(DouyinAccessError):
                    bot._download_direct_video(original, path, 8000)
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0], 'https://aweme.snssdk.com/aweme/v1/play/?video_id=abc')
            self.assertFalse(path.exists())

    def test_download_redirect_to_watermarked_stream_is_rejected(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            with patch('douyin.cog.open_public_url', return_value=Response(url='https://v.example/video.mp4?watermark=1')):
                with self.assertRaises(DouyinWatermarkError):
                    bot._download_direct_video('https://v.example/video.mp4', path, 8000)
            self.assertFalse(path.exists())

    def test_yt_dlp_selector_excludes_marked_formats_including_fallback(self):
        from yt_dlp import YoutubeDL
        def fmt(name, **extra):
            return {'format_id': name, 'url': 'https://v.example/video.mp4', 'ext': 'mp4',
                    'width': 1280, 'height': 720, 'vcodec': 'h264', 'acodec': 'aac', **extra}
        with YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            select = ydl.build_format_selector(cog()._format_selector(1080))
            marked = [fmt('wm', format_note='Download video, watermarked'),
                      fmt('share', url='https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc')]
            self.assertEqual(list(select({'formats': marked, 'has_merged_format': True,
                                          'incomplete_formats': False})), [])
            selected = list(select({'formats': [fmt('clean'), *marked], 'has_merged_format': True,
                                    'incomplete_formats': False}))
            self.assertEqual([f['format_id'] for f in selected], ['clean'])

    def test_cookie_share_success_skips_legacy_api_downloaders(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            bot.cookie_file = Path(directory) / 'cookies.txt'
            bot.cookie_file.write_text('configured', encoding='utf-8')
            video_path = Path(directory) / 'v.mp4'
            video_path.write_bytes(b'v' * 2048)
            result = DownloadedDouyinVideo('video', URL, video_path, 3)
            with patch.object(bot, '_clear_work_dir'), \
                    patch.object(bot, '_download_public_video', side_effect=DouyinAccessError('empty')), \
                    patch.object(bot, '_download_cookie_video', return_value=result) as share, \
                    patch.object(bot, '_download_video_at_height') as yt, \
                    patch.object(bot, '_download_video_with_f2') as f2:
                self.assertEqual(bot._download_video(DouyinCandidate(URL), Path(directory), 8000), result)
            share.assert_called_once()
            yt.assert_not_called()
            f2.assert_not_called()

    def test_generic_yt_dlp_cookie_error_does_not_claim_expiry(self):
        message = cog()._format_error(RuntimeError('Fresh cookies (not necessarily logged in) are needed'))
        self.assertIn('不能证明 Cookie 已过期', message)

    def test_cookie_use_preserves_source_and_filters_expired_unrelated_domains(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot.cookie_file = root / 'original.txt'
            original = (
                '# Netscape HTTP Cookie File\n'
                '.douyin.com\tTRUE\t/\tTRUE\t4102444800\tttwid\tgood\n'
                '.douyin.com\tTRUE\t/\tTRUE\t1\told\texpired\n'
                '.other.com\tTRUE\t/\tTRUE\t4102444800\tsecret\tprivate\n')
            bot.cookie_file.write_text(original, encoding='utf-8')
            self.assertEqual(bot._read_cookie_header(), 'ttwid=good')
            work = root / 'work'
            work.mkdir()
            (work / 'video.mp4').write_bytes(b'v' * 2048)
            manager = Mock()
            ydl = Mock()
            ydl.extract_info.return_value = {'title': 'video'}
            manager.__enter__ = Mock(return_value=ydl)
            manager.__exit__ = Mock(return_value=False)
            with patch('douyin.cog.yt_dlp.YoutubeDL', return_value=manager) as factory:
                bot._download_video_at_height(DouyinCandidate(URL), work, 720)
            cookie_copy = Path(factory.call_args.args[0]['cookiefile'])
            self.assertEqual(cookie_copy.parent, work)
            copied = cookie_copy.read_text()
            self.assertIn('good', copied)
            self.assertNotIn('expired', copied)
            self.assertNotIn('private', copied)
            self.assertEqual(bot.cookie_file.read_text(encoding='utf-8'), original)

    def test_f2_exit_zero_http_403_is_still_access_failure(self):
        bot = cog()
        def run(*_args, **kwargs):
            kwargs['stdout'].write(b'HTTP/1.1\x1b[0m " \x1b[31m403 Forbidden\nCookie: sessionid=private-secret')
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_read_cookie_header', return_value='sessionid=private-secret'), \
                    patch('douyin.cog.subprocess.run', side_effect=run) as process:
                with self.assertRaises(DouyinAccessError) as error:
                    bot._download_video_with_f2(DouyinCandidate(URL), Path(directory))
            self.assertIn('403', str(error.exception))
            self.assertNotIn('private-secret', str(error.exception))
            self.assertEqual(process.call_args.kwargs['cwd'], directory)

    def test_later_generic_failure_does_not_hide_access_failure(self):
        bot = cog()
        bot.use_f2 = bot.f2_available = True
        bot.cookie_file = Path('configured')
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_download_public_video', side_effect=DouyinAccessError('公开页拒绝')), \
                    patch.object(bot, '_download_video_at_height', side_effect=RuntimeError('Fresh cookies are needed')), \
                    patch.object(bot, '_download_video_with_f2', side_effect=RuntimeError('no video')):
                with self.assertRaises(DouyinAccessError) as error:
                    bot._download_video(DouyinCandidate(URL), Path(directory), 8000)
            self.assertIn('Cookie', str(error.exception))
            self.assertIn('凭证管理', str(error.exception))

    def test_successful_public_route_skips_all_fallbacks(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'v.mp4'
            path.write_bytes(b'v' * 2048)
            result = DownloadedDouyinVideo('test', URL, path, 3)
            with patch.object(bot, '_clear_work_dir'), patch.object(bot, '_download_public_video', return_value=result):
                with patch.object(bot, '_download_video_at_height') as yt:
                    self.assertEqual(bot._download_video(DouyinCandidate(URL), Path(directory), 8000), result)
        yt.assert_not_called()

    def test_cookie_failure_does_not_retry_per_height_or_invoke_unconfigured_api(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_download_public_video', side_effect=DouyinAccessError('empty')):
                with patch.object(bot, '_download_video_at_height', side_effect=RuntimeError('Fresh cookies are needed')) as yt:
                    with patch.object(bot, '_download_video_with_parse_api') as api:
                        with self.assertRaises(DouyinAccessError):
                            bot._download_video(DouyinCandidate(URL), Path(directory), 8000)
        yt.assert_called_once()
        api.assert_not_called()

    def test_oversized_external_video_gets_duration_before_compression(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'api.mp4'
            path.write_bytes(b'v' * 2048)
            original = DownloadedDouyinVideo('title', URL, path)
            with patch.object(bot, '_probe_duration_seconds', return_value=3.5):
                with patch.object(bot, '_compress_video_if_needed', return_value=None) as compress:
                    bot._ensure_uploadable(original, Path(directory), 1000)
            self.assertEqual(compress.call_args.args[0].duration_seconds, 3.5)

    def test_download_accepts_video_and_refuses_html_images_partial_and_oversize(self):
        for body, content_type, length, limit, good in (
            (b'v' * 4096, 'video/mp4', 4096, 5000, True),
            (b'<html>' * 1000, 'text/html', None, 10000, False),
            (b'image' * 1000, 'image/jpeg', None, 10000, False),
            (b'<html>' * 1000, 'application/octet-stream', None, 10000, False),
            (b'v' * 4096, 'video/mp4', 8000, 10000, False),
            (b'v' * 4096, 'video/mp4', None, 2000, False),
            (b'v' * 4096, 'video/mp4', 4096, 2000, False),
        ):
            with self.subTest(content_type=content_type, length=length, limit=limit):
                bot = cog()
                bot.download_max_bytes = limit
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'v.mp4'
                    with patch('douyin.cog.open_public_url', return_value=Response(body, content_type, length)):
                        if good:
                            bot._download_direct_video('https://v.example/v.mp4', path, 1000)
                            self.assertEqual(path.stat().st_size, 4096)
                        else:
                            with self.assertRaises(RuntimeError):
                                bot._download_direct_video('https://v.example/v.mp4', path, 1000)
                            self.assertFalse(path.exists())

    def test_no_cover_url_mistaken_for_video(self):
        bot = cog()
        self.assertIsNone(bot._extract_video_url({'cover': 'https://p.byteimg.com/test.jpeg'}))
        self.assertEqual(bot._extract_video_url({
            'cover': 'https://p.byteimg.com/test.jpeg',
            'video_url': 'https://v.douyinvod.com/video.mp4',
        }), 'https://v.douyinvod.com/video.mp4')

    def test_error_redacts_urls_cookies_and_mentions(self):
        error = cog()._format_error(RuntimeError(
            'bad https://api.example/?token=secret sessionid=private; msToken=hidden @everyone'
        ))
        for secret in ('secret', 'private', 'hidden', '@everyone'):
            self.assertNotIn(secret, error)

    def test_deadline_stops_before_another_route(self):
        bot = cog()
        bot._download_deadline = time.monotonic() - 1
        with self.assertRaises(TimeoutError):
            bot._remaining_timeout()

    def test_yt_dlp_retries_disabled_and_single_quality(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'v.mp4'
            path.write_bytes(b'v' * 2048)
            ydl = Mock()
            ydl.extract_info.return_value = {'title': 'test', 'duration': 3}
            manager = Mock()
            manager.__enter__ = Mock(return_value=ydl)
            manager.__exit__ = Mock(return_value=False)
            with patch('douyin.cog.yt_dlp.YoutubeDL', return_value=manager) as factory:
                bot._download_video_at_height(DouyinCandidate(URL), Path(directory), 720)
            opts = factory.call_args.args[0]
            self.assertEqual(opts['retries'], 0)
            self.assertEqual(opts['fragment_retries'], 0)
            self.assertEqual(opts['extractor_retries'], 0)
            self.assertNotIn('cookiefile', opts)
            ydl.extract_info.assert_called_once()

    def test_f2_cookies_are_not_exposed_in_process_argv_or_errors(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_read_cookie_header', return_value='sessionid=private-secret'):
                with patch('douyin.cog.subprocess.run', return_value=SimpleNamespace(returncode=1)) as run:
                    with self.assertRaisesRegex(RuntimeError, 'F2'):
                        bot._download_video_with_f2(DouyinCandidate(URL), Path(directory))
            self.assertNotIn('private-secret', str(run.call_args.args))
            self.assertIn('sessionid=private-secret', json.loads(run.call_args.kwargs['input']))

    def test_configured_api_tries_at_most_three_endpoints(self):
        bot = cog()
        bot.parse_api_urls = ['https://parser.example/' + str(n) for n in range(10)]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_request_parse_api', side_effect=RuntimeError('unavailable')) as read:
                with self.assertRaises(RuntimeError):
                    bot._download_video_with_parse_api(DouyinCandidate(URL), Path(directory), 8000)
        self.assertEqual(read.call_count, 3)


class AsyncResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_blocks_repeated_commands(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_temp_root', return_value=Path(directory)):
                with patch.object(bot, '_download_video', side_effect=DouyinAccessError('403')) as download:
                    for _ in range(2):
                        with self.assertRaises(DouyinAccessError):
                            await bot._resolve_and_send_video(
                                guild=None, candidate=DouyinCandidate(URL),
                                update_status=AsyncMock(), send_video=AsyncMock(),
                            )
        self.assertEqual(download.call_count, 1)

    async def test_timeout_keeps_lock_and_files_until_worker_exits(self):
        bot = cog()
        bot.download_timeout_seconds = 0.05
        entered = threading.Event()
        finish = threading.Event()
        paths = []

        def work(_candidate, directory, _limit):
            paths.append(directory)
            (directory / 'pending').write_text('still working')
            entered.set()
            finish.wait(5)
            raise TimeoutError('done')

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(bot, '_temp_root', return_value=Path(directory)):
                with patch.object(bot, '_download_video', side_effect=work):
                    try:
                        with self.assertRaises(TimeoutError):
                            await bot._resolve_and_send_video(
                                guild=None, candidate=DouyinCandidate(URL),
                                update_status=AsyncMock(), send_video=AsyncMock(),
                            )
                        self.assertTrue(entered.is_set())
                        self.assertTrue(paths[0].exists())
                        self.assertTrue(bot.download_lock.locked())
                        with self.assertRaisesRegex(RuntimeError, '清理'):
                            await bot._resolve_and_send_video(
                                guild=None, candidate=DouyinCandidate(URL),
                                update_status=AsyncMock(), send_video=AsyncMock(),
                            )
                    finally:
                        finish.set()
                        await asyncio.gather(*bot._cleanup_tasks)
        self.assertFalse(bot.download_lock.locked())
        self.assertFalse(paths[0].exists())

    async def test_card_is_video_container_with_source_and_no_mentions(self):
        bot = cog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'v' * 2048)
            video = DownloadedDouyinVideo('**标题** @everyone', URL, path, 31.5)
            view = bot._build_video_card(video, 'douyin-video.mp4')
            container = view.to_components()[0]
            self.assertEqual(container['type'], 17)
            self.assertEqual(container['accent_color'], 0x25F4EE)
            children = container['components']
            media = next(item for item in children if item['type'] == 12)
            self.assertEqual(media['items'][0]['media']['url'], 'attachment://douyin-video.mp4')
            texts = '\n'.join(item['content'] for item in children if item['type'] == 10)
            self.assertIn('00:32', texts)
            self.assertNotIn('@everyone', texts)
            button = next(item for item in children if item['type'] == 1)['components'][0]
            self.assertEqual(button['url'], URL)
            with handle_message_parameters(view=view, file=discord.File(path, filename='douyin-video.mp4'),
                                           allowed_mentions=discord.AllowedMentions.none()) as params:
                payload = json.loads(next(item['value'] for item in params.multipart if item['name'] == 'payload_json'))
                self.assertTrue(payload['flags'] & 32768)
                self.assertNotIn('content', payload)
                self.assertNotIn('embeds', payload)
            view.stop()

    async def test_slash_and_prefix_send_cards_without_yt_dlp_gate(self):
        for slash in (True, False):
            with self.subTest(slash=slash), tempfile.TemporaryDirectory() as directory:
                bot = cog()
                path = Path(directory) / 'video.mp4'
                path.write_bytes(b'v' * 2048)
                video = DownloadedDouyinVideo('title', URL, path, 3)
                send = AsyncMock()

                async def resolve(**kwargs):
                    await kwargs['send_video'](video, 'douyin-video.mp4')

                with patch.object(bot, '_resolve_and_send_video', side_effect=resolve):
                    with patch('douyin.cog.yt_dlp', None):
                        if slash:
                            interaction = SimpleNamespace(
                                channel=SimpleNamespace(send=AsyncMock()), guild=None,
                                response=SimpleNamespace(defer=AsyncMock()),
                                edit_original_response=AsyncMock(), followup=SimpleNamespace(send=send),
                            )
                            await DouyinVideoCog.send_douyin_video.callback(bot, interaction, URL)
                        else:
                            ctx = SimpleNamespace(
                                channel=SimpleNamespace(send=send), guild=None,
                                author=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
                            )
                            await DouyinVideoCog.send_douyin_video_prefix.callback(bot, ctx, url=URL)
                send.assert_awaited_once()
                self.assertIsInstance(send.call_args.kwargs['view'], discord.ui.LayoutView)
                self.assertNotIn('content', send.call_args.kwargs)
                send.call_args.kwargs['view'].stop()


if __name__ == '__main__':
    unittest.main()
