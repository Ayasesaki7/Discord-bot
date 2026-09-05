from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
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


BILIBILI_VIDEO_URL_PATTERN = re.compile(
    r'https?://(?:(?:www|m)\.)?bilibili\.com/video/'
    r'(?P<bvid>BV[0-9A-Za-z]{10})(?P<tail>[^\s<>()]*)?',
    re.IGNORECASE,
)
BILIBILI_SHORT_URL_PATTERN = re.compile(
    r'https?://b23\.tv/[0-9A-Za-z]+(?:[^\s<>()]*)?',
    re.IGNORECASE,
)
BV_ID_PATTERN = re.compile(r'(?<![0-9A-Za-z])(?P<bvid>BV[0-9A-Za-z]{10})(?![0-9A-Za-z])')

DEFAULT_DISCORD_UPLOAD_LIMIT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_HEIGHT = 480
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 240
DEFAULT_DOWNLOAD_MAX_BYTES = 128 * 1024 * 1024
BILIBILI_QN_BY_HEIGHT = (
    (1080, 80),
    (720, 64),
    (480, 32),
    (360, 16),
)


@dataclass(frozen=True)
class BilibiliCandidate:
    source_url: str
    bvid: str | None


@dataclass(frozen=True)
class DownloadedBilibiliVideo:
    title: str
    webpage_url: str
    bvid: str | None
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


def _safe_filename(text: str, fallback: str = 'bilibili-video') -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', ' ', text).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return (cleaned or fallback)[:80]


class BilibiliVideoCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.max_height = _env_int('BILIBILI_MAX_HEIGHT', DEFAULT_MAX_HEIGHT, minimum=144)
        self.download_timeout_seconds = _env_int(
            'BILIBILI_DOWNLOAD_TIMEOUT_SECONDS',
            DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
            minimum=30,
        )
        self.max_upload_bytes_override = self._read_max_upload_bytes_override()
        self.download_max_bytes = self._read_download_max_bytes()
        self.compress_if_needed = _env_flag('BILIBILI_COMPRESS_IF_NEEDED', True)
        self.cookie_file = self._read_cookie_file()
        self.ffmpeg_executable = self._resolve_ffmpeg_executable()
        self.download_lock = asyncio.Lock()

        if yt_dlp is None:
            print('[WARN] Bilibili resolver disabled: yt-dlp is not installed')
        if self.ffmpeg_executable is None:
            print('[WARN] Bilibili resolver could not find ffmpeg; merged videos may fail')

    @app_commands.command(
        name='bv',
        description='解析 B 站链接或 BV 号，并把视频文件发到当前频道',
    )
    @app_commands.describe(link_or_bv='B 站视频链接、b23 短链或 BV 号')
    async def send_bilibili_video(
        self,
        interaction: discord.Interaction,
        link_or_bv: str,
    ) -> None:
        if yt_dlp is None:
            await interaction.response.send_message(
                'B 站视频解析功能缺少 yt-dlp 依赖，暂时不能使用。',
                ephemeral=True,
            )
            return

        candidate = self._find_candidate(link_or_bv)
        if candidate is None:
            await interaction.response.send_message(
                '没有识别到 B 站视频链接或 BV 号。',
                ephemeral=True,
            )
            return

        channel = interaction.channel
        if channel is None or not hasattr(channel, 'send'):
            await interaction.response.send_message(
                '当前上下文里没有可以发送视频的频道。',
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        label = candidate.bvid or candidate.source_url
        await interaction.edit_original_response(content=f'正在解析 B 站视频：{label}')

        async def update_status(content: str) -> None:
            await interaction.edit_original_response(content=content)

        async def send_video(content: str, file_path: Path, filename: str) -> None:
            await interaction.followup.send(
                content=content,
                file=discord.File(file_path, filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=False,
            )

        try:
            await self._resolve_and_send_video(
                guild=interaction.guild,
                candidate=candidate,
                update_status=update_status,
                send_video=send_video,
            )
        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content='解析 B 站视频超时了，稍后可以再试一次。',
            )
        except Exception as exc:
            print(
                '[WARN] Failed to resolve Bilibili video: '
                f'user_id={interaction.user.id}, channel_id={interaction.channel_id}, error={exc}'
            )
            await interaction.edit_original_response(
                content=f'解析 B 站视频失败：{exc}',
            )

    @commands.command(
        name='bv',
        aliases=['bili', 'b站', 'b站视频', '哔哩视频'],
    )
    async def send_bilibili_video_prefix(
        self,
        ctx: commands.Context,
        *,
        link_or_bv: str,
    ) -> None:
        if yt_dlp is None:
            await ctx.reply(
                'B 站视频解析功能缺少 yt-dlp 依赖，暂时不能使用。',
                mention_author=False,
            )
            return

        candidate = self._find_candidate(link_or_bv)
        if candidate is None:
            await ctx.reply(
                '没有识别到 B 站视频链接或 BV 号。',
                mention_author=False,
            )
            return

        if not hasattr(ctx.channel, 'send'):
            await ctx.reply('当前上下文里没有可以发送视频的频道。', mention_author=False)
            return

        label = candidate.bvid or candidate.source_url
        try:
            status_message = await ctx.author.send(f'正在解析 B 站视频：{label}')
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
            await ctx.channel.send(
                content=content,
                file=discord.File(file_path, filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        try:
            await self._resolve_and_send_video(
                guild=ctx.guild,
                candidate=candidate,
                update_status=update_status,
                send_video=send_video,
            )
        except asyncio.TimeoutError:
            await update_status('解析 B 站视频超时了，稍后可以再试一次。')
        except Exception as exc:
            print(
                '[WARN] Failed to resolve Bilibili video: '
                f'user_id={ctx.author.id}, channel_id={ctx.channel.id}, error={exc}'
            )
            await update_status(f'解析 B 站视频失败：{exc}')

    @send_bilibili_video_prefix.error
    async def send_bilibili_video_prefix_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                '用法：`!bv BV1gZ5j6zEbX` 或 `!bv https://www.bilibili.com/video/BV.../`',
                mention_author=False,
            )
            return
        raise error

    async def _resolve_and_send_video(
        self,
        *,
        guild: discord.Guild | None,
        candidate: BilibiliCandidate,
        update_status: StatusUpdater,
        send_video: VideoSender,
    ) -> None:
        upload_limit = self._upload_limit_for_guild(guild)
        async with self.download_lock:
            with tempfile.TemporaryDirectory(prefix='bilibili_', dir=str(self._temp_root())) as tmp_dir:
                video = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._download_video,
                        candidate,
                        Path(tmp_dir),
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
                    f'B 站视频：{video.title}\n{video.webpage_url}',
                    video.file_path,
                    f'{_safe_filename(video.title)}.mp4',
                )
                await update_status(f'已发送：{video.title}')

    def _find_candidate(self, text: str) -> BilibiliCandidate | None:
        url_match = BILIBILI_VIDEO_URL_PATTERN.search(text)
        if url_match is not None:
            url = url_match.group(0)
            bvid = url_match.group('bvid')
            return BilibiliCandidate(source_url=url, bvid=bvid)

        short_match = BILIBILI_SHORT_URL_PATTERN.search(text)
        if short_match is not None:
            return BilibiliCandidate(source_url=short_match.group(0), bvid=None)

        bv_match = BV_ID_PATTERN.search(text)
        if bv_match is not None:
            bvid = bv_match.group('bvid')
            return BilibiliCandidate(
                source_url=f'https://www.bilibili.com/video/{bvid}/',
                bvid=bvid,
            )
        return None

    def _download_video(
        self,
        candidate: BilibiliCandidate,
        work_dir: Path,
        upload_limit: int,
    ) -> DownloadedBilibiliVideo:
        last_error: Exception | None = None
        heights = self._candidate_heights()
        for height in heights:
            self._clear_work_dir(work_dir)
            try:
                video = self._download_video_at_height(candidate, work_dir, height, upload_limit)
            except Exception as exc:
                last_error = exc
                continue

            if video.file_path.stat().st_size <= upload_limit:
                return video

            compressed = self._compress_video_if_needed(video, work_dir, upload_limit)
            if compressed is not None and compressed.file_path.stat().st_size <= upload_limit:
                return compressed

            if height == heights[-1]:
                return compressed or video

        if last_error is not None:
            raise last_error
        raise RuntimeError('没有找到可下载的视频格式。')

    def _download_video_at_height(
        self,
        candidate: BilibiliCandidate,
        work_dir: Path,
        height: int,
        upload_limit: int,
    ) -> DownloadedBilibiliVideo:
        headers = self._bilibili_headers(candidate.source_url)
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
            'http_headers': headers,
        }
        if self.ffmpeg_executable:
            ydl_opts['ffmpeg_location'] = self.ffmpeg_executable
        if self.cookie_file is not None:
            ydl_opts['cookiefile'] = str(self.cookie_file)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(candidate.source_url, download=True)
        except Exception as exc:
            if self._looks_like_bilibili_412(exc):
                print('[WARN] Bilibili webpage returned 412; falling back to public API download')
                return self._download_video_via_api(candidate, work_dir, height, upload_limit)
            raise

        video_path = self._find_downloaded_video_file(work_dir)
        if video_path is None:
            raise RuntimeError('下载完成后没有找到视频文件。')

        return DownloadedBilibiliVideo(
            title=str(info.get('title') or candidate.bvid or 'Bilibili video'),
            webpage_url=str(info.get('webpage_url') or candidate.source_url),
            bvid=str(info.get('id') or candidate.bvid) if (info.get('id') or candidate.bvid) else None,
            file_path=video_path,
            duration_seconds=self._read_duration(info),
        )

    def _download_video_via_api(
        self,
        candidate: BilibiliCandidate,
        work_dir: Path,
        height: int,
        upload_limit: int,
    ) -> DownloadedBilibiliVideo:
        bvid = self._bvid_for_api(candidate)
        if bvid is None:
            raise RuntimeError('B 站网页被 412 拦截，且没有拿到 BV 号，无法切换 API 备用解析。')

        page_url = f'https://www.bilibili.com/video/{bvid}/'
        view_url = (
            'https://api.bilibili.com/x/web-interface/view?'
            + urllib.parse.urlencode({'bvid': bvid})
        )
        view = self._read_bilibili_json(view_url, referer='https://www.bilibili.com/')
        if int(view.get('code') or 0) != 0:
            raise RuntimeError(f'B 站 API 获取视频信息失败：{view.get("message") or view.get("code")}')

        view_data = view.get('data') or {}
        if not isinstance(view_data, dict):
            raise RuntimeError('B 站 API 返回的视频信息格式不正确。')

        cid = view_data.get('cid')
        if not cid:
            pages = view_data.get('pages')
            if isinstance(pages, list) and pages and isinstance(pages[0], dict):
                cid = pages[0].get('cid')
        if not cid:
            raise RuntimeError('B 站 API 没有返回 cid，无法下载视频。')

        qn = self._quality_for_height(height)
        play_url = (
            'https://api.bilibili.com/x/player/playurl?'
            + urllib.parse.urlencode(
                {
                    'bvid': bvid,
                    'cid': str(cid),
                    'qn': str(qn),
                    'fnval': '0',
                    'fourk': '0',
                }
            )
        )
        play = self._read_bilibili_json(play_url, referer=page_url)
        if int(play.get('code') or 0) != 0:
            raise RuntimeError(f'B 站 API 获取播放地址失败：{play.get("message") or play.get("code")}')

        play_data = play.get('data') or {}
        if not isinstance(play_data, dict):
            raise RuntimeError('B 站 API 返回的播放地址格式不正确。')

        durl_items = play_data.get('durl')
        if not isinstance(durl_items, list) or not durl_items:
            raise RuntimeError('B 站 API 没有返回可直接下载的 MP4 地址。')

        first_item = durl_items[0]
        if not isinstance(first_item, dict):
            raise RuntimeError('B 站 API 返回的 MP4 地址格式不正确。')

        expected_size = first_item.get('size')
        if isinstance(expected_size, int) and expected_size > min(self.download_max_bytes, upload_limit * 3):
            raise RuntimeError(f'视频文件太大：{_format_size(expected_size)}')

        download_urls = [first_item.get('url')]
        backup_urls = first_item.get('backup_url')
        if isinstance(backup_urls, list):
            download_urls.extend(backup_urls)
        download_urls = [url for url in download_urls if isinstance(url, str) and url]
        if not download_urls:
            raise RuntimeError('B 站 API 没有返回有效下载地址。')

        output_path = work_dir / f'{bvid}.mp4'
        self._download_direct_file(download_urls, output_path, referer=page_url)

        duration_seconds = None
        timelength = play_data.get('timelength')
        if isinstance(timelength, (int, float)) and timelength > 0:
            duration_seconds = float(timelength) / 1000

        return DownloadedBilibiliVideo(
            title=str(view_data.get('title') or bvid),
            webpage_url=page_url,
            bvid=bvid,
            file_path=output_path,
            duration_seconds=duration_seconds,
        )

    def _bvid_for_api(self, candidate: BilibiliCandidate) -> str | None:
        if candidate.bvid:
            return candidate.bvid
        match = BV_ID_PATTERN.search(candidate.source_url)
        if match is not None:
            return match.group('bvid')
        try:
            request = urllib.request.Request(
                candidate.source_url,
                headers=self._bilibili_headers('https://www.bilibili.com/'),
                method='HEAD',
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                final_url = response.geturl()
        except Exception:
            return None
        match = BV_ID_PATTERN.search(final_url)
        if match is not None:
            return match.group('bvid')
        return None

    def _read_bilibili_json(self, url: str, *, referer: str) -> dict[str, object]:
        request = urllib.request.Request(url, headers=self._bilibili_headers(referer))
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))

    def _download_direct_file(self, urls: list[str], output_path: Path, *, referer: str) -> None:
        last_error: Exception | None = None
        limit = self.download_max_bytes
        for url in urls:
            try:
                request = urllib.request.Request(url, headers=self._bilibili_headers(referer))
                with urllib.request.urlopen(request, timeout=30) as response, output_path.open('wb') as file:
                    downloaded = 0
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > limit:
                            raise RuntimeError(f'视频文件超过下载上限：{_format_size(limit)}')
                        file.write(chunk)
                if output_path.is_file() and output_path.stat().st_size > 1024:
                    return
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError('B 站 API 下载地址不可用。')

    def _quality_for_height(self, height: int) -> int:
        for min_height, qn in BILIBILI_QN_BY_HEIGHT:
            if height >= min_height:
                return qn
        return 16

    def _bilibili_headers(self, referer: str) -> dict[str, str]:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Referer': referer,
            'Origin': 'https://www.bilibili.com',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        cookie_header = self._cookie_header()
        if cookie_header:
            headers['Cookie'] = cookie_header
        return headers

    def _cookie_header(self) -> str | None:
        if self.cookie_file is None:
            return None
        try:
            pairs: list[str] = []
            for line in self.cookie_file.read_text(encoding='utf-8', errors='ignore').splitlines():
                line = line.strip()
                if line.startswith('#HttpOnly_'):
                    line = line[len('#HttpOnly_'):]
                elif not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    name = parts[5].strip()
                    value = parts[6].strip()
                    if name:
                        pairs.append(f'{name}={value}')
                    continue
                if '=' in line and ';' not in line:
                    pairs.append(line)
            return '; '.join(pairs) if pairs else None
        except Exception as exc:
            print(f'[WARN] Failed to read Bilibili cookie header: {exc}')
            return None

    def _looks_like_bilibili_412(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return '412' in text and 'precondition failed' in text

    def _format_selector(self, height: int) -> str:
        return (
            f'bestvideo[ext=mp4][vcodec^=avc1][height<={height}]+bestaudio[ext=m4a]/'
            f'bestvideo[ext=mp4][vcodec^=avc1][width<={height}]+bestaudio[ext=m4a]/'
            f'bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/'
            f'bestvideo[ext=mp4][width<={height}]+bestaudio[ext=m4a]/'
            f'best[ext=mp4][height<={height}]/'
            f'best[ext=mp4][width<={height}]/'
            f'best[height<={height}]/'
            f'best[width<={height}]/'
            'best'
        )

    def _candidate_heights(self) -> list[int]:
        heights = [self.max_height, 480, 360]
        unique: list[int] = []
        for height in heights:
            if height not in unique and height <= self.max_height:
                unique.append(height)
        return unique or [360]

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
        raw = os.getenv('BILIBILI_MAX_UPLOAD_MB', '').strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return max(int(value * 1024 * 1024), 1024 * 1024)

    def _read_download_max_bytes(self) -> int:
        raw = os.getenv('BILIBILI_DOWNLOAD_MAX_MB', '').strip()
        if not raw:
            return DEFAULT_DOWNLOAD_MAX_BYTES
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_DOWNLOAD_MAX_BYTES
        return max(int(value * 1024 * 1024), 8 * 1024 * 1024)

    def _read_cookie_file(self) -> Path | None:
        raw = os.getenv('BILIBILI_COOKIE_FILE', '').strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        if path.is_file():
            return path
        print(f'[WARN] BILIBILI_COOKIE_FILE does not exist: {path}')
        return None

    def reload_cookie_file(self) -> bool:
        """Refresh the configured cookie path after a protected update."""

        self.cookie_file = self._read_cookie_file()
        print(
            '[INFO] Bilibili credential hot-reloaded: '
            f'configured={self.cookie_file is not None}'
        )
        return self.cookie_file is not None

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
        root = Path(__file__).resolve().parents[1] / 'bilibili_cache'
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _clear_work_dir(self, work_dir: Path) -> None:
        for path in work_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)

    def _find_downloaded_video_file(self, work_dir: Path) -> Path | None:
        candidates = [
            path
            for path in work_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {'.mp4', '.mkv', '.webm', '.flv'}
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_size)

    def _compress_video_if_needed(
        self,
        video: DownloadedBilibiliVideo,
        work_dir: Path,
        upload_limit: int,
    ) -> DownloadedBilibiliVideo | None:
        if not self.compress_if_needed or self.ffmpeg_executable is None:
            return None
        if video.duration_seconds is None or video.duration_seconds <= 0:
            return None

        output_path = work_dir / 'compressed.mp4'
        target_total_bits = int(upload_limit * 8 * 0.88)
        total_kbps = max(int(target_total_bits / video.duration_seconds / 1000), 96)
        audio_kbps = 48 if total_kbps < 220 else 64
        video_kbps = max(total_kbps - audio_kbps, 80)

        command = [
            self.ffmpeg_executable,
            '-y',
            '-i',
            str(video.file_path),
            '-vf',
            "scale='min(640,iw)':-2",
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
            print(f'[WARN] Failed to compress Bilibili video: {exc}')
            return None

        if not output_path.is_file() or output_path.stat().st_size <= 1024:
            return None
        return DownloadedBilibiliVideo(
            title=video.title,
            webpage_url=video.webpage_url,
            bvid=video.bvid,
            file_path=output_path,
            duration_seconds=video.duration_seconds,
        )
