"""Bounded access to Douyin share pages and their public media URLs.

Credentials are optional and limited to HTTPS Douyin pages, never their media
CDNs. No generated signatures or challenge solver. Empty/challenge pages are
failures, never successful video metadata.
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar


MOBILE_UA = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 '
    'Mobile/15E148 Safari/604.1'
)
DOUYIN_HOSTS = {'douyin.com', 'www.douyin.com', 'v.douyin.com',
                'm.douyin.com', 'iesdouyin.com', 'www.iesdouyin.com'}
VIDEO_ID = re.compile(r'/(?:share/)?video/(\d{15,22})(?:/|$)')


class DouyinPublicError(RuntimeError):
    pass


class DouyinAccessError(DouyinPublicError):
    """Public access was refused or did not expose playable media."""


class DouyinWatermarkError(DouyinPublicError):
    """Only a known watermarked stream is available; do not send it."""


def is_watermarked_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if re.search(r'/(?:playwm|watermark)(?:/|$)', parsed.path, re.I):
        return True
    return any(key.lower() in {'watermark', 'is_watermark', 'watermarked'}
               and value.lower() not in {'0', 'false', 'no', ''}
               for key, value in urllib.parse.parse_qsl(parsed.query))


def unwatermarked_url(url: str) -> str:
    """Select Douyin's ordinary playback endpoint, not its share/download overlay.

    Only the exact official playback endpoint is rewritten. Never blindly
    replace parts of third-party or signed CDN URLs, or fall back to playwm.
    """
    parsed = urllib.parse.urlsplit(url)
    if (parsed.hostname == 'aweme.snssdk.com'
            and parsed.path.rstrip('/') in {'/aweme/v1/playwm', '/aweme/v1/play'}
            and not parsed.username and not parsed.password and parsed.port in {None, 80, 443}):
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                 if key.lower() not in {'logo_name', 'watermark', 'is_watermark', 'watermarked'}]
        if any(key == 'video_id' and value for key, value in query):
            url = parsed._replace(path='/aweme/v1/play/', query=urllib.parse.urlencode(query)).geturl()
    if is_watermarked_url(url):
        raise DouyinWatermarkError('原站只提供了带水印的播放地址，未发送水印版。')
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url: str, allowed_hosts: set[str] | None = None) -> None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme not in {'http', 'https'} or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 80, 443}):
        raise DouyinPublicError('解析结果包含不安全的下载地址。')
    if allowed_hosts is not None and host not in allowed_hosts:
        raise DouyinPublicError('分享链接跳转到了非抖音网站。')
    addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                   type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise DouyinPublicError('拒绝访问本机或内网下载地址。')


def open_public_url(url: str, *, timeout: float, allowed_hosts: set[str] | None = None,
                    cookie_jar: CookieJar | None = None):
    # Validate every redirect, including links returned by optional external parsers.
    if cookie_jar is not None and (not allowed_hosts or not allowed_hosts <= DOUYIN_HOSTS):
        raise DouyinPublicError('Cookie 只允许用于抖音 HTTPS 分享页面。')
    handlers = [_NoRedirect()]
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener = urllib.request.build_opener(*handlers)
    deadline = time.monotonic() + timeout
    for _ in range(5):
        if cookie_jar is not None and urllib.parse.urlsplit(url).scheme != 'https':
            raise DouyinPublicError('拒绝把抖音 Cookie 发送到非 HTTPS 页面。')
        validate_url(url, allowed_hosts)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('抖音链接跳转超时。')
        request = urllib.request.Request(url, headers={
            'User-Agent': MOBILE_UA, 'Referer': 'https://www.douyin.com/',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })
        try:
            return opener.open(request, timeout=remaining)
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get('Location')
                exc.close()
                if not location:
                    raise DouyinPublicError('分享链接跳转地址为空。') from None
                url = urllib.parse.urljoin(url, location)
                continue
            code = exc.code
            exc.close()
            if code in {401, 403, 412, 429}:
                raise DouyinAccessError(f'抖音公开访问被拒绝（HTTP {code}）。') from None
            raise DouyinPublicError(f'抖音资源请求失败（HTTP {code}）。') from None
    raise DouyinPublicError('分享链接跳转次数过多。')


@dataclass(frozen=True)
class PublicVideo:
    video_id: str
    title: str
    video_url: str
    duration_seconds: float | None


def video_id_from_url(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname not in DOUYIN_HOSTS:
        return None
    match = VIDEO_ID.search(parsed.path)
    if match:
        return match.group(1)
    value = urllib.parse.parse_qs(parsed.query).get('modal_id', [''])[0]
    return value if re.fullmatch(r'\d{15,22}', value) else None


def parse_public_video(page: str, expected_id: str) -> PublicVideo:
    payloads = []
    for match in re.finditer(r'(?:window\.)?_ROUTER_DATA\s*=\s*', page):
        try:
            payloads.append(json.JSONDecoder().raw_decode(page[match.end():].lstrip())[0])
        except ValueError:
            continue
    for match in re.finditer(
        r'<script\b[^>]*\bid=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', page, re.S | re.I,
    ):
        try:
            payloads.append(json.loads(urllib.parse.unquote(html.unescape(match.group(1)))))
        except ValueError:
            continue

    def entries(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from entries(child)
        elif isinstance(value, list):
            for child in value:
                yield from entries(child)

    watermark_only = False
    for payload in payloads:
        for item in entries(payload):
            # Never accidentally download a recommended, unrelated video.
            if str(item.get('aweme_id', item.get('awemeId', ''))) != expected_id:
                continue
            video = item.get('video')
            if not isinstance(video, dict):
                continue
            addresses = [video.get(key) for key in ('play_addr_h264', 'playAddrH264', 'play_addr', 'playAddr')]
            rates = video.get('bit_rate') or video.get('bitRate') or []
            if isinstance(rates, list):
                addresses.extend(rate.get('play_addr') or rate.get('playAddr')
                                 for rate in rates[:30] if isinstance(rate, dict))
            urls = []
            for address in addresses:
                if isinstance(address, dict):
                    values = address.get('url_list') or address.get('urlList') or []
                    if isinstance(values, list):
                        urls.extend(url for url in values if isinstance(url, str)
                                    and url.startswith(('https://', 'http://')))
            # Prefer supplied ordinary streams over conversion of a known
            # official playwm endpoint. download_addr is deliberately excluded.
            urls.sort(key=is_watermarked_url)
            for url in urls:
                try:
                    url = unwatermarked_url(url)
                except DouyinWatermarkError:
                    watermark_only = True
                    continue
                duration = video.get('duration')
                duration = float(duration) / 1000 if isinstance(duration, (int, float)) and duration > 0 else None
                return PublicVideo(expected_id, str(item.get('desc') or '抖音视频'), url, duration)
    if watermark_only:
        raise DouyinWatermarkError('没有找到该作品可用的无水印播放流，未回退到水印版。')
    raise DouyinAccessError(
        '公开分享页没有提供该视频的可播放数据，可能受风控、登录限制或作品状态影响。'
    )
