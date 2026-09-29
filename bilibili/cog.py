from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from http.cookiejar import MozillaCookieJar

import discord
from discord import app_commands
from discord.ext import commands

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

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
    part_title: str | None = None


StatusUpdater = Callable[[str], Awaitable[None]]
VideoSender = Callable[[DownloadedBilibiliVideo, str], Awaitable[None]]


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


class BilibiliApiError(RuntimeError):
    """A non-retryable metadata/playback error, not a quality selection failure."""


class BilibiliRiskControlError(BilibiliApiError):
    pass


class BilibiliVideoTooLarge(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
        self.risk_cooldown_seconds = _env_int('BILIBILI_RISK_COOLDOWN_SECONDS', 120, minimum=0)
        self.risk_blocked_until = 0.0
        self.title_cache = {}
        self.title_blocked_until = 0.0
        self._download_deadline = 0.0
        self._cleanup_tasks: set[asyncio.Task] = set()

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

        async def send_video(video: DownloadedBilibiliVideo, filename: str) -> None:
            with closing(discord.File(video.file_path, filename=filename)) as file:
                await interaction.followup.send(
                    view=self._build_video_card(video, filename), file=file,
                    allowed_mentions=discord.AllowedMentions.none(), ephemeral=False,
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

        async def send_video(video: DownloadedBilibiliVideo, filename: str) -> None:
            with closing(discord.File(video.file_path, filename=filename)) as file:
                await ctx.channel.send(
                    view=self._build_video_card(video, filename), file=file,
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
        if self.download_lock.locked():
            raise BilibiliApiError('已有 B 站视频正在处理或清理，请等它结束后再发。')
        await self.download_lock.acquire()
        work_dir = None
        handed_off = False
        try:
            remaining = self.risk_blocked_until - time.monotonic()
            if remaining > 0:
                raise BilibiliRiskControlError(
                    f'B 站暂时拒绝服务器请求，正在冷却，请约 {int(remaining) + 1} 秒后再试。'
                )
            work_dir = Path(tempfile.mkdtemp(prefix='bilibili_', dir=str(self._temp_root())))
            worker = asyncio.create_task(asyncio.to_thread(
                self._download_video, candidate, work_dir, upload_limit,
            ))
            try:
                video = await asyncio.wait_for(asyncio.shield(worker), timeout=self.download_timeout_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # to_thread cancellation does not terminate its worker. Retain the
                # lock and directory until it exits, including on command cancel.
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
            await send_video(video, f'{_safe_filename(video.bvid or "bilibili-video")}.mp4')
            await update_status(f'已发送：{video.title}')
        except BilibiliRiskControlError:
            self.risk_blocked_until = time.monotonic() + self.risk_cooldown_seconds
            raise
        finally:
            if not handed_off:
                try:
                    if work_dir is not None:
                        await asyncio.to_thread(shutil.rmtree, work_dir)
                finally:
                    self.download_lock.release()

    async def _finish_abandoned_download(self, worker: asyncio.Task, work_dir: Path) -> None:
        try:
            await worker
        except BilibiliRiskControlError:
            self.risk_blocked_until = time.monotonic() + self.risk_cooldown_seconds
        except Exception as exc:
            print(f'[INFO] Bilibili background download ended: {type(exc).__name__}')
        finally:
            try:
                await asyncio.to_thread(shutil.rmtree, work_dir)
            except OSError as exc:
                print(f'[WARN] Bilibili temporary cleanup failed: {type(exc).__name__}')
            finally:
                self.download_lock.release()

    def _build_video_card(
        self, video: DownloadedBilibiliVideo, filename: str,
    ) -> discord.ui.LayoutView:
        title = ' '.join(video.title.split()) or 'B 站视频'
        if len(title) > 180:
            title = title[:179] + '…'
        title = discord.utils.escape_mentions(discord.utils.escape_markdown(title))
        details = []
        if video.duration_seconds is not None and video.duration_seconds > 0:
            seconds = round(video.duration_seconds)
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration = f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'
            details.append(f'⏱ {duration}')
        details.append(f'📦 {_format_size(video.file_path.stat().st_size)}')
        if video.bvid:
            details.append(f'`{video.bvid}`')
        page = urllib.parse.parse_qs(urllib.parse.urlsplit(video.webpage_url).query).get('p', ['1'])[-1]
        if page.isdecimal() and int(page) > 1:
            details.append(f'P{int(page)}')

        heading = [discord.ui.TextDisplay('-# BILIBILI · 视频分享'),
                   discord.ui.TextDisplay(f'### {title}')]
        if video.part_title and video.part_title != video.title:
            part = ' '.join(video.part_title.split())[:120]
            part = discord.utils.escape_mentions(discord.utils.escape_markdown(part))
            heading.append(discord.ui.TextDisplay(f'-# 分 P · {part}'))
        container = discord.ui.Container(
            *heading,
            discord.ui.MediaGallery(discord.MediaGalleryItem(
                f'attachment://{filename}', description=video.title[:256],
            )),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay('-# ' + '　·　'.join(details)),
            discord.ui.ActionRow(discord.ui.Button(
                label='在 B 站打开', style=discord.ButtonStyle.link,
                emoji='↗️', url=video.webpage_url,
            )),
            accent_colour=0xFB7299,
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(container)
        return view

    def _find_candidate(self, text: str) -> BilibiliCandidate | None:
        url_match = BILIBILI_VIDEO_URL_PATTERN.search(text)
        if url_match is not None:
            url = url_match.group(0).rstrip('，。；！!、』」】')
            bvid = url_match.group('bvid')
            return BilibiliCandidate(source_url=url, bvid=bvid)

        short_match = BILIBILI_SHORT_URL_PATTERN.search(text)
        if short_match is not None:
            return BilibiliCandidate(source_url=short_match.group(0).rstrip('，。；！!、』」】'), bvid=None)

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
        self._download_deadline = time.monotonic() + self.download_timeout_seconds
        candidate = self._resolve_candidate(candidate)
        page_info = self._read_page_info(candidate)
        page_info = {**page_info, 'archive_title': self._read_archive_title(candidate)}
        print(f'[INFO] Bilibili direct API resolver: bvid={candidate.bvid}, page={page_info["page"]}')
        last_error: Exception | None = None
        heights = self._candidate_heights()
        for height in heights:
            self._remaining_timeout()
            self._clear_work_dir(work_dir)
            try:
                video = self._download_video_via_api(
                    candidate, work_dir, height, upload_limit, page_info=page_info,
                )
            except BilibiliVideoTooLarge as exc:
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

    def _remaining_timeout(self, maximum: float = 30) -> float:
        deadline = getattr(self, '_download_deadline', 0.0)
        if not deadline:
            return maximum
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('B 站视频处理超时。')
        return min(maximum, remaining)

    def _resolve_candidate(self, candidate: BilibiliCandidate) -> BilibiliCandidate:
        if candidate.bvid:
            return candidate
        # Stop at the redirect header: fetching the destination HTML would
        # reintroduce the webpage 412 dependency this resolver avoids.
        url = candidate.source_url
        opener = urllib.request.build_opener(_NoRedirect())
        for _ in range(4):
            parsed = urllib.parse.urlsplit(url)
            if parsed.hostname not in {'b23.tv', 'www.bilibili.com', 'm.bilibili.com', 'bilibili.com'}:
                raise BilibiliApiError('短链跳转到了非 B 站地址，已拒绝访问。')
            found = BILIBILI_VIDEO_URL_PATTERN.search(url)
            if found:
                return BilibiliCandidate(url, found.group('bvid'))
            request = urllib.request.Request(
                url, headers=self._bilibili_headers('https://www.bilibili.com/', include_cookie=False),
            )
            try:
                with opener.open(request, timeout=self._remaining_timeout(15)) as response:
                    location = response.headers.get('Location')
            except urllib.error.HTTPError as exc:
                try:
                    if exc.code not in {301, 302, 303, 307, 308}:
                        self._raise_http_error(exc.code, '短链')
                    location = exc.headers.get('Location')
                finally:
                    exc.close()
            if not location:
                raise BilibiliApiError('短链没有返回视频跳转地址，请直接发送 BV 号或完整视频链接。')
            url = urllib.parse.urljoin(url, location)
        raise BilibiliApiError('短链跳转次数过多，请直接发送 BV 号。')

    def _read_archive_title(self, candidate: BilibiliCandidate) -> str | None:
        """Optional bounded metadata lookup; never make media depend on /view or HTML."""
        bvid = candidate.bvid
        if not bvid:
            return None
        now = time.monotonic()
        cached = self.title_cache.get(bvid)
        if cached and cached[0] > now:
            return cached[1]
        if now < self.title_blocked_until:
            return None
        title = None
        try:
            url = 'https://api.bilibili.com/x/web-interface/wbi/view?' + urllib.parse.urlencode({'bvid': bvid})
            payload = self._read_bilibili_json(url, referer=f'https://www.bilibili.com/video/{bvid}/', timeout=6)
            data = payload.get('data')
            if isinstance(data, dict) and data.get('bvid') == bvid and isinstance(data.get('title'), str):
                title = data['title'].strip()[:1000] or None
        except BilibiliRiskControlError:
            # Only title lookups cool down; pagelist/playurl may still be usable.
            self.title_blocked_until = time.monotonic() + self.risk_cooldown_seconds
            print('[WARN] Bilibili title lookup risk-controlled; video download can continue')
        except (BilibiliApiError, OSError):
            print('[WARN] Bilibili title unavailable; video download can continue')
        self.title_cache[bvid] = (time.monotonic() + (3600 if title else 300), title)
        while len(self.title_cache) > 128:
            self.title_cache.pop(next(iter(self.title_cache)))
        return title

    def _read_page_info(self, candidate: BilibiliCandidate) -> dict[str, object]:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(candidate.source_url).query)
        try:
            page_number = int(query.get('p', ['1'])[-1])
        except (TypeError, ValueError):
            raise BilibiliApiError('分 P 参数 p 必须是正整数。') from None
        if page_number < 1:
            raise BilibiliApiError('分 P 参数 p 必须是正整数。')
        url = 'https://api.bilibili.com/x/player/pagelist?' + urllib.parse.urlencode(
            {'bvid': candidate.bvid},
        )
        data = self._read_bilibili_json(url, referer='https://www.bilibili.com/').get('data')
        if not isinstance(data, list) or not data:
            raise BilibiliApiError('没有读取到分 P 信息，视频可能已删除或不可访问。')
        for item in data:
            if isinstance(item, dict) and item.get('page') == page_number and item.get('cid'):
                return {**item, 'page_count': len(data)}
        raise BilibiliApiError(f'这个视频没有第 {page_number} P。')

    def _download_video_via_api(
        self,
        candidate: BilibiliCandidate,
        work_dir: Path,
        height: int,
        upload_limit: int,
        *,
        page_info: dict[str, object],
    ) -> DownloadedBilibiliVideo:
        bvid = candidate.bvid
        page_number = page_info['page']
        page_url = f'https://www.bilibili.com/video/{bvid}/'
        if page_number != 1:
            page_url += '?' + urllib.parse.urlencode({'p': page_number})
        play_url = 'https://api.bilibili.com/x/player/playurl?' + urllib.parse.urlencode({
            'bvid': bvid, 'cid': str(page_info['cid']),
            'qn': str(self._quality_for_height(height)), 'fnval': '0', 'fourk': '0',
        })
        play_data = self._read_bilibili_json(play_url, referer=page_url).get('data')
        if not isinstance(play_data, dict):
            raise BilibiliApiError('B 站返回的播放地址格式不正确。')
        if play_data.get('is_preview'):
            raise BilibiliApiError('该视频只允许试看，未下载或发送不完整视频；请检查账号观看权限。')

        output_path = work_dir / f'{bvid}.mp4'
        items = play_data.get('durl')
        dash = play_data.get('dash')
        if isinstance(items, list) and items:
            if not all(isinstance(item, dict) for item in items):
                raise BilibiliApiError('B 站返回的视频分段格式不正确。')
            total_size = sum(self._positive_int(item.get('size')) for item in items)
            if total_size > self.download_max_bytes:
                raise BilibiliVideoTooLarge(f'视频文件超过下载上限：{_format_size(total_size)}')
            if len(items) > 1 and not self.ffmpeg_executable:
                raise BilibiliApiError('这是分段视频，需要安装 ffmpeg 才能完整合并。')
            paths = []
            remaining = self.download_max_bytes
            for index, item in enumerate(items):
                part_path = output_path if len(items) == 1 else work_dir / f'part-{index:03d}.media'
                self._download_direct_file(
                    self._media_urls(item), part_path, referer=page_url, limit=remaining,
                )
                remaining -= part_path.stat().st_size
                paths.append(part_path)
            # Legacy API can return FLV segments. Never silently send only the
            # first segment or call an FLV file MP4.
            if len(paths) > 1 or 'mp4' not in str(play_data.get('format', 'mp4')).lower():
                if paths == [output_path]:
                    source = work_dir / 'part-000.media'
                    output_path.rename(source)
                    paths = [source]
                self._merge_media(paths, output_path, concatenate=True)
        elif isinstance(dash, dict):
            if not self.ffmpeg_executable:
                raise BilibiliApiError('该视频需要合并音视频，请先安装 ffmpeg。')
            videos = [item for item in dash.get('video', []) if isinstance(item, dict)]
            audios = [item for item in dash.get('audio', []) if isinstance(item, dict)]
            if not videos:
                raise BilibiliApiError('播放接口没有返回视频轨道。')
            fitting = [item for item in videos if self._positive_int(item.get('height')) <= height]
            pool = fitting or videos
            # Prefer widely playable H.264, then the requested resolution.
            video_track = max(pool, key=lambda item: (
                str(item.get('codecs', '')).startswith('avc'),
                self._positive_int(item.get('height')) * (1 if fitting else -1),
            ))
            tracks = [video_track]
            if audios:
                aac = [item for item in audios if str(item.get('codecs', '')).startswith('mp4a')]
                tracks.append(max(aac or audios, key=lambda item: self._positive_int(item.get('bandwidth'))))
            paths = []
            remaining = self.download_max_bytes
            for index, track in enumerate(tracks):
                path = work_dir / f'track-{index}.media'
                self._download_direct_file(self._media_urls(track), path, referer=page_url, limit=remaining)
                remaining -= path.stat().st_size
                paths.append(path)
            self._merge_media(paths, output_path, concatenate=False)
        else:
            raise BilibiliApiError('没有返回可观看的视频流，可能需要登录或没有观看权限。')

        timelength = play_data.get('timelength')
        duration = float(timelength) / 1000 if isinstance(timelength, (int, float)) and timelength > 0 else None
        return DownloadedBilibiliVideo(
            title=str(page_info.get('archive_title') or f'B 站视频 · {bvid}'), webpage_url=page_url,
            bvid=bvid, file_path=output_path, duration_seconds=duration,
            part_title=str(page_info.get('part') or '') if page_info.get('page_count', 1) > 1 else None,
        )

    @staticmethod
    def _positive_int(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _media_urls(item: dict[str, object]) -> list[str]:
        primary = item.get('url') or item.get('baseUrl') or item.get('base_url')
        backups = item.get('backup_url') or item.get('backupUrl') or []
        urls = [primary, *(backups if isinstance(backups, list) else [])]
        valid = []
        for url in urls:
            if not isinstance(url, str):
                continue
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username:
                continue
            if url not in valid:
                valid.append(url)
        if not valid:
            raise BilibiliApiError('B 站没有返回有效媒体下载地址。')
        return valid[:3]

    def _merge_media(self, paths: list[Path], output_path: Path, *, concatenate: bool) -> None:
        if not self.ffmpeg_executable:
            raise BilibiliApiError('这个视频格式需要 ffmpeg 转封装才能发送。')
        command = [self.ffmpeg_executable, '-nostdin', '-y']
        if concatenate:
            manifest = output_path.parent / 'segments.txt'
            manifest.write_text(''.join(f"file '{path.name}'\n" for path in paths), encoding='utf-8')
            command += ['-f', 'concat', '-safe', '1', '-i', str(manifest)]
        else:
            for path in paths:
                command += ['-i', str(path)]
            command += ['-map', '0:v:0']
            if len(paths) > 1:
                command += ['-map', '1:a:0']
        command += ['-c', 'copy', '-movflags', '+faststart', str(output_path)]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=self._remaining_timeout(120))
        except subprocess.TimeoutExpired:
            raise TimeoutError('B 站音视频合并超时。') from None
        except (subprocess.SubprocessError, OSError):
            raise BilibiliApiError('视频下载完成，但 ffmpeg 合并失败，未发送不完整视频。') from None
        if not output_path.is_file() or output_path.stat().st_size <= 1024:
            raise BilibiliApiError('音视频合并后没有生成有效文件。')

    @staticmethod
    def _raise_http_error(status: int, stage: str) -> None:
        if status in {412, 429}:
            raise BilibiliRiskControlError(
                f'B 站{stage}接口暂时拒绝服务器请求（HTTP {status}）。'
                '已停止自动重试，请稍后再试；如需登录请更新自己的 B 站 Cookie。'
            )
        raise BilibiliApiError(f'B 站{stage}请求失败（HTTP {status}）。')

    def _read_bilibili_json(self, url: str, *, referer: str, timeout: float = 20) -> dict[str, object]:
        stage = '标题' if '/wbi/view' in url else '分 P' if '/pagelist' in url else '播放'
        request = urllib.request.Request(url, headers=self._bilibili_headers(referer))
        try:
            with urllib.request.urlopen(request, timeout=self._remaining_timeout(timeout)) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            try:
                self._raise_http_error(exc.code, stage)
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError):
            raise BilibiliApiError(f'连接 B 站{stage}接口失败或超时，请稍后再试。') from None
        if len(raw) > 2 * 1024 * 1024:
            raise BilibiliApiError(f'B 站{stage}接口返回数据过大。')
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise BilibiliApiError(f'B 站{stage}接口没有返回有效 JSON。') from None
        if not isinstance(payload, dict) or 'code' not in payload:
            raise BilibiliApiError(f'B 站{stage}接口返回格式不正确。')
        code = payload['code']
        if code in {-352, -412, 412, 429}:
            raise BilibiliRiskControlError(
                f'B 站{stage}接口触发风控（代码 {code}），已停止重试，请稍后再试。'
            )
        if code != 0:
            raise BilibiliApiError(
                f'B 站{stage}接口返回错误码 {code}，请检查视频是否公开、可播放或需要登录。'
            )
        return payload

    def _download_direct_file(
        self, urls: list[str], output_path: Path, *, referer: str, limit: int | None = None,
    ) -> None:
        last_error: Exception | None = None
        limit = self.download_max_bytes if limit is None else min(limit, self.download_max_bytes)
        if limit <= 0:
            raise BilibiliVideoTooLarge('视频分段总大小超过下载上限。')
        for url in urls:
            try:
                self._remaining_timeout()
                request = urllib.request.Request(url, headers=self._bilibili_headers(referer, include_cookie=False))
                with urllib.request.urlopen(request, timeout=self._remaining_timeout(30)) as response, output_path.open('wb') as file:
                    content_type = response.headers.get('Content-Type', '').lower()
                    if 'text/html' in content_type or 'application/json' in content_type:
                        raise BilibiliApiError('媒体服务器返回了错误页面，未保存为视频。')
                    declared_size = self._positive_int(response.headers.get('Content-Length'))
                    if declared_size > limit:
                        raise BilibiliVideoTooLarge('媒体文件超过剩余下载上限。')
                    downloaded = 0
                    while True:
                        self._remaining_timeout()
                        # read1 yields after one socket read even on slow CDN
                        # streams, so trickling bytes cannot evade the deadline.
                        chunk = response.read1(1024 * 256)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > limit:
                            raise BilibiliVideoTooLarge(f'视频文件超过下载上限：{_format_size(limit)}')
                        file.write(chunk)
                    if declared_size and downloaded != declared_size:
                        raise BilibiliApiError('媒体下载中途断开，未保存为完整视频。')
                if output_path.is_file() and output_path.stat().st_size > 1024:
                    return
                raise BilibiliApiError('媒体下载结果为空或不完整。')
            except BilibiliVideoTooLarge:
                output_path.unlink(missing_ok=True)
                raise
            except TimeoutError:
                output_path.unlink(missing_ok=True)
                raise
            except urllib.error.HTTPError as exc:
                output_path.unlink(missing_ok=True)
                status = exc.code
                exc.close()
                if status in {412, 429}:
                    self._raise_http_error(status, '媒体下载')
                last_error = BilibiliApiError(f'媒体服务器返回 HTTP {status}。')
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                last_error = exc
                continue
        if last_error is not None:
            raise BilibiliApiError('B 站媒体下载失败，已尝试可用的备用地址。') from last_error
        raise BilibiliApiError('B 站 API 下载地址不可用。')

    def _quality_for_height(self, height: int) -> int:
        for min_height, qn in BILIBILI_QN_BY_HEIGHT:
            if height >= min_height:
                return qn
        return 16

    def _bilibili_headers(self, referer: str, *, include_cookie: bool = True) -> dict[str, str]:
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
        cookie_header = self._cookie_header() if include_cookie else None
        if cookie_header:
            headers['Cookie'] = cookie_header
        return headers

    def _cookie_header(self) -> str | None:
        if self.cookie_file is None:
            return None
        try:
            raw = self.cookie_file.read_text(encoding='utf-8-sig')
            if '\t' in raw:
                jar = MozillaCookieJar(str(self.cookie_file))
                jar.load(ignore_discard=True, ignore_expires=False)
                # Respect the cookie domain/path/expiry. Browser exports can
                # contain credentials for many unrelated websites.
                request = urllib.request.Request('https://api.bilibili.com/')
                jar.add_cookie_header(request)
                return request.get_header('Cookie')
            pairs: list[str] = []
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith('#HttpOnly_'):
                    line = line[len('#HttpOnly_'):]
                elif not line or line.startswith('#'):
                    continue
                for pair in line.removeprefix('Cookie:').split(';'):
                    if '=' in pair:
                        pairs.append(pair.strip())
            return '; '.join(pairs) if pairs else None
        except Exception as exc:
            print(f'[WARN] Failed to read Bilibili cookie header: {type(exc).__name__}')
            return None

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
                timeout=self._remaining_timeout(max(60, min(int(video.duration_seconds * 3), 600))),
            )
        except (TimeoutError, subprocess.TimeoutExpired):
            raise TimeoutError('B 站视频压缩超时。') from None
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
            part_title=video.part_title,
        )
