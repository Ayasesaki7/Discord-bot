from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


DOUYIN_URL_PATTERN = re.compile(
    r'https?://(?:(?:www|v|m|www\.ies)\.)?douyin\.com/[^\s<>()]+'
    r'|https?://(?:www\.)?iesdouyin\.com/[^\s<>()]+',
    re.IGNORECASE,
)

DEFAULT_DISCORD_UPLOAD_LIMIT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_HEIGHT = 1080
DEFAULT_COMPRESS_WIDTH = 1080
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 240
DEFAULT_DOWNLOAD_MAX_BYTES = 192 * 1024 * 1024
DEFAULT_PARSE_API_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class DouyinCandidate:
    source_url: str


@dataclass(frozen=True)
class DownloadedDouyinVideo:
    title: str
    webpage_url: str
    file_path: Path
    duration_seconds: float | None = None


StatusUpdater = Callable[[str], Awaitable[None]]
VideoSender = Callable[[str, Path, str], Awaitable[None]]


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, '').strip().lower()
    if not raw:
        return default
    return raw in {'1', 'true', 'yes', 'y', 'on'}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None:
        return max(value, minimum)
    return value


def _format_size(byte_count: int) -> str:
    if byte_count >= 1024 * 1024:
        return f'{byte_count / 1024 / 1024:.1f} MB'
    if byte_count >= 1024:
        return f'{byte_count / 1024:.1f} KB'
    return f'{byte_count} B'


def _safe_filename(text: str, fallback: str = 'douyin-video') -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', ' ', text).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return (cleaned or fallback)[:80]


class DouyinVideoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.max_height = _env_int('DOUYIN_MAX_HEIGHT', DEFAULT_MAX_HEIGHT, minimum=144)
        self.compress_width = _env_int(
            'DOUYIN_COMPRESS_WIDTH',
            DEFAULT_COMPRESS_WIDTH,
            minimum=360,
        )
        self.download_timeout_seconds = _env_int(
            'DOUYIN_DOWNLOAD_TIMEOUT_SECONDS',
            DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
            minimum=30,
        )
        self.max_upload_bytes_override = self._read_max_upload_bytes_override()
        self.download_max_bytes = self._read_download_max_bytes()
        self.compress_if_needed = _env_flag('DOUYIN_COMPRESS_IF_NEEDED', True)
        self.parse_api_urls = self._read_parse_api_urls()
        self.use_f2 = _env_flag('DOUYIN_USE_F2', True)
        self.parse_api_timeout_seconds = _env_int(
            'DOUYIN_PARSE_API_TIMEOUT_SECONDS',
            DEFAULT_PARSE_API_TIMEOUT_SECONDS,
            minimum=5,
        )
        self.cookie_file = self._read_cookie_file()
        self.cookies_from_browser = self._read_cookies_from_browser()
        self.ffmpeg_executable = self._resolve_ffmpeg_executable()
        self.f2_available = importlib.util.find_spec('f2') is not None
        self.download_lock = asyncio.Lock()

        if yt_dlp is None:
            print('[WARN] Douyin resolver disabled: yt-dlp is not installed')
        if self.use_f2 and not self.f2_available:
            print('[WARN] Douyin f2 fallback disabled: f2 is not installed')
        if self.ffmpeg_executable is None:
            print('[WARN] Douyin resolver could not find ffmpeg; compressed videos may fail')

    @app_commands.command(
        name='dy',
        description='解析抖音链接，并把视频文件发到当前频道',
    )
    @app_commands.describe(url='抖音视频链接，例如 v.douyin.com 或 douyin.com/video')
    async def send_douyin_video(
        self,
        interaction: discord.Interaction,
        url: str,
    ) -> None:
        if yt_dlp is None:
            await interaction.response.send_message(
                '抖音视频解析功能缺少 yt-dlp 依赖，暂时不能使用。',
                ephemeral=True,
            )
            return

        candidate = self._find_candidate(url)
        if candidate is None:
            await interaction.response.send_message(
                '没有识别到抖音视频链接。',
                ephemeral=True,
            )
            return

        if interaction.channel is None:
            await interaction.response.send_message(
                '当前上下文里没有可以发送视频的频道。',
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.edit_original_response(content='正在解析抖音视频...')

        async def update_status(content: str) -> None:
            await interaction.edit_original_response(content=content)

        async def send_video(content: str, file_path: Path, filename: str) -> None:
            file = discord.File(file_path, filename=filename)
            try:
                await interaction.followup.send(
                    content=content,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=False,
                )
            finally:
                file.close()

        try:
            await self._resolve_and_send_video(
                guild=interaction.guild,
                candidate=candidate,
                update_status=update_status,
                send_video=send_video,
            )
        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content='解析抖音视频超时了，稍后可以再试一次。',
            )
        except Exception as exc:
            print(
                '[WARN] Failed to resolve Douyin video: '
                f'user_id={interaction.user.id}, channel_id={interaction.channel_id}, error={exc}'
            )
            await interaction.edit_original_response(
                content=f'解析抖音视频失败：{self._format_error(exc)}'
            )

    @commands.command(
        name='dy',
        aliases=['douyin', '抖音', '抖音视频'],
    )
    async def send_douyin_video_prefix(
        self,
        ctx: commands.Context,
        *,
        url: str,
    ) -> None:
        if yt_dlp is None:
            await ctx.reply(
                '抖音视频解析功能缺少 yt-dlp 依赖，暂时不能使用。',
                mention_author=False,
            )
            return

        candidate = self._find_candidate(url)
        if candidate is None:
            await ctx.reply('没有识别到抖音视频链接。', mention_author=False)
            return

        if not hasattr(ctx.channel, 'send'):
            await ctx.reply('当前上下文里没有可以发送视频的频道。', mention_author=False)
            return

        try:
            status_message = await ctx.author.send('正在解析抖音视频...')
        except discord.HTTPException:
            status_message = None

        async def update_status(content: str) -> None:
            if status_message is None:
                return
            await status_message.edit(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        async def send_video(content: str, file_path: Path, filename: str) -> None:
            file = discord.File(file_path, filename=filename)
            try:
                await ctx.channel.send(
                    content=content,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                file.close()

        try:
            await self._resolve_and_send_video(
                guild=ctx.guild,
                candidate=candidate,
                update_status=update_status,
                send_video=send_video,
            )
        except asyncio.TimeoutError:
            await update_status('解析抖音视频超时了，稍后可以再试一次。')
        except Exception as exc:
            print(
                '[WARN] Failed to resolve Douyin video: '
                f'user_id={ctx.author.id}, channel_id={ctx.channel.id}, error={exc}'
            )
            await update_status(f'解析抖音视频失败：{self._format_error(exc)}')

    @send_douyin_video_prefix.error
    async def send_douyin_video_prefix_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                '用法：`!dy https://v.douyin.com/.../`',
                mention_author=False,
            )
            return
        raise error

    async def _resolve_and_send_video(
        self,
        *,
        guild: discord.Guild | None,
        candidate: DouyinCandidate,
        update_status: StatusUpdater,
        send_video: VideoSender,
    ) -> None:
        upload_limit = self._upload_limit_for_guild(guild)
        async with self.download_lock:
            work_dir = Path(tempfile.mkdtemp(prefix='douyin_', dir=str(self._temp_root())))
            try:
                video = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._download_video,
                        candidate,
                        work_dir,
                        upload_limit,
                    ),
                    timeout=self.download_timeout_seconds,
                )
                file_size = video.file_path.stat().st_size
                if file_size > upload_limit:
                    await update_status(
                        f'视频太大啦：{_format_size(file_size)}，'
                        f'当前频道最多只能上传 {_format_size(upload_limit)}。\n'
                        f'原链接：{video.webpage_url}'
                    )
                    return

                await update_status(
                    f'解析完成，正在上传：{video.title} ({_format_size(file_size)})'
                )
                await send_video(
                    f'抖音视频：{video.title}\n{video.webpage_url}',
                    video.file_path,
                    f'{_safe_filename(video.title)}.mp4',
                )
                await update_status(f'已发送：{video.title}')

            finally:
                await asyncio.to_thread(self._remove_tree_with_retries, work_dir)

    def _find_candidate(self, text: str) -> DouyinCandidate | None:
        match = DOUYIN_URL_PATTERN.search(text)
        if match is None:
            return None
        return DouyinCandidate(source_url=self._normalize_douyin_url(match.group(0)))

    def _normalize_douyin_url(self, raw_url: str) -> str:
        parsed = urllib.parse.urlparse(raw_url)
        host = parsed.netloc.lower()
        share_match = re.search(r'/share/video/(?P<video_id>\d+)', parsed.path)
        if host.endswith('iesdouyin.com') and share_match is not None:
            return f'https://www.douyin.com/video/{share_match.group("video_id")}'
        return raw_url

    def _download_video(
        self,
        candidate: DouyinCandidate,
        work_dir: Path,
        upload_limit: int,
    ) -> DownloadedDouyinVideo:
        if self.parse_api_urls:
            try:
                video = self._download_video_with_parse_api(candidate, work_dir, upload_limit)
                return self._ensure_uploadable(video, work_dir, upload_limit)
            except Exception as exc:
                print(f'[WARN] Douyin parse API failed, falling back to yt-dlp: {exc}')

        if self.use_f2 and self.f2_available:
            try:
                video = self._download_video_with_f2(candidate, work_dir)
                return self._ensure_uploadable(video, work_dir, upload_limit)
            except Exception as exc:
                print(f'[WARN] Douyin f2 fallback failed, falling back to yt-dlp: {exc}')

        last_error: Exception | None = None
        heights = self._candidate_heights()
        for height in heights:
            self._clear_work_dir(work_dir)
            try:
                video = self._download_video_at_height(candidate, work_dir, height)
            except Exception as exc:
                last_error = exc
                continue

            if video.file_path.stat().st_size <= upload_limit:
                return video

            fitted = self._ensure_uploadable(video, work_dir, upload_limit)
            if fitted.file_path.stat().st_size <= upload_limit or height == heights[-1]:
                return fitted

        if last_error is not None:
            raise last_error
        raise RuntimeError('没有找到可下载的视频格式。')

    def _ensure_uploadable(
        self,
        video: DownloadedDouyinVideo,
        work_dir: Path,
        upload_limit: int,
    ) -> DownloadedDouyinVideo:
        if video.file_path.stat().st_size <= upload_limit:
            return video
        compressed = self._compress_video_if_needed(video, work_dir, upload_limit)
        return compressed or video

    def _download_video_with_parse_api(
        self,
        candidate: DouyinCandidate,
        work_dir: Path,
        upload_limit: int,
    ) -> DownloadedDouyinVideo:
        last_error: Exception | None = None
        for index, parse_api_url in enumerate(self.parse_api_urls, start=1):
            try:
                api_response = self._request_parse_api(parse_api_url, candidate.source_url)
                video_url = self._extract_video_url(api_response)
                if not video_url:
                    raise RuntimeError('第三方解析接口没有返回可用的视频直链。')

                title = self._extract_title(api_response) or 'Douyin video'
                webpage_url = self._extract_webpage_url(api_response) or candidate.source_url
                output_path = work_dir / f'douyin-api-{index}.mp4'
                self._download_direct_video(video_url, output_path, upload_limit)

                return DownloadedDouyinVideo(
                    title=title,
                    webpage_url=webpage_url,
                    file_path=output_path,
                    duration_seconds=None,
                )
            except Exception as exc:
                last_error = exc
                print(f'[WARN] Douyin parse API #{index} failed: {exc}')

        if last_error is not None:
            raise last_error
        raise RuntimeError('没有配置第三方解析接口。')

    def _download_video_with_f2(
        self,
        candidate: DouyinCandidate,
        work_dir: Path,
    ) -> DownloadedDouyinVideo:
        cookie_header = self._read_cookie_header()
        if not cookie_header:
            raise RuntimeError('f2 需要登录后的抖音 cookie。')

        command = [
            sys.executable,
            '-m',
            'f2',
            'dy',
            '-M',
            'one',
            '-u',
            candidate.source_url,
            '-p',
            str(work_dir),
            '-m',
            'false',
            '-v',
            'false',
            '-d',
            'false',
            '-f',
            'false',
            '-o',
            '1',
            '-e',
            str(max(self.download_timeout_seconds, 30)),
            '-r',
            '1',
            '-k',
            cookie_header,
        ]
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        result = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            text=True,
            encoding='utf-8',
            errors='replace',
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.download_timeout_seconds,
        )
        if result.returncode != 0:
            raise RuntimeError(self._compact_process_output(result.stdout))

        video_path = self._find_downloaded_video_file_recursive(work_dir)
        if video_path is None:
            raise RuntimeError('f2 运行完成后没有找到视频文件。')

        return DownloadedDouyinVideo(
            title=video_path.stem,
            webpage_url=candidate.source_url,
            file_path=video_path,
            duration_seconds=self._probe_duration_seconds(video_path),
        )

    def _read_cookie_header(self) -> str:
        if self.cookie_file is None or not self.cookie_file.is_file():
            return ''
        pairs: list[str] = []
        try:
            lines = self.cookie_file.read_text(
                encoding='utf-8-sig',
                errors='replace',
            ).splitlines()
        except OSError:
            return ''

        for line in lines:
            line = line.strip()
            if line.startswith('#HttpOnly_'):
                line = line[len('#HttpOnly_'):]
            elif not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 7 or 'douyin.com' not in parts[0]:
                continue
            pairs.append(f'{parts[5]}={parts[6]}')
        return '; '.join(pairs)

    def _compact_process_output(self, output: str, limit: int = 800) -> str:
        compact = ' '.join(output.split())
        if not compact:
            return 'f2 执行失败。'
        if len(compact) > limit:
            compact = compact[-limit:]
        return compact
    def _request_parse_api(self, parse_api_url: str, source_url: str) -> object:
        encoded_url = urllib.parse.quote(source_url, safe='')
        api_url = parse_api_url
        if '{url}' in api_url:
            api_url = api_url.replace('{url}', encoded_url)
        else:
            separator = '&' if '?' in api_url else '?'
            api_url = f'{api_url}{separator}url={encoded_url}'

        request = urllib.request.Request(
            api_url,
            headers={
                'Accept': 'application/json,text/plain,*/*',
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
            },
        )
        with urllib.request.urlopen(
            request,
            timeout=self.parse_api_timeout_seconds,
        ) as response:
            body = response.read(2 * 1024 * 1024)

        text = body.decode('utf-8-sig', errors='replace').strip()
        if text.startswith(('callback(', 'jsonp(')) and text.endswith(')'):
            text = text[text.find('(') + 1 : -1]
        return json.loads(text)

    def _download_direct_video(
        self,
        video_url: str,
        output_path: Path,
        upload_limit: int,
    ) -> None:
        request = urllib.request.Request(
            video_url,
            headers={
                'Accept': '*/*',
                'Referer': 'https://www.douyin.com/',
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
            },
        )
        byte_limit = min(self.download_max_bytes, max(upload_limit * 3, upload_limit))
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content_length = response.headers.get('Content-Length')
                if content_length and int(content_length) > self.download_max_bytes:
                    raise RuntimeError(
                        f'解析出的直链文件过大：{_format_size(int(content_length))}'
                    )

                with output_path.open('wb') as file_obj:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > byte_limit:
                            raise RuntimeError(
                                f'解析出的直链超过下载限制：{_format_size(byte_limit)}'
                            )
                        file_obj.write(chunk)
        except urllib.error.URLError as exc:
            raise RuntimeError(f'下载第三方解析直链失败：{exc}') from exc

        if not output_path.is_file() or output_path.stat().st_size <= 1024:
            raise RuntimeError('第三方解析直链下载后文件为空。')

    def _extract_video_url(self, data: object) -> str | None:
        preferred_names = {
            'video_url',
            'play_url',
            'no_watermark',
            'nwm_video_url',
            'download_url',
            'wmplay',
            'nwmplay',
        }
        fallback_urls: list[str] = []
        for key, value in self._walk_json_values(data):
            if not isinstance(value, str) or not value.startswith(('http://', 'https://')):
                continue
            if not self._looks_like_video_url(value):
                continue
            key_name = str(key).lower()
            if key_name in preferred_names:
                return value
            fallback_urls.append(value)
        return fallback_urls[0] if fallback_urls else None

    def _extract_title(self, data: object) -> str | None:
        for key, value in self._walk_json_values(data):
            if str(key).lower() in {'title', 'desc', 'description'} and isinstance(value, str):
                title = value.strip()
                if title:
                    return title[:120]
        return None

    def _extract_webpage_url(self, data: object) -> str | None:
        for key, value in self._walk_json_values(data):
            if str(key).lower() in {'share_url', 'webpage_url'} and isinstance(value, str):
                if value.startswith(('http://', 'https://')):
                    return value
        return None

    def _walk_json_values(self, data: object):
        if isinstance(data, dict):
            for key, value in data.items():
                yield key, value
                yield from self._walk_json_values(value)
        elif isinstance(data, list):
            for value in data:
                yield '', value
                yield from self._walk_json_values(value)

    def _looks_like_video_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if path.endswith(('.mp4', '.mov', '.m4v')):
            return True
        return any(
            marker in host
            for marker in (
                'douyinvod',
                'byteimg',
                'ixigua',
                'amemv',
                'snssdk',
                'aweme',
            )
        )

    def _download_video_at_height(
        self,
        candidate: DouyinCandidate,
        work_dir: Path,
        height: int,
    ) -> DownloadedDouyinVideo:
        ydl_opts = {
            'format': self._format_selector(height),
            'merge_output_format': 'mp4',
            'outtmpl': str(work_dir / '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'noprogress': True,
            'overwrites': True,
            'retries': 3,
            'fragment_retries': 3,
            'socket_timeout': 20,
            'max_filesize': self.download_max_bytes,
            'http_headers': {
                'Referer': 'https://www.douyin.com/',
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
            },
        }
        if self.ffmpeg_executable:
            ydl_opts['ffmpeg_location'] = self.ffmpeg_executable
        if self.cookie_file is not None:
            ydl_opts['cookiefile'] = str(self.cookie_file)
        elif self.cookies_from_browser is not None:
            ydl_opts['cookiesfrombrowser'] = (self.cookies_from_browser,)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(candidate.source_url, download=True)

        video_path = self._find_downloaded_video_file(work_dir)
        if video_path is None:
            raise RuntimeError('下载完成后没有找到视频文件。')

        return DownloadedDouyinVideo(
            title=str(info.get('title') or info.get('id') or 'Douyin video'),
            webpage_url=str(info.get('webpage_url') or candidate.source_url),
            file_path=video_path,
            duration_seconds=self._read_duration(info),
        )

    def _format_selector(self, height: int) -> str:
        return (
            f'bestvideo[height<={height}]+bestaudio/'
            f'bestvideo[width<={height}]+bestaudio/'
            f'best[ext=mp4][height<={height}]/'
            f'best[ext=mp4][width<={height}]/'
            f'best[height<={height}]/'
            f'best[width<={height}]/'
            'best'
        )

    def _candidate_heights(self) -> list[int]:
        heights = [self.max_height, 1080, 720, 480, 360]
        unique: list[int] = []
        for height in heights:
            if height not in unique and height <= self.max_height:
                unique.append(height)
        return unique or [720, 480, 360]

    def _upload_limit_for_guild(self, guild: discord.Guild | None) -> int:
        if self.max_upload_bytes_override is not None:
            return self.max_upload_bytes_override
        guild_limit = getattr(guild, 'filesize_limit', None)
        if isinstance(guild_limit, int) and guild_limit > 0:
            return guild_limit
        return DEFAULT_DISCORD_UPLOAD_LIMIT_BYTES

    def _read_duration(self, info: dict[str, object]) -> float | None:
        raw = info.get('duration')
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
        return None

    def _read_max_upload_bytes_override(self) -> int | None:
        raw = os.getenv('DOUYIN_MAX_UPLOAD_MB', '').strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return max(int(value * 1024 * 1024), 1024 * 1024)

    def _read_download_max_bytes(self) -> int:
        raw = os.getenv('DOUYIN_DOWNLOAD_MAX_MB', '').strip()
        if not raw:
            return DEFAULT_DOWNLOAD_MAX_BYTES
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_DOWNLOAD_MAX_BYTES
        return max(int(value * 1024 * 1024), 8 * 1024 * 1024)

    def _read_parse_api_urls(self) -> list[str]:
        raw = os.getenv('DOUYIN_PARSE_API_URLS', '').strip()
        if not raw:
            raw = os.getenv('DOUYIN_PARSE_API_URL', '').strip()
        if not raw:
            return []
        urls = []
        for item in re.split(r'[\r\n|]+', raw):
            url = item.strip()
            if url:
                urls.append(url)
        return urls

    def _read_cookie_file(self) -> Path | None:
        raw = os.getenv('DOUYIN_COOKIE_FILE', '').strip()
        if raw:
            path = Path(raw)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[1] / path
            if path.is_file():
                return path
            print(f'[WARN] DOUYIN_COOKIE_FILE does not exist: {path}')

        for path in self._default_cookie_file_candidates():
            if path.is_file():
                print(f'[INFO] Douyin cookie file auto-detected: {path}')
                return path
        return None

    def reload_cookie_file(self) -> bool:
        """Refresh the configured cookie path after a protected update."""

        self.cookie_file = self._read_cookie_file()
        print(
            '[INFO] Douyin credential hot-reloaded: '
            f'configured={self.cookie_file is not None}'
        )
        return self.cookie_file is not None

    def _default_cookie_file_candidates(self) -> list[Path]:
        project_root = Path(__file__).resolve().parents[1]
        home = Path.home()
        return [
            project_root / 'config' / 'credentials' / 'douyin_cookies.txt',
            project_root / 'douyin' / 'cookies.txt',
            project_root / 'douyin' / 'www.douyin.com_cookies.txt',
            project_root / 'cookies.txt',
            project_root / 'www.douyin.com_cookies.txt',
            home / 'Downloads' / 'cookies.txt',
            home / 'Desktop' / 'www.douyin.com_cookies.txt',
            home / 'Downloads' / 'www.douyin.com_cookies.txt',
        ]

    def _read_cookies_from_browser(self) -> str | None:
        raw = os.getenv('DOUYIN_COOKIES_FROM_BROWSER', '').strip().lower()
        if not raw:
            return None
        allowed = {'brave', 'chrome', 'chromium', 'edge', 'firefox', 'opera', 'safari', 'vivaldi'}
        if raw not in allowed:
            print(f'[WARN] Unsupported DOUYIN_COOKIES_FROM_BROWSER value: {raw}')
            return None
        return raw

    def _format_error(self, exc: Exception) -> str:
        text = str(exc)
        lowered = text.lower()
        if 'could not copy chrome cookie database' in lowered or (
            'permission denied' in lowered and 'cookies' in lowered
        ):
            return (
                '浏览器 cookie 数据库正在被占用，yt-dlp 读不到。'
                '请先完全关闭 Chrome/Edge 后重启 bot；如果仍失败，'
                '改用 DOUYIN_COOKIE_FILE=config/credentials/douyin_cookies.txt。'
            )
        if 'fresh cookies' in lowered or ('cookies' in lowered and 'needed' in lowered):
            cookie_hint = self._cookie_file_diagnostic_hint()
            if cookie_hint:
                return cookie_hint
            return (
                '抖音要求 fresh cookies。请把导出的 cookies.txt '
                '放到 config/credentials/ 目录；也可以在 .env 设置 '
                'DOUYIN_COOKIES_FROM_BROWSER=edge/chrome/firefox。'
            )
        return text

    def _cookie_file_diagnostic_hint(self) -> str | None:
        if self.cookie_file is None or not self.cookie_file.is_file():
            return None
        try:
            text = self.cookie_file.read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            return None

        cookie_names: set[str] = set()
        for line in text.splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7:
                cookie_names.add(parts[5])

        if 's_v_web_id' not in cookie_names:
            return (
                '这份抖音 cookie 文件缺少 s_v_web_id，yt-dlp 会判定不是 fresh cookies。'
                '请在浏览器打开 https://www.douyin.com/ 或那条视频链接，等页面正常播放后，'
                '用 Get cookies.txt LOCALLY 重新导出完整 cookies，覆盖 '
                'config/credentials/douyin_cookies.txt。'
            )
        if 'sessionid' not in cookie_names:
            return (
                '这份抖音 cookie 文件不像是登录后的 cookie。'
                '请先在浏览器登录网页版抖音，打开视频页确认能播放，再重新导出 cookies。'
            )
        return (
            'cookie 文件已读到，但抖音接口仍然拒绝了这份 cookie。'
            '这不是缺某一条固定 cookie，而是当前 yt-dlp 的抖音解析器被抖音风控/签名校验挡住了。'
            '换 cookies.txt 或 Firefox 直读也可能一样失败，只能等 yt-dlp 修复或换解析方案。'
        )

    def _resolve_ffmpeg_executable(self) -> str | None:
        ffmpeg_executable = shutil.which('ffmpeg')
        if ffmpeg_executable:
            return ffmpeg_executable
        if imageio_ffmpeg is None:
            return None
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    def _temp_root(self) -> Path:
        root = Path(__file__).resolve().parents[1] / 'douyin_cache'
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _clear_work_dir(self, work_dir: Path) -> None:
        for path in work_dir.iterdir():
            if path.is_file():
                self._unlink_with_retries(path)

    def _unlink_with_retries(self, path: Path) -> None:
        for attempt in range(8):
            try:
                path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.25 * (attempt + 1))

    def _remove_tree_with_retries(self, path: Path) -> None:
        for attempt in range(8):
            try:
                shutil.rmtree(path)
                return
            except FileNotFoundError:
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.25 * (attempt + 1))

    def _find_downloaded_video_file(self, work_dir: Path) -> Path | None:
        candidates = [
            path
            for path in work_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {'.mp4', '.mkv', '.webm', '.flv'}
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_size)

    def _find_downloaded_video_file_recursive(self, work_dir: Path) -> Path | None:
        candidates = [
            path
            for path in work_dir.rglob('*')
            if path.is_file() and path.suffix.lower() in {'.mp4', '.mkv', '.webm', '.flv'}
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_size)

    def _probe_duration_seconds(self, file_path: Path) -> float | None:
        if self.ffmpeg_executable is None:
            return None
        try:
            result = subprocess.run(
                [self.ffmpeg_executable, '-i', str(file_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=30,
            )
        except Exception:
            return None

        match = re.search(
            r'Duration:\s*(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}(?:\.\d+)?)',
            result.stderr,
        )
        if match is None:
            return None
        hours = int(match.group('h'))
        minutes = int(match.group('m'))
        seconds = float(match.group('s'))
        return hours * 3600 + minutes * 60 + seconds

    def _compress_video_if_needed(
        self,
        video: DownloadedDouyinVideo,
        work_dir: Path,
        upload_limit: int,
    ) -> DownloadedDouyinVideo | None:
        if not self.compress_if_needed or self.ffmpeg_executable is None:
            return None
        if video.duration_seconds is None or video.duration_seconds <= 0:
            return None

        output_path = work_dir / 'compressed.mp4'
        target_total_bits = int(upload_limit * 8 * 0.90)
        total_kbps = max(int(target_total_bits / video.duration_seconds / 1000), 160)
        audio_kbps = 64 if total_kbps < 360 else 96
        video_kbps = max(total_kbps - audio_kbps, 96)
        scale_width = max(self.compress_width, 360)

        command = [
            self.ffmpeg_executable,
            '-y',
            '-i',
            str(video.file_path),
            '-vf',
            f"scale='min({scale_width},iw)':-2",
            '-c:v',
            'libx264',
            '-preset',
            'veryfast',
            '-b:v',
            f'{video_kbps}k',
            '-maxrate',
            f'{video_kbps}k',
            '-bufsize',
            f'{video_kbps * 2}k',
            '-c:a',
            'aac',
            '-b:a',
            f'{audio_kbps}k',
            '-movflags',
            '+faststart',
            str(output_path),
        ]
        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(60, min(int(video.duration_seconds * 3), 600)),
            )
        except Exception as exc:
            print(f'[WARN] Failed to compress Douyin video: {exc}')
            return None

        if not output_path.is_file() or output_path.stat().st_size <= 1024:
            return None
        return DownloadedDouyinVideo(
            title=video.title,
            webpage_url=video.webpage_url,
            file_path=output_path,
            duration_seconds=video.duration_seconds,
        )
