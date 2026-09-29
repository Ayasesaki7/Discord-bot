"""Bounded downloads and one-time Opus preparation for Discord voice."""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver


FALLBACK_SEARCH_PREFIXES = {'bilibili': 'bilisearch', 'youtube': 'ytsearch'}


def safe_error(error: object) -> str:
    text = re.sub(r'https?://[^\s\'"<>]+', '[URL hidden]', str(error))
    text = re.sub(r'(?i)([\w]*(?:token|key|cookie|sessionid)[\w]*)=[^;\s]+', r'\1=[hidden]', text)
    return ' '.join(text.split())[:400]


def public_http_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 80, 443}):
        raise ValueError('音频链接必须是公开的 HTTP/HTTPS 地址。')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname.lower() == 'localhost' or parsed.hostname.lower().endswith('.localhost'):
            raise ValueError('不能下载本机或内网音频地址。') from None
    else:
        if not address.is_global:
            raise ValueError('不能下载本机或内网音频地址。')
    return url


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        results = await super().resolve(host, port, family)
        if not results or any(not ipaddress.ip_address(item['host']).is_global for item in results):
            raise OSError('拒绝连接内网音频地址。')
        return results


class AudioPipeline:
    @staticmethod
    def _media_signature(header: bytes) -> bool:
        # CDN Content-Type can be wrong (QQ serves MP3 as form-urlencoded).
        # This only permits downloading to quarantine; full FFmpeg decoding
        # still has to succeed before the file becomes a playable cache entry.
        return (
            header.startswith((b'ID3', b'fLaC', b'OggS', b'\x1a\x45\xdf\xa3'))
            or (header.startswith(b'RIFF') and header[8:12] == b'WAVE')
            or header[4:8] == b'ftyp'
            or (len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0)
        )

    def __init__(self, executable: str | None, *, max_bytes: int, bitrate: int = 128):
        self.executable = executable
        self.max_bytes = max_bytes
        self.bitrate = bitrate
        self.session: aiohttp.ClientSession | None = None

    async def close(self):
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60, connect=15, sock_read=20),
                connector=aiohttp.TCPConnector(limit=8, resolver=PublicResolver()),
                cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
            )
        return self.session

    async def download(self, url: str, path: Path, *, cookie: str = '') -> int:
        session = await self._session()
        temp = path.with_name(path.name + '.part')
        try:
            # A total deadline also covers all redirects, not 60 seconds per hop.
            async with asyncio.timeout(60):
                for _ in range(5):
                    public_http_url(url)
                    host = (urlsplit(url).hostname or '').lower()
                    headers = {
                        'User-Agent': 'Mozilla/5.0',
                        'Accept': 'audio/*,application/octet-stream;q=0.9,*/*;q=0.5',
                        'Referer': 'https://y.qq.com/',
                    }
                    # Never forward the QQ credential to direct URLs or foreign redirects.
                    if cookie and (host == 'qq.com' or host.endswith('.qq.com')):
                        headers['Cookie'] = cookie
                    async with session.get(url, headers=headers, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get('Location')
                            if not location:
                                raise RuntimeError('音频地址跳转为空。')
                            url = urljoin(url, location)
                            continue
                        if response.status != 200:
                            return response.status
                        mime = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                        mime_ok = not mime or mime.startswith('audio/') or mime in {
                            'application/octet-stream', 'binary/octet-stream',
                            'application/ogg', 'video/mp4', 'video/webm',
                        }
                        length = response.content_length
                        if length is not None and length > self.max_bytes:
                            raise RuntimeError('音频超过下载大小限制。')
                        total = 0
                        prefix = bytearray()
                        with temp.open('wb') as target:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                total += len(chunk)
                                if total > self.max_bytes:
                                    raise RuntimeError('音频超过下载大小限制。')
                                if not mime_ok:
                                    prefix.extend(chunk[:max(0, 64 - len(prefix))])
                                    if len(prefix) >= 16:
                                        if not self._media_signature(bytes(prefix)):
                                            raise RuntimeError(f'音源不是可识别的音频，可能是错误页（类型 {mime[:80]}）。')
                                        mime_ok = True
                                target.write(chunk)
                        if not mime_ok:
                            raise RuntimeError('音源未包含可识别的音频文件头。')
                        if total <= 1024 or (length is not None and total != length):
                            raise RuntimeError('音频下载为空或不完整。')
                        os.replace(temp, path)
                        return 200
                raise RuntimeError('音频链接跳转次数过多。')
        finally:
            temp.unlink(missing_ok=True)

    async def _run(self, command: list[str], *, timeout: float, ok_codes: tuple[int, ...] = (0,)) -> None:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        tail = bytearray()

        async def drain():
            while chunk := await process.stderr.read(4096):
                tail.extend(chunk)
                del tail[:-4096]

        reader = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(process.wait(), timeout)
            await reader
            if process.returncode not in ok_codes:
                raise RuntimeError(safe_error(tail.decode('utf-8', 'replace')) or '音频处理失败。')
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def prepare(self, source: Path, destination: Path) -> None:
        if not self.executable:
            raise RuntimeError('缺少 FFmpeg，不能检查和准备音频。')
        try:
            await self._run([
                self.executable, '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-xerror', '-y', '-i', str(source), '-map', '0:a:0', '-vn',
                '-c:a', 'libopus', '-b:a', f'{self.bitrate}k',
                '-ar', '48000', '-ac', '2', '-threads', '1',
                '-f', 'opus', str(destination),
            ], timeout=120)
            if not self.valid_cache(destination):
                raise RuntimeError('音频转换未产生有效的 Opus 文件。')
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    async def fallback(self, query: str, directory: Path, *, source: str = 'youtube',
                       duration: float | None = None, cookie_file: Path | None = None) -> Path | None:
        stem = f'fallback-{source}'
        command = [
            sys.executable, '-m', 'yt_dlp', '--quiet', '--no-warnings',
            '--no-progress', '--no-playlist', '--no-cache-dir',
            '--retries', '0', '--fragment-retries', '0', '--extractor-retries', '0',
            '--socket-timeout', '15', '--max-filesize', str(self.max_bytes),
            '-f', 'bestaudio/best', '-o', str(directory / f'{stem}.%(ext)s'),
        ]
        results = 1
        if duration:
            # Search hits are often covers, live cuts or hour-long loops. Only
            # accept the first of a few candidates whose length fits the song.
            slack = max(15.0, duration * 0.1)
            command += ['--match-filters', f'duration>={duration - slack:.0f} & duration<={duration + slack:.0f}',
                        '--max-downloads', '1']
            results = 3
        if cookie_file is not None:
            command += ['--cookies', str(cookie_file)]
        command += ['--', f'{FALLBACK_SEARCH_PREFIXES[source]}{results}:{query}']
        # yt-dlp exits with 101 when --max-downloads stops it after a match.
        await self._run(command, timeout=90, ok_codes=(0, 101))
        paths = [p for p in directory.glob(f'{stem}.*')
                 if p.suffix not in {'.part', '.ytdl'} and p.is_file()
                 and 1024 < p.stat().st_size <= self.max_bytes]
        return max(paths, key=lambda p: p.stat().st_size) if paths else None

    @staticmethod
    def valid_cache(path: Path) -> bool:
        try:
            if path.stat().st_size <= 64:
                return False
            with path.open('rb') as stream:
                header = stream.read(256)
            return header.startswith(b'OggS') and b'OpusHead' in header
        except OSError:
            return False
