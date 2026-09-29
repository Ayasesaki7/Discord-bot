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
from dataclasses import dataclass, replace
from pathlib import Path
from http.cookiejar import CookieJar, MozillaCookieJar

import discord
from discord import app_commands
from discord.ext import commands

from .public import (
    DOUYIN_HOSTS, DouyinAccessError, DouyinWatermarkError, open_public_url,
    parse_public_video, video_id_from_url, is_watermarked_url, unwatermarked_url,
)

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
VideoSender = Callable[[DownloadedDouyinVideo, str], Awaitable[None]]


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
        self.use_f2 = _env_flag('DOUYIN_USE_F2', False)
        self.public_share_enabled = _env_flag('DOUYIN_PUBLIC_SHARE_ENABLED', True)
        self.risk_cooldown_seconds = _env_int('DOUYIN_RISK_COOLDOWN_SECONDS', 120, minimum=0)
        self.risk_blocked_until = 0.0
        self._download_deadline = 0.0
        self._cleanup_tasks: set[asyncio.Task] = set()
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
            print('[WARN] Douyin yt-dlp fallback unavailable; public-share resolver is still enabled')
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

        async def send_video(video: DownloadedDouyinVideo, filename: str) -> None:
            file = discord.File(video.file_path, filename=filename)
            try:
                await interaction.followup.send(
                    view=self._build_video_card(video, filename),
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
                f'user_id={interaction.user.id}, channel_id={interaction.channel_id}, error={self._format_error(exc)}'
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
            status_message = await ctx.reply('正在解析抖音视频...', mention_author=False)

        async def update_status(content: str) -> None:
            if status_message is None:
                return
            await status_message.edit(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        async def send_video(video: DownloadedDouyinVideo, filename: str) -> None:
            file = discord.File(video.file_path, filename=filename)
            try:
                await ctx.channel.send(
                    view=self._build_video_card(video, filename),
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
                f'user_id={ctx.author.id}, channel_id={ctx.channel.id}, error={self._format_error(exc)}'
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
        if time.monotonic() < self.risk_blocked_until:
            remaining = max(1, round(self.risk_blocked_until - time.monotonic()))
            raise DouyinAccessError(f'上次抖音公开访问被拒绝，暂停重复请求，还需等待约 {remaining} 秒。')
        if self.download_lock.locked():
            raise RuntimeError('已有抖音视频正在处理或清理，请等它结束后再发。')
        await self.download_lock.acquire()
        work_dir = None
        handed_off = False
        try:
            work_dir = Path(tempfile.mkdtemp(prefix='douyin_', dir=str(self._temp_root())))
            worker = asyncio.create_task(asyncio.to_thread(
                self._download_video, candidate, work_dir, upload_limit,
            ))
            try:
                video = await asyncio.wait_for(asyncio.shield(worker), timeout=self.download_timeout_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Cancelling to_thread cannot stop its thread. Keep its lock and files
                # alive until it exits, so cleanup/new downloads never race with it.
                handed_off = True
                task = asyncio.create_task(self._finish_abandoned_download(worker, work_dir))
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)
                raise
            file_size = video.file_path.stat().st_size
            if file_size > upload_limit:
                await update_status(
                    f'视频太大啦：{_format_size(file_size)}，'
                    f'当前频道最多只能上传 {_format_size(upload_limit)}。\n'
                    f'原链接：{video.webpage_url}'
                )
                return
            await update_status(f'解析完成，正在上传：{video.title} ({_format_size(file_size)})')
            # Keep a machine-safe attachment name so Markdown/media references agree.
            await send_video(video, 'douyin-video' + video.file_path.suffix.lower())
            await update_status(f'已发送：{video.title}')
        except DouyinAccessError:
            self.risk_blocked_until = time.monotonic() + self.risk_cooldown_seconds
            raise
        finally:
            if not handed_off:
                try:
                    if work_dir is not None:
                        await asyncio.to_thread(self._remove_tree_with_retries, work_dir)
                finally:
                    self.download_lock.release()

    async def _finish_abandoned_download(self, worker: asyncio.Task, work_dir: Path) -> None:
        try:
            await worker
        except DouyinAccessError:
            self.risk_blocked_until = time.monotonic() + self.risk_cooldown_seconds
        except Exception as exc:
            print(f'[INFO] Douyin background download ended: {type(exc).__name__}')
        finally:
            try:
                await asyncio.to_thread(self._remove_tree_with_retries, work_dir)
            except Exception as exc:
                print(f'[WARN] Douyin temporary cleanup failed: {type(exc).__name__}')
            finally:
                self.download_lock.release()

    def _build_video_card(
        self, video: DownloadedDouyinVideo, filename: str,
    ) -> discord.ui.LayoutView:
        title = ' '.join(video.title.split()) or '抖音视频'
        title = discord.utils.escape_mentions(discord.utils.escape_markdown(title[:180]))
        details = []
        if video.duration_seconds is not None and video.duration_seconds > 0:
            hours, remainder = divmod(round(video.duration_seconds), 3600)
            minutes, seconds = divmod(remainder, 60)
            duration = f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'
            details.append(f'⏱ {duration}')
        details.append(f'📦 {_format_size(video.file_path.stat().st_size)}')
        container = discord.ui.Container(
            discord.ui.TextDisplay('-# DOUYIN · 抖音视频'),
            discord.ui.TextDisplay(f'### {title}'),
            discord.ui.MediaGallery(discord.MediaGalleryItem(
                f'attachment://{filename}', description=video.title[:256],
            )),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay('-# ' + '　·　'.join(details)),
            discord.ui.ActionRow(discord.ui.Button(
                label='在抖音打开', style=discord.ButtonStyle.link,
                emoji='↗️', url=video.webpage_url,
            )),
            accent_colour=0x25F4EE,
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        return view

    def _find_candidate(self, text: str) -> DouyinCandidate | None:
        match = DOUYIN_URL_PATTERN.search(text)
        if match is None:
            return None
        return DouyinCandidate(source_url=self._normalize_douyin_url(match.group(0).rstrip('，。；！!、』」】')))

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
        self._download_deadline = time.monotonic() + self.download_timeout_seconds
        routes = []
        if self.public_share_enabled:
            routes.append(('public-share', lambda: self._download_public_video(candidate, work_dir, upload_limit)))
            if self.cookie_file is not None and self.cookie_file.is_file():
                routes.append(('cookie-share', lambda: self._download_cookie_video(candidate, work_dir, upload_limit)))
        if yt_dlp is not None:
            routes.append(('yt-dlp', lambda: self._download_video_at_height(candidate, work_dir, self.max_height)))
        if self.use_f2 and self.f2_available and self.cookie_file is not None:
            routes.append(('f2', lambda: self._download_video_with_f2(candidate, work_dir)))
        if self.parse_api_urls:
            routes.append(('configured-api', lambda: self._download_video_with_parse_api(candidate, work_dir, upload_limit)))
        last_error = None
        access_failed = False
        access_detail = ''
        for label, run in routes:
            self._remaining_timeout()
            self._clear_work_dir(work_dir)
            try:
                video = run()
                video = self._ensure_uploadable(video, work_dir, upload_limit)
                print(f'[INFO] Douyin resolved: route={label}, bytes={video.file_path.stat().st_size}')
                return video
            except TimeoutError:
                raise
            except Exception as exc:
                last_error = exc
                access_failed = access_failed or self._is_access_error(exc)
                if self._is_access_error(exc):
                    access_detail = self._format_error(exc)
                print(f'[WARN] Douyin {label} failed: {self._format_error(exc)}')
        if last_error is not None:
            if access_failed:
                raise DouyinAccessError(
                    '原站没有提供可用视频，已配置的备用解析也未成功。'
                    f'访问错误：{access_detail} '
                    '请确认原链接在浏览器可播放，再检查凭证与服务器访问情况；'
                    '单纯重启 BOT 或反复重试不能解除原站访问限制。'
                ) from None
            raise last_error
        raise RuntimeError('没有启用可用的抖音解析方式。')

    def _remaining_timeout(self, maximum: float = 30) -> float:
        deadline = getattr(self, '_download_deadline', 0.0)
        if not deadline:
            return maximum
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('抖音视频处理超时。')
        return min(maximum, remaining)

    def _download_public_video(
        self, candidate: DouyinCandidate, work_dir: Path, upload_limit: int,
    ) -> DownloadedDouyinVideo:
        return self._download_share_video(candidate, work_dir, upload_limit)

    def _download_cookie_video(
        self, candidate: DouyinCandidate, work_dir: Path, upload_limit: int,
    ) -> DownloadedDouyinVideo:
        try:
            source = MozillaCookieJar(str(self.cookie_file))
            source.load(ignore_discard=True, ignore_expires=False)
        except (OSError, ValueError):
            raise RuntimeError('抖音 Cookie 文件无法读取或格式无效，请通过凭证管理更新。') from None
        jar = CookieJar()
        for cookie in source:
            if cookie.domain.lstrip('.').lower() in DOUYIN_HOSTS:
                jar.set_cookie(cookie)
        if not len(jar):
            raise RuntimeError('抖音 Cookie 文件没有未过期的相关凭证，请通过凭证管理更新。')
        return self._download_share_video(candidate, work_dir, upload_limit, cookie_jar=jar)

    def _download_share_video(
        self, candidate: DouyinCandidate, work_dir: Path, upload_limit: int,
        *, cookie_jar: CookieJar | None = None,
    ) -> DownloadedDouyinVideo:
        options = {'allowed_hosts': DOUYIN_HOSTS}
        if cookie_jar is not None:
            options['cookie_jar'] = cookie_jar
        video_id = video_id_from_url(candidate.source_url)
        if video_id is None:
            with open_public_url(candidate.source_url, timeout=self._remaining_timeout(20),
                                 **options) as response:
                video_id = video_id_from_url(response.geturl())
            if video_id is None:
                raise RuntimeError('该分享链接不是可识别的单条视频，暂不支持图集、直播或合集。')
        # The legacy iesdouyin host often returns a valid HTML shell without
        # videoInfoRes. Use the current official share host, which also matches
        # the domain of the user's exported Douyin cookies.
        url = f'https://www.douyin.com/share/video/{video_id}/'
        with open_public_url(url, timeout=self._remaining_timeout(20),
                             **options) as response:
            chunks, size = [], 0
            while True:
                self._remaining_timeout()
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    raise RuntimeError('抖音分享页超出解析大小限制。')
                chunks.append(chunk)
        info = parse_public_video(b''.join(chunks).decode('utf-8', 'replace'), video_id)
        path = work_dir / 'douyin-public.mp4'
        self._download_direct_video(info.video_url, path, upload_limit)
        return DownloadedDouyinVideo(
            title=info.title, webpage_url=f'https://www.douyin.com/video/{video_id}',
            file_path=path, duration_seconds=info.duration_seconds,
        )

    def _ensure_uploadable(
        self,
        video: DownloadedDouyinVideo,
        work_dir: Path,
        upload_limit: int,
    ) -> DownloadedDouyinVideo:
        if video.file_path.stat().st_size > self.download_max_bytes:
            raise RuntimeError(f'视频超过下载限制：{_format_size(self.download_max_bytes)}')
        if video.duration_seconds is None:
            video = replace(video, duration_seconds=self._probe_duration_seconds(video.file_path))
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
        for index, parse_api_url in enumerate(self.parse_api_urls[:3], start=1):
            try:
                self._remaining_timeout()
                api_response = self._request_parse_api(parse_api_url, candidate.source_url)
                video_url = self._extract_video_url(api_response)
                if not video_url:
                    raise RuntimeError('第三方解析接口没有返回可用的视频直链。')

                title = self._extract_title(api_response) or 'Douyin video'
                webpage_url = candidate.source_url
                output_path = work_dir / f'douyin-api-{index}.mp4'
                self._download_direct_video(video_url, output_path, upload_limit)

                return DownloadedDouyinVideo(
                    title=title,
                    webpage_url=webpage_url,
                    file_path=output_path,
                    duration_seconds=None,
                )
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    raise
                last_error = exc
                print(f'[WARN] Douyin parse API #{index} failed: {self._format_error(exc)}')

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
            raise RuntimeError('f2 备用解析需要配置可用的抖音 Cookie。')

        arguments = [
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
            str(max(1, int(self._remaining_timeout(20)))),
            '-r',
            '1',
            '-k',
            cookie_header,
        ]
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        # F2 can exit 0 after an HTTP failure. Inspect only a bounded private
        # capture for status codes; never forward its cookies/configuration.
        # Its logs and SQLite files also belong to this job, not the project.
        with tempfile.TemporaryFile() as capture:
            result = subprocess.run(
                [sys.executable, '-c',
                 'import json, sys; from f2.cli.cli_commands import main; main(args=json.load(sys.stdin))'],
                input=json.dumps(arguments), cwd=str(work_dir), env=env,
                text=True, encoding='utf-8', errors='replace',
                stdout=capture, stderr=subprocess.STDOUT,
                timeout=self._remaining_timeout(60),
            )
            capture.seek(0)
            diagnostic = capture.read(64 * 1024).decode('utf-8', 'replace')
        diagnostic = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', diagnostic)
        denied = re.search(r'HTTP/\S+[^\d\r\n]{1,8}(401|403|412|429)\b', diagnostic)
        video_path = self._find_downloaded_video_file_recursive(work_dir)
        if video_path is None and denied:
            raise DouyinAccessError(
                f'F2 访问抖音接口被拒绝（HTTP {denied.group(1)}），没有下载到视频。'
            )
        if result.returncode != 0:
            raise RuntimeError(f'F2 备用解析失败（退出码 {result.returncode}），不循环重试。')

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
        try:
            jar = MozillaCookieJar(str(self.cookie_file))
            jar.load(ignore_discard=True, ignore_expires=False)
            request = urllib.request.Request('https://www.douyin.com/')
            jar.add_cookie_header(request)
            return request.get_header('Cookie') or ''
        except (OSError, ValueError):
            return ''

    def _compact_process_output(self, output: str, limit: int = 800) -> str:
        compact = ' '.join(output.split())
        if not compact:
            return 'f2 执行失败。'
        if len(compact) > limit:
            compact = compact[-limit:]
        return self._safe_error_text(compact)

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
            timeout=self._remaining_timeout(self.parse_api_timeout_seconds),
        ) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise RuntimeError('第三方解析响应过大。')

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
        total = 0
        try:
            video_url = unwatermarked_url(video_url)
            with open_public_url(video_url, timeout=self._remaining_timeout(20)) as response:
                if is_watermarked_url(response.geturl()):
                    raise DouyinWatermarkError('播放地址又跳转到了水印版本，已停止下载。')
                content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
                if content_type and not (content_type.startswith('video/') or content_type in {
                    'application/octet-stream', 'binary/octet-stream', 'application/mp4',
                }):
                    raise RuntimeError('直链返回的不是视频文件（可能是验证页或图片）。')
                content_length = response.headers.get('Content-Length')
                expected = int(content_length) if content_length and content_length.isdecimal() else None
                if expected is not None and expected > self.download_max_bytes:
                    raise RuntimeError(f'视频超过下载限制：{_format_size(self.download_max_bytes)}')
                with output_path.open('wb') as file_obj:
                    while True:
                        self._remaining_timeout()
                        chunk = response.read1(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.download_max_bytes:
                            raise RuntimeError(f'视频超过下载限制：{_format_size(self.download_max_bytes)}')
                        if not file_obj.tell() and (chunk.lstrip().startswith((b'<', b'{', b'['))):
                            raise RuntimeError('直链返回的是网页或错误数据，不是视频。')
                        file_obj.write(chunk)
                if expected is not None and total != expected:
                    raise RuntimeError('视频下载不完整，请稍后重试。')
                if total <= 1024:
                    raise RuntimeError('视频文件为空或过小。')
        except BaseException:
            output_path.unlink(missing_ok=True)
            raise

    def _extract_video_url(self, data: object) -> str | None:
        clean_names = {'no_watermark', 'nwm_video_url', 'nwmplay', 'nowm', 'nowatermark', 'play_no_watermark'}
        watermarked_names = {'wmplay', 'playwm', 'wm_url', 'watermark_url', 'watermarked_url', 'watermark'}
        preferred_names = {'video_url', 'play_url', 'download_url'}
        candidates = []

        def visit(value, priority=2):
            if isinstance(value, dict):
                for key, child in value.items():
                    key = str(key).lower()
                    if key not in watermarked_names:
                        rank = 0 if key in clean_names else 1 if key in preferred_names else 2
                        visit(child, min(priority, rank))
            elif isinstance(value, list):
                for child in value:
                    visit(child, priority)
            elif isinstance(value, str) and value.startswith(('http://', 'https://')) and self._looks_like_video_url(value):
                try:
                    candidates.append((priority, unwatermarked_url(value)))
                except DouyinWatermarkError:
                    pass

        visit(data)
        return min(candidates, key=lambda item: item[0])[1] if candidates else None

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
        if path.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif')):
            return False
        if path.endswith(('.mp4', '.mov', '.m4v')):
            return True
        return any(
            marker in host
            for marker in (
                'douyinvod',
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
            'retries': 0,
            'fragment_retries': 0,
            'extractor_retries': 0,
            'socket_timeout': self._remaining_timeout(20),
            'progress_hooks': [lambda _status: self._remaining_timeout()],
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
            # yt-dlp saves its cookie jar on exit (even on failure). Give it a
            # per-job copy, not the credential manager's persistent browser export.
            cookie_copy = work_dir / 'cookies.txt'
            try:
                source = MozillaCookieJar(str(self.cookie_file))
                source.load(ignore_discard=True, ignore_expires=False)
                filtered = MozillaCookieJar(str(cookie_copy))
                for cookie in source:
                    if cookie.domain.lstrip('.').lower() in DOUYIN_HOSTS:
                        filtered.set_cookie(cookie)
                filtered.save(ignore_discard=True, ignore_expires=False)
                cookie_copy.chmod(0o600)
            except (OSError, ValueError):
                raise RuntimeError('抖音 Cookie 文件无法读取或格式无效，请通过凭证管理更新 Netscape 格式 Cookie。') from None
            ydl_opts['cookiefile'] = str(cookie_copy)
        elif self.cookies_from_browser is not None:
            ydl_opts['cookiesfrombrowser'] = (self.cookies_from_browser,)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(candidate.source_url, download=True)

        video_path = self._find_downloaded_video_file(work_dir)
        if video_path is None:
            raise RuntimeError('下载完成后没有找到视频文件。')

        return DownloadedDouyinVideo(
            title=str(info.get('title') or info.get('id') or 'Douyin video'),
            webpage_url=candidate.source_url,
            file_path=video_path,
            duration_seconds=self._read_duration(info),
        )

    def _format_selector(self, height: int) -> str:
        # Do not accept yt-dlp's lower-priority watermarked download_addr when
        # ordinary playback formats are unavailable. '?' permits absent notes.
        clean = '[format_note!*=?watermarked][url!*=?playwm]'
        video, audio, best = f'bestvideo{clean}', f'bestaudio{clean}', f'best{clean}'
        return (
            f'{video}[height<={height}]+{audio}/'
            f'{video}[width<={height}]+{audio}/'
            f'{best}[ext=mp4][height<={height}]/'
            f'{best}[ext=mp4][width<={height}]/'
            f'{best}[height<={height}]/'
            f'{best}[width<={height}]/'
            f'{best}'
        )

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
        return [
            project_root / 'config' / 'credentials' / 'douyin_cookies.txt',
            project_root / 'douyin' / 'cookies.txt',
            project_root / 'douyin' / 'www.douyin.com_cookies.txt',
            project_root / 'cookies.txt',
            project_root / 'www.douyin.com_cookies.txt',
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

    @staticmethod
    def _safe_error_text(text: str) -> str:
        text = re.sub(r'https?://[^\s\'"<>]+', '[链接已隐藏]', text)
        text = re.sub(
            r'(?i)\b([\w]*(?:token|cookie|sessionid|sid_tt|sid_guard|ttwid|api_key)[\w]*)\s*=\s*[^;\s\'"]+',
            r'\1=[已隐藏]', text,
        )
        return discord.utils.escape_mentions(' '.join(text.split())[:500])

    @staticmethod
    def _is_access_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return isinstance(exc, DouyinAccessError) or any(
            marker in text for marker in ('fresh cookies', 'cookies are needed', 'http error 403',
                                          'http error 412', 'http error 429', 'captcha', '验证码')
        )

    def _format_error(self, exc: Exception) -> str:
        if isinstance(exc, subprocess.TimeoutExpired):
            return '抖音备用下载或压缩进程超时。'
        if isinstance(exc, TimeoutError):
            return '抖音视频处理超时，正在清理后台任务。'
        if 'fresh cookies' in str(exc).lower():
            return (
                'yt-dlp 没有从抖音详情接口拿到有效视频数据。'
                '该通用错误不能证明 Cookie 已过期，也可能是接口限制或作品不可用；'
                '若需更新凭证请使用凭证管理，不要反复请求同一接口。'
            )
        return self._safe_error_text(str(exc)) or type(exc).__name__

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
                timeout=self._remaining_timeout(30),
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
                timeout=self._remaining_timeout(max(60, min(int(video.duration_seconds * 3), 600))),
            )
        except Exception as exc:
            print(f'[WARN] Failed to compress Douyin video: {self._format_error(exc)}')
            return None

        if not output_path.is_file() or output_path.stat().st_size <= 1024:
            return None
        return DownloadedDouyinVideo(
            title=video.title,
            webpage_url=video.webpage_url,
            file_path=output_path,
            duration_seconds=video.duration_seconds,
        )
