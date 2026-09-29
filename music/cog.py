# cogs/music/music.py
import discord
from discord import app_commands
from discord.ext import commands

import os, time, asyncio, re, json, shutil, random, hashlib
import urllib.parse
import urllib.request
import copy
import tempfile
from .audio import FALLBACK_SEARCH_PREFIXES, AudioPipeline, public_http_url, safe_error
from .lyrics import parse_lrc, next_refresh_delay
from .search import search_songs
from .access import is_voice_channel, panel_destination
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from .ui import BaseView, MusicInterface, SearchResultView, DEFAULT_COVER_PATH, DEFAULT_COVER_FILENAME
from .auth import setup_async
try:
    from ..config import config
except ImportError:  # Top-level extension loading on Linux deployments.
    from config import config

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


MAX_DIRECT_AUDIO_BYTES = 200 * 1024 * 1024


class Music(commands.Cog):
    # Defaults, overridable through MUSIC_* environment variables in __init__.
    cache_max_age_seconds = 30 * 86400
    cache_max_bytes = 20 * 1024 ** 3
    preload_depth = 3
    fallback_sources = ("bilibili", "youtube")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.loop = None
        self.queues = {}
        self.agent_locks = {}
        self.download_locks = {}
        self.download_jobs = {}
        self.download_failures = {}
        self.download_slots = asyncio.Semaphore(4)
        # Background prefetch never holds every download slot, so a song
        # someone is waiting for can always start downloading.
        self.prefetch_slots = asyncio.Semaphore(2)
        self.prefetch_keys = set()
        self.voice_locks = {}
        self.background_tasks = set()
        self.lyric_cache = {}
        self.lyric_jobs = {}
        self.lyric_slots = asyncio.Semaphore(4)
        self.qq_audio_blocked_until = 0.0
        self.max_queue_length = 200
        self.music_logged_in = False  # QQ ?????????????????????????
        self.cache_root = Path(__file__).resolve().parents[1] / "music_cache"
        self.cache_max_age_seconds = max(3600, self._env_hours("MUSIC_CACHE_MAX_AGE_HOURS", 720.0))
        self.cache_max_bytes = max(256, self._env_int("MUSIC_CACHE_MAX_MB", 20480)) * 1024 * 1024
        self.preload_depth = max(1, min(10, self._env_int("MUSIC_PRELOAD_DEPTH", 3)))
        self.fallback_sources = self._resolve_fallback_sources()
        self.qqmusic_cookie_file = Path(
            os.getenv("QQMUSIC_COOKIE_FILE", "").strip()
            or (Path(__file__).resolve().parents[1] / "config" / "credentials" / "qqmusic_cookie.txt")
        )
        self.ffmpeg_executable = self._resolve_ffmpeg_executable()
        self.audio = AudioPipeline(self.ffmpeg_executable, max_bytes=MAX_DIRECT_AUDIO_BYTES)
        self.qqmusic_cookie_source = "none"
        self.qqmusic_cookie = self._load_qqmusic_cookie()
        self.qqmusic_quality = os.getenv("QQMUSIC_QUALITY", "auto").strip().lower()
        self.qqmusic_guid = self._resolve_qqmusic_guid()
        self.qqmusic_cookie_map = self._parse_cookie_map(self.qqmusic_cookie)
        self.qqmusic_uin = self._extract_cookie_uin(self.qqmusic_cookie_map)
        self.qqmusic_cookie_expire_at = self._extract_cookie_expire_at(
            self.qqmusic_cookie_map
        )
        self.qqmusic_auto_refresh = self._env_flag("QQMUSIC_AUTO_REFRESH", True)
        self.qqmusic_refresh_check_seconds = max(
            300, self._env_int("QQMUSIC_REFRESH_CHECK_SECONDS", 3600)
        )
        self.qqmusic_refresh_max_age_seconds = max(
            0, self._env_hours("QQMUSIC_REFRESH_MAX_AGE_HOURS", 24.0)
        )
        self.qqmusic_refresh_expire_margin_seconds = max(
            0, self._env_hours("QQMUSIC_REFRESH_EXPIRE_MARGIN_HOURS", 24.0)
        )
        self.qqmusic_cookie_notify_dm = self._env_flag(
            "QQMUSIC_COOKIE_NOTIFY_DM", True
        )
        self.qqmusic_cookie_notify_user_id = self._resolve_qqmusic_notify_user_id()
        self.qqmusic_cookie_notify_expire_seconds = max(
            0, self._env_hours("QQMUSIC_COOKIE_NOTIFY_EXPIRE_HOURS", 72.0)
        )
        self.qqmusic_cookie_notify_cooldown_seconds = max(
            300, self._env_hours("QQMUSIC_COOKIE_NOTIFY_COOLDOWN_HOURS", 12.0)
        )
        self.qqmusic_cookie_notify_last_sent_at = {}
        self.qqmusic_refresh_task = None
        self.qqmusic_refresh_lock = asyncio.Lock()
        self.qqmusic_last_refresh_attempt_at = 0.0
        self.voice_reconnect_after = {}
        self.qq_download_403_count = 0
        if not self.cache_root.exists():
            self.cache_root.mkdir(parents=True, exist_ok=True)
        self.cleaner_task = None

        if self.qqmusic_cookie:
            cookie_source = (
                f"cookie file {self.qqmusic_cookie_file}"
                if self.qqmusic_cookie_file.is_file()
                else "QQMUSIC_COOKIE env"
            )
            print(
                f"[INFO] QQ Music cookie loaded from {cookie_source}; official audio will be preferred"
            )
            self._log_cookie_health()
        else:
            print(
                "[WARN] QQ Music cookie is not configured; guest mode and fallback sources will be used"
            )

    async def cog_load(self):
        self.loop = asyncio.get_running_loop()
        if self.cleaner_task is None or self.cleaner_task.done():
            self.cleaner_task = asyncio.create_task(self._periodic_cache_cleaner())
        if (
            self.qqmusic_auto_refresh
            and self.qqmusic_cookie
            and (self.qqmusic_refresh_task is None or self.qqmusic_refresh_task.done())
        ):
            self.qqmusic_refresh_task = asyncio.create_task(
                self._periodic_qqmusic_cookie_refresher()
            )

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)

        def done(finished):
            self.background_tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                print(f"[WARN] Music task failed: {safe_error(finished.exception())}")

        task.add_done_callback(done)
        return task

    async def cog_unload(self):
        tasks = set(self.background_tasks) | set(self.download_jobs.values())
        for data in self.queues.values():
            data["stopping"] = True
            data["voice_generation"] = data.get("voice_generation", 0) + 1
            tasks.update(t for t in (data.get("progress_task"), data.get("preload_task"),
                                     data.get("play_task")) if t is not None)
        tasks.update(t for t in (self.cleaner_task, self.qqmusic_refresh_task) if t is not None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for guild_id in self.queues:
            guild = self.bot.get_guild(guild_id)
            if guild and guild.voice_client:
                await guild.voice_client.disconnect(force=True)
        await self.audio.close()

    @commands.Cog.listener()
    async def on_ready(self):
        """Handle async music setup on bot ready"""
        if not self.music_logged_in:
            self.music_logged_in = await setup_async(
                self.bot, getattr(config, "OWNER_ID", 0)
            )

    def _clean_audio_cache(self):
        protected = set(self.download_locks)
        for data in self.queues.values():
            for song in [data.get("current"), *data.get("queue", [])]:
                if song:
                    protected.add(self._cache_key(song))
                    if song.get("local_path"):
                        protected.add(Path(song["local_path"]).stem)
        files = []
        for path in self.cache_root.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix in {".mp3", ".opus", ".tmp"}:
                try:
                    files.append((path, path.stat()))
                except OSError:
                    pass
        total = sum(info.st_size for _, info in files)
        removed = 0
        for path, info in sorted(files, key=lambda entry: entry[1].st_mtime):
            if path.stem in protected:
                continue
            # Cache hits refresh mtime, so this evicts least-recently-played first.
            if time.time() - info.st_mtime <= self.cache_max_age_seconds and total <= self.cache_max_bytes:
                continue
            try:
                path.unlink(missing_ok=True)
                total -= info.st_size
                removed += 1
            except OSError:
                pass
        return removed

    async def _periodic_cache_cleaner(self):
        await self.bot.wait_until_ready()
        try:
            while not self.bot.is_closed():
                await asyncio.sleep(600)
                # Snapshot and deletion stay on the event loop: no race with a new
                # playback acquiring the same cache between protection and unlink.
                removed = self._clean_audio_cache()
                if removed:
                    print(f"[INFO] Music cache cleaned: files={removed}")
        except asyncio.CancelledError:
            return

    def _resolve_ffmpeg_executable(self):
        ffmpeg_executable = shutil.which("ffmpeg")
        if ffmpeg_executable:
            return ffmpeg_executable

        if imageio_ffmpeg is None:
            return None

        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    def _load_qqmusic_cookie(self) -> str:
        cookie_from_env = os.getenv("QQMUSIC_COOKIE", "").strip()
        if cookie_from_env:
            self.qqmusic_cookie_source = "env"
            return cookie_from_env

        try:
            if self.qqmusic_cookie_file.is_file():
                self.qqmusic_cookie_source = "file"
                return self.qqmusic_cookie_file.read_text(encoding="utf-8-sig").strip()
        except Exception as exc:
            print(
                f"[WARN] failed to read QQ Music cookie file {self.qqmusic_cookie_file}: {exc}"
            )
        return ""

    def _resolve_qqmusic_guid(self) -> str:
        raw_guid = os.getenv("QQMUSIC_GUID", "").strip()
        if raw_guid and raw_guid != "10000":
            return raw_guid
        return str(random.randint(10**9, 10**10 - 1))

    def _extract_cookie_expire_at(self, cookie_map: dict[str, str]) -> int | None:
        return self._extract_cookie_timestamp(cookie_map, "psrf_access_token_expiresAt")

    def _extract_cookie_timestamp(
        self, cookie_map: dict[str, str], key: str
    ) -> int | None:
        raw_value = cookie_map.get(key, "").strip()
        if not raw_value:
            return None
        try:
            return int(raw_value)
        except ValueError:
            return None

    def _env_flag(self, key: str, default: bool) -> bool:
        raw_value = os.getenv(key)
        if raw_value is None:
            return default
        return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}

    def _env_int(self, key: str, default: int) -> int:
        raw_value = os.getenv(key, "").strip()
        if not raw_value:
            return default
        try:
            return int(raw_value)
        except ValueError:
            print(f"[WARN] {key} must be an integer; using {default}")
            return default

    def _resolve_fallback_sources(self) -> tuple[str, ...]:
        raw = os.getenv("MUSIC_FALLBACK_SOURCES")
        if raw is None:
            return type(self).fallback_sources
        sources = []
        for name in raw.replace(",", " ").lower().split():
            if name not in FALLBACK_SEARCH_PREFIXES:
                print(f"[WARN] MUSIC_FALLBACK_SOURCES ignores unknown source {name[:40]!r}")
            elif name not in sources:
                sources.append(name)
        return tuple(sources)

    def _env_hours(self, key: str, default: float) -> int:
        raw_value = os.getenv(key, "").strip()
        if not raw_value:
            hours = default
        else:
            try:
                hours = float(raw_value)
            except ValueError:
                print(f"[WARN] {key} must be a number of hours; using {default}")
                hours = default
        return int(hours * 3600)

    def _resolve_qqmusic_notify_user_id(self) -> int:
        for key in ("QQMUSIC_COOKIE_NOTIFY_USER_ID", "ATRI_OWNER_DISCORD_ID"):
            raw_value = os.getenv(key, "").strip()
            if not raw_value:
                continue
            try:
                return int(raw_value)
            except ValueError:
                print(f"[WARN] {key} must be a Discord user id; ignoring it")

        return 0

    def _log_cookie_health(self):
        if not self.qqmusic_cookie:
            return

        uin_text = self.qqmusic_uin or "unknown"
        print(f"[INFO] QQ Music login uin detected: {uin_text}")
        print(f"[INFO] QQ Music guid in use: {self.qqmusic_guid}")

        create_at = self._extract_cookie_timestamp(
            self.qqmusic_cookie_map, "psrf_musickey_createtime"
        )
        if create_at is not None:
            age_hours = max(0, int(time.time()) - create_at) / 3600
            refresh_hours = self.qqmusic_refresh_max_age_seconds / 3600
            print(
                f"[INFO] QQ Music musickey age is {age_hours:.1f}h; "
                f"auto refresh threshold is {refresh_hours:.1f}h"
            )

        if self.qqmusic_cookie_expire_at is None:
            print(
                "[WARN] QQ Music cookie does not expose psrf_access_token_expiresAt; expiry cannot be predicted"
            )
            return

        expire_at = datetime.fromtimestamp(
            self.qqmusic_cookie_expire_at, tz=timezone.utc
        ).astimezone()
        remaining_seconds = self.qqmusic_cookie_expire_at - int(time.time())
        if remaining_seconds <= 0:
            print(
                f"[WARN] QQ Music cookie appears expired at {expire_at.strftime('%Y-%m-%d %H:%M:%S %z')}; please refresh it"
            )
            return

        remaining_days = remaining_seconds / 86400
        level = "WARN" if remaining_days <= 3 else "INFO"
        print(
            f"[{level}] QQ Music cookie expires at {expire_at.strftime('%Y-%m-%d %H:%M:%S %z')} ({remaining_days:.1f} days remaining)"
        )

    def _parse_cookie_map(self, cookie_header: str) -> dict[str, str]:
        if not cookie_header:
            return {}

        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return {}

        return {key: morsel.value for key, morsel in cookie.items()}

    def _extract_cookie_uin(self, cookie_map: dict[str, str]) -> str:
        for key in ("uin", "wxuin"):
            value = cookie_map.get(key, "")
            if not value:
                continue
            match = re.search(r"(\d+)", value)
            if match:
                return match.group(1)
        return "0"

    def _qq_login_uin(self) -> str:
        return self.qqmusic_uin or "0"

    def _qq_cookie_value(self, *keys: str) -> str:
        for key in keys:
            value = self.qqmusic_cookie_map.get(key, "").strip()
            if value:
                return value
        return ""

    async def reload_qqmusic_cookie(self) -> bool:
        """Reload the protected cookie file without restarting the bot."""

        async with self.qqmusic_refresh_lock:
            self.qqmusic_cookie_source = "none"
            self.qqmusic_cookie = self._load_qqmusic_cookie()
            self.qqmusic_cookie_map = self._parse_cookie_map(self.qqmusic_cookie)
            self.qqmusic_uin = self._extract_cookie_uin(self.qqmusic_cookie_map)
            self.qqmusic_cookie_expire_at = self._extract_cookie_expire_at(
                self.qqmusic_cookie_map
            )
        if (
            self.qqmusic_auto_refresh
            and self.qqmusic_cookie
            and (self.qqmusic_refresh_task is None or self.qqmusic_refresh_task.done())
        ):
            self.qqmusic_refresh_task = asyncio.create_task(
                self._periodic_qqmusic_cookie_refresher()
            )
        print(
            "[INFO] QQ Music credential hot-reloaded: "
            f"configured={bool(self.qqmusic_cookie)}"
        )
        self.download_failures.clear()
        self.qq_audio_blocked_until = 0.0
        return bool(self.qqmusic_cookie)

    def _serialize_cookie_map(self, cookie_map: dict[str, str]) -> str:
        return "; ".join(f"{key}={value}" for key, value in cookie_map.items())

    def _apply_refreshed_qqmusic_cookie(self, data: dict) -> bool:
        new_musickey = str(data.get("musickey") or data.get("music_key") or "").strip()
        if not new_musickey:
            return False

        now = int(time.time())
        login_mode = str(
            data.get("loginMode")
            or data.get("login_mode")
            or self.qqmusic_cookie_map.get("tmeLoginType")
            or "2"
        ).strip()
        new_map = dict(self.qqmusic_cookie_map)
        new_map["qqmusic_key"] = new_musickey
        new_map["qm_keyst"] = new_musickey
        new_map["psrf_musickey_createtime"] = str(
            self._coerce_timestamp(
                data.get("musickey_create_time")
                or data.get("musickeyCreateTime")
                or data.get("musickey_createtime")
            )
            or now
        )

        music_id = str(data.get("musicid") or data.get("uin") or "").strip()
        if music_id:
            if login_mode == "1":
                new_map["wxuin"] = music_id
            else:
                new_map["uin"] = music_id

        openid = str(data.get("openid") or "").strip()
        if openid:
            new_map["wxopenid" if login_mode == "1" else "psrf_qqopenid"] = openid

        access_token = str(
            data.get("access_token") or data.get("accessToken") or ""
        ).strip()
        if access_token:
            key = "wxaccess_token" if login_mode == "1" else "psrf_qqaccess_token"
            new_map[key] = access_token

        refresh_token = str(
            data.get("refresh_token") or data.get("refreshToken") or ""
        ).strip()
        if refresh_token:
            new_map[
                "wxrefresh_token" if login_mode == "1" else "psrf_qqrefresh_token"
            ] = refresh_token

        refresh_key = str(
            data.get("refresh_key") or data.get("refreshKey") or ""
        ).strip()
        if refresh_key:
            new_map["refresh_key"] = refresh_key

        expires_at = self._coerce_timestamp(
            data.get("psrf_access_token_expiresAt")
            or data.get("access_token_expires_at")
            or data.get("accessTokenExpiresAt")
            or data.get("expires_at")
            or data.get("expire_at")
        )
        if expires_at is None:
            expires_in = self._coerce_int(
                data.get("expires_in") or data.get("expire_in") or data.get("expired_in")
            )
            if expires_in and expires_in > 0:
                expires_at = expires_in if expires_in > 10**9 else now + expires_in
        if expires_at:
            new_map["psrf_access_token_expiresAt"] = str(expires_at)

        self.qqmusic_cookie_map = new_map
        self.qqmusic_cookie = self._serialize_cookie_map(new_map)
        self.qqmusic_uin = self._extract_cookie_uin(new_map)
        self.qqmusic_cookie_expire_at = self._extract_cookie_expire_at(new_map)
        return True

    def _coerce_int(self, value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _coerce_timestamp(self, value) -> int | None:
        timestamp = self._coerce_int(value)
        if timestamp is None:
            return None
        if timestamp > 10**12:
            timestamp //= 1000
        return timestamp

    def _write_qqmusic_cookie_file(self) -> bool:
        if not self.qqmusic_cookie:
            return False
        try:
            self.qqmusic_cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self.qqmusic_cookie_file.write_text(
                self.qqmusic_cookie + "\n", encoding="utf-8"
            )
            if self.qqmusic_cookie_source == "env":
                print(
                    "[WARN] QQMUSIC_COOKIE env is set; refreshed cookie is active now "
                    "and was written to the cookie file, but restart will still prefer "
                    "QQMUSIC_COOKIE unless you remove it from .env"
                )
            else:
                self.qqmusic_cookie_source = "file"
            return True
        except Exception as exc:
            print(f"[WARN] failed to write QQ Music cookie file: {exc}")
            return False

    def _qqmusic_cookie_needs_refresh(self) -> bool:
        if not self.qqmusic_cookie:
            return False

        now = int(time.time())
        create_at = self._extract_cookie_timestamp(
            self.qqmusic_cookie_map, "psrf_musickey_createtime"
        )
        if (
            self.qqmusic_refresh_max_age_seconds > 0
            and create_at is not None
            and now - create_at >= self.qqmusic_refresh_max_age_seconds
        ):
            return True

        if (
            self.qqmusic_cookie_expire_at is not None
            and self.qqmusic_cookie_expire_at - now
            <= self.qqmusic_refresh_expire_margin_seconds
        ):
            return True

        return False

    def _qqmusic_cookie_status_message(self, prefix: str) -> str:
        details = [prefix, f"Cookie file: {self.qqmusic_cookie_file}"]
        if self.qqmusic_uin:
            details.append(f"Login uin: {self.qqmusic_uin}")

        create_at = self._extract_cookie_timestamp(
            self.qqmusic_cookie_map, "psrf_musickey_createtime"
        )
        if create_at is not None:
            age_hours = max(0, int(time.time()) - create_at) / 3600
            details.append(f"Musickey age: {age_hours:.1f}h")

        if self.qqmusic_cookie_expire_at is None:
            details.append("Expires: unknown")
        else:
            expire_at = datetime.fromtimestamp(
                self.qqmusic_cookie_expire_at, tz=timezone.utc
            ).astimezone()
            remaining_seconds = self.qqmusic_cookie_expire_at - int(time.time())
            details.append(f"Expires: {expire_at.strftime('%Y-%m-%d %H:%M:%S %z')}")
            details.append(f"Remaining: {remaining_seconds / 86400:.1f} days")

        if self.qqmusic_cookie_source == "env":
            details.append(
                "QQMUSIC_COOKIE is set in .env; restart will prefer that value unless you clear it."
            )

        details.append(
            "Please refresh config/credentials/qqmusic_cookie.txt if playback keeps failing."
        )
        return "\n".join(details)

    def _qqmusic_cookie_expiry_notification(self) -> tuple[str, str] | None:
        if not self.qqmusic_cookie:
            return (
                "missing_cookie",
                self._qqmusic_cookie_status_message(
                    "QQ Music cookie is not configured. Official audio may fall back or fail."
                ),
            )

        if self.qqmusic_cookie_expire_at is None:
            return (
                "unknown_expiry",
                self._qqmusic_cookie_status_message(
                    "QQ Music cookie expiry cannot be predicted."
                ),
            )

        remaining_seconds = self.qqmusic_cookie_expire_at - int(time.time())
        if remaining_seconds <= 0:
            return (
                "expired",
                self._qqmusic_cookie_status_message(
                    "QQ Music cookie appears expired."
                ),
            )

        if remaining_seconds <= self.qqmusic_cookie_notify_expire_seconds:
            return (
                "near_expiry",
                self._qqmusic_cookie_status_message(
                    "QQ Music cookie is close to expiry."
                ),
            )

        return None

    async def _send_qqmusic_cookie_dm(
        self, kind: str, message: str, *, force: bool = False
    ) -> bool:
        if not self.qqmusic_cookie_notify_dm or not self.qqmusic_cookie_notify_user_id:
            return False

        now = time.time()
        last_sent_at = self.qqmusic_cookie_notify_last_sent_at.get(kind, 0)
        if (
            not force
            and now - last_sent_at < self.qqmusic_cookie_notify_cooldown_seconds
        ):
            return False

        try:
            user = self.bot.get_user(self.qqmusic_cookie_notify_user_id)
            if user is None:
                user = await self.bot.fetch_user(self.qqmusic_cookie_notify_user_id)
            await user.send(message)
            self.qqmusic_cookie_notify_last_sent_at[kind] = now
            print(f"[INFO] QQ Music cookie notification sent by DM: {kind}")
            return True
        except discord.Forbidden:
            print(
                "[WARN] Could not DM QQ Music cookie notification; "
                "the target user may have DMs disabled"
            )
        except discord.HTTPException as exc:
            print(f"[WARN] Failed to DM QQ Music cookie notification: {exc}")
        return False

    async def _notify_qqmusic_cookie_expiry_if_needed(self) -> None:
        notification = self._qqmusic_cookie_expiry_notification()
        if notification is None:
            return
        kind, message = notification
        await self._send_qqmusic_cookie_dm(kind, message)

    def _build_qqmusic_refresh_payload(self) -> dict | None:
        musickey = self._qq_cookie_value("qqmusic_key", "qm_keyst")
        login_type = self._qq_cookie_value("tmeLoginType") or "2"
        if login_type == "1":
            refresh_token = self._qq_cookie_value("wxrefresh_token")
            openid = self._qq_cookie_value("wxopenid")
            access_token = self._qq_cookie_value("wxaccess_token", "psrf_wxaccess_token")
            music_id = self._qq_cookie_value("wxuin", "uin")
        else:
            refresh_token = self._qq_cookie_value(
                "psrf_qqrefresh_token", "qqrefresh_token"
            )
            openid = self._qq_cookie_value("psrf_qqopenid")
            access_token = self._qq_cookie_value("psrf_qqaccess_token")
            music_id = self._qq_cookie_value("uin")

        if not musickey or not refresh_token or not music_id:
            print(
                "[WARN] QQ Music cookie cannot be auto-refreshed; missing "
                "qqmusic_key/qm_keyst, refresh token, or uin"
            )
            return None

        refresh_param = {
            "refresh_token": refresh_token,
            "expired_in": 0,
            "musicid": int(music_id) if music_id.isdigit() else music_id,
            "musickey": musickey,
            "loginMode": int(login_type) if login_type.isdigit() else login_type,
        }
        if openid:
            refresh_param["openid"] = openid
        if access_token:
            refresh_param["access_token"] = access_token

        refresh_key = self._qq_cookie_value("refresh_key")
        if refresh_key:
            refresh_param["refresh_key"] = refresh_key

        return {
            "comm": {
                "ct": 24,
                "cv": 0,
                "uin": music_id,
                "format": "json",
                "platform": "yqq.json",
            },
            "req": {
                "module": "music.login.LoginServer",
                "method": "Login",
                "param": refresh_param,
            },
        }

    def _refresh_qqmusic_cookie_sync(self, reason: str) -> bool:
        payload = self._build_qqmusic_refresh_payload()
        if payload is None:
            return False

        try:
            result = self._qq_request_json(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                method="POST",
                data=payload,
            )
        except Exception as exc:
            print(f"[WARN] QQ Music cookie refresh failed during {reason}: {exc}")
            return False

        req_result = result.get("req", {}) if isinstance(result, dict) else {}
        code = req_result.get("code")
        data = req_result.get("data") or {}
        if code != 0 or not isinstance(data, dict):
            message = req_result.get("message") or req_result.get("msg") or "unknown"
            print(
                f"[WARN] QQ Music cookie refresh rejected during {reason}: "
                f"code={code}, message={message}"
            )
            return False

        if not self._apply_refreshed_qqmusic_cookie(data):
            print(
                f"[WARN] QQ Music cookie refresh during {reason} did not return musickey"
            )
            return False

        saved = self._write_qqmusic_cookie_file()
        save_text = "saved to cookie file" if saved else "active in memory only"
        print(f"[OK] QQ Music cookie refreshed during {reason}; {save_text}")
        self._log_cookie_health()
        return True

    async def _refresh_qqmusic_cookie_if_needed(
        self, *, force: bool = False, reason: str = "scheduled"
    ) -> bool:
        if not self.qqmusic_auto_refresh or not self.qqmusic_cookie:
            return False

        if not force and not self._qqmusic_cookie_needs_refresh():
            return False

        async with self.qqmusic_refresh_lock:
            if not force and not self._qqmusic_cookie_needs_refresh():
                return False

            now = time.time()
            minimum_attempt_gap = 60 if force else 300
            if now - self.qqmusic_last_refresh_attempt_at < minimum_attempt_gap:
                return False
            self.qqmusic_last_refresh_attempt_at = now

            return await asyncio.to_thread(self._refresh_qqmusic_cookie_sync, reason)

    async def _periodic_qqmusic_cookie_refresher(self):
        await self.bot.wait_until_ready()
        try:
            needs_refresh = self._qqmusic_cookie_needs_refresh()
            refreshed = await self._refresh_qqmusic_cookie_if_needed(reason="startup")
            if needs_refresh and not refreshed:
                await self._send_qqmusic_cookie_dm(
                    "refresh_failed",
                    self._qqmusic_cookie_status_message(
                        "QQ Music cookie automatic refresh failed during startup."
                    ),
                )
            await self._notify_qqmusic_cookie_expiry_if_needed()

            while not self.bot.is_closed():
                await asyncio.sleep(self.qqmusic_refresh_check_seconds)
                needs_refresh = self._qqmusic_cookie_needs_refresh()
                refreshed = await self._refresh_qqmusic_cookie_if_needed(
                    reason="scheduled"
                )
                if needs_refresh and not refreshed:
                    await self._send_qqmusic_cookie_dm(
                        "refresh_failed",
                        self._qqmusic_cookie_status_message(
                            "QQ Music cookie automatic refresh failed."
                        ),
                    )
                await self._notify_qqmusic_cookie_expiry_if_needed()
        except asyncio.CancelledError:
            return

    def _qq_request_headers(self, headers: dict | None = None) -> dict:
        request_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Referer": "https://y.qq.com/",
            "Origin": "https://y.qq.com",
        }
        if self.qqmusic_cookie:
            request_headers["Cookie"] = self.qqmusic_cookie
        if headers:
            request_headers.update(headers)
        return request_headers

    def _qq_quality_candidates(self, song: dict) -> list[tuple[str, str]]:
        song_mid = song.get("mid")
        media_mid = song.get("media_mid") or song_mid
        if not song_mid or not media_mid:
            return []

        preferred = self.qqmusic_quality
        quality_map = {
            "flac": [("F000", ".flac", media_mid)],
            "320": [("M800", ".mp3", media_mid)],
            "128": [("M500", ".mp3", media_mid)],
            "m4a": [("C400", ".m4a", song_mid)],
            "auto": [
                ("F000", ".flac", media_mid),
                ("M800", ".mp3", media_mid),
                ("M500", ".mp3", media_mid),
                ("C400", ".m4a", song_mid),
            ],
        }
        selected = quality_map.get(preferred, quality_map["auto"])
        excluded = song.get("_qq_failed_files", set()) | song.get("_qq_unavailable_files", set())
        return [(f"{prefix}{target}{suffix}", suffix) for prefix, suffix, target in selected
                if f"{prefix}{target}{suffix}" not in excluded]

    def _build_fallback_query(self, song: dict) -> str:
        artists = ", ".join(
            artist.get("name", "")
            for artist in song.get("ar", [])
            if isinstance(artist, dict) and artist.get("name")
        )
        return " ".join(part for part in [song.get("name"), artists] if part)

    def _fallback_cookie_file(self, source: str, directory: Path) -> Path | None:
        """Give yt-dlp a private copy of a Netscape Bilibili cookie file, if any.

        yt-dlp writes its cookie jar back on exit; the protected credential
        must never be rewritten by a search subprocess.
        """
        raw = os.getenv("BILIBILI_COOKIE_FILE", "").strip()
        if source != "bilibili" or not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        try:
            with path.open(encoding="utf-8-sig") as stream:
                if "HTTP Cookie File" not in stream.readline():
                    return None  # A raw Cookie header is not something yt-dlp can load.
            copy_path = directory / "fallback-cookies.txt"
            shutil.copyfile(path, copy_path)
            copy_path.chmod(0o600)
            return copy_path
        except OSError:
            return None

    def _normalize_direct_audio_url(self, raw_url: str) -> str | None:
        url = raw_url.strip()
        if not url:
            return None
        try:
            return public_http_url(url)
        except ValueError:
            return None

    def _direct_audio_name_from_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        basename = os.path.basename(urllib.parse.unquote(parsed.path)).strip()
        if basename:
            stem, _ext = os.path.splitext(basename)
            return stem[:80] or basename[:80]
        return parsed.netloc[:80] or "direct-audio"

    def _direct_audio_song(self, url: str, requester: str) -> dict:
        song_id = "direct_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        name = self._direct_audio_name_from_url(url)
        return {
            "id": song_id,
            "mid": None,
            "media_mid": None,
            "name": name,
            "ar": [{"name": "Direct Link"}],
            "al": {"name": urllib.parse.urlparse(url).netloc or "Direct Link", "picUrl": ""},
            "dt": 0,
            "requester": requester,
            "source": "direct_url",
            "direct_url": url,
        }

    def _get_or_create_queue(self, guild_id: int):
        if guild_id not in self.queues:
            self.queues[guild_id] = {
                "current": None,
                "queue": [],
                "message": None,
                "view": MusicInterface(self.bot, guild_id),
                "start_time": None,
                "paused_elapsed": 0,
                "progress_task": None,
                "channel": None,
                "error_count": 0,
                "play_mode": "sequential",
                "priority_next": None,
                "voice_generation": 0,
                "stopping": False,
                "ui_lock": asyncio.Lock(),
                "play_task": None,
                "preload_task": None,
                "agent_phase": "idle",
                "playback_failed": False,
                "lyrics_enabled": True,
                "lyrics_task": None,
                "agent_last_error": None,
                "agent_state_version": 0,
                "agent_last_transition": None,
            }
        else:
            self.queues[guild_id].setdefault("play_mode", "sequential")
            self.queues[guild_id].setdefault("priority_next", None)
            self.queues[guild_id].setdefault("voice_generation", 0)
            self.queues[guild_id].setdefault("stopping", False)
            self.queues[guild_id].setdefault("agent_phase", "idle")
            self.queues[guild_id].setdefault("agent_last_error", None)
            self.queues[guild_id].setdefault("agent_state_version", 0)
            self.queues[guild_id].setdefault("agent_last_transition", None)
        return self.queues[guild_id]

    def _priority_next_index(self, queue_list, priority_song):
        if priority_song is None:
            return None

        for index, queued_song in enumerate(queue_list):
            if queued_song is priority_song:
                return index
        return None

    def _select_next_song_index(self, queue_data: dict):
        queue_list = queue_data["queue"]
        if not queue_list:
            queue_data["priority_next"] = None
            queue_data["shuffle_next"] = None
            return None
        priority = self._priority_next_index(queue_list, queue_data.get("priority_next"))
        if priority is not None:
            return priority
        queue_data["priority_next"] = None
        if queue_data.get("play_mode") == "shuffle":
            selected = self._priority_next_index(queue_list, queue_data.get("shuffle_next"))
            if selected is None:
                selected = random.randrange(len(queue_list))
                queue_data["shuffle_next"] = queue_list[selected]
            return selected
        return 0

    def _preview_next_song(self, guild_id: int):
        data = self._get_or_create_queue(guild_id)
        index = self._select_next_song_index(data)
        return data["queue"][index] if index is not None else None

    def toggle_play_mode(self, guild_id: int):
        queue_data = self._get_or_create_queue(guild_id)
        current_mode = queue_data.get("play_mode", "sequential")
        queue_data["play_mode"] = (
            "shuffle" if current_mode == "sequential" else "sequential"
        )
        queue_data["shuffle_next"] = None
        return queue_data["play_mode"]

    # --- UI 更新逻辑 ---
    def _panel_uploads(self, view, message=None):
        """Attach the default image once per panel, not on every lyric refresh."""
        if not getattr(view, "uses_default_cover", False):
            return []
        attachments = getattr(message, "attachments", ())
        if any(item.filename == DEFAULT_COVER_FILENAME for item in attachments):
            return []
        return [discord.File(DEFAULT_COVER_PATH, filename=DEFAULT_COVER_FILENAME)]

    async def _send_player_panel(self, channel, view):
        if not is_voice_channel(channel):
            raise ValueError('音乐面板只能发送到语音频道的文字聊天区。')
        uploads = self._panel_uploads(view)
        try:
            return await channel.send(view=view, files=uploads, allowed_mentions=discord.AllowedMentions.none())
        finally:
            for upload in uploads:
                upload.close()

    async def _edit_player_panel(self, message, view):
        uploads = self._panel_uploads(view, message)
        kwargs = {}
        if uploads:
            kwargs["attachments"] = list(getattr(message, "attachments", ())) + uploads
        try:
            return await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        finally:
            for upload in uploads:
                upload.close()

    async def update_player_ui(self, guild_id: int):
        data = self.queues.get(guild_id)
        if not data:
            return
        async with data.setdefault("ui_lock", asyncio.Lock()):
            target = panel_destination(self.bot.get_guild(guild_id), data)
            if target is None:
                return
            data['channel'] = target
            previous_message = data.get('message')
            if previous_message and getattr(getattr(previous_message, 'channel', None), 'id', None) != target.id:
                # Only this tracked, bot-created player card is retired; other
                # channel messages are untouched. Old text-channel buttons are
                # also rejected by BaseView even if Discord refuses deletion.
                await self._delete_panel_message(guild_id)
            view = data["view"]
            view.update_container()
            message = data.get("message")
            if message:
                try:
                    edited = await self._edit_player_panel(message, view)
                    # discord.py returns a NEW Message; keep its attachment list
                    # so the next lyric refresh does not upload the image again.
                    if isinstance(edited, discord.Message):
                        data["message"] = edited
                    return
                except discord.NotFound:
                    data["message"] = None
                except discord.HTTPException as exc:
                    # Rate limits/transient failures do not mean the panel was deleted.
                    print(f"[WARN] Music panel edit deferred: status={exc.status}")
                    return
            if data.get("channel"):
                try:
                    data["message"] = await self._send_player_panel(data["channel"], view)
                except discord.HTTPException as exc:
                    print(f"[WARN] Music panel send failed: status={exc.status}")

    async def _delete_panel_message(self, guild_id: int):
        if guild_id not in self.queues:
            return

        data = self.queues[guild_id]
        message = data.get("message")
        data["message"] = None

        if not message:
            return

        try:
            await message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    # --- QQ 音乐 API 封装 ---
    def _advance_voice_generation(self, guild_id: int) -> int:
        queue_data = self._get_or_create_queue(guild_id)
        queue_data["voice_generation"] = queue_data.get("voice_generation", 0) + 1
        return queue_data["voice_generation"]

    async def _respect_voice_reconnect_delay(self, guild_id: int) -> None:
        ready_at = self.voice_reconnect_after.get(guild_id)
        if ready_at is None:
            return

        delay = ready_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.voice_reconnect_after.pop(guild_id, None)

    def _drop_stale_voice_client(self, vc) -> None:
        if not vc or vc.is_connected():
            return
        try:
            vc.cleanup()
        except Exception:
            pass

    async def _ensure_voice_client_for(self, guild, member, *, expected_channel_id=None):
        if not guild or not getattr(getattr(member, "voice", None), "channel", None):
            return None
        async with self.voice_locks.setdefault(guild.id, asyncio.Lock()):
            # Member location may have changed while searching/waiting for a lock.
            target = getattr(getattr(member, "voice", None), "channel", None)
            if target is None or (expected_channel_id is not None and target.id != expected_channel_id):
                return None
            vc = guild.voice_client
            self._drop_stale_voice_client(vc)
            vc = guild.voice_client
            if vc and vc.is_connected():
                if vc.channel != target:
                    raise RuntimeError("请加入 BOT 当前所在的语音频道后再操作。")
                return vc
            await self._respect_voice_reconnect_delay(guild.id)
            if getattr(getattr(member, "voice", None), "channel", None) != target:
                return None
            return await target.connect(timeout=20, reconnect=True)

    async def _ensure_voice_client(self, interaction: discord.Interaction):
        return await self._ensure_voice_client_for(
            interaction.guild,
            interaction.user,
            expected_channel_id=interaction.channel.id,
        )

    def _handle_track_finished(self, guild_id: int, generation: int, error: Exception | None) -> None:
        data = self.queues.get(guild_id)
        if (not data or data.get("stopping") or data.get("voice_generation") != generation
                or data.get("finished_generation") == generation):
            return
        data["finished_generation"] = generation
        if error:
            self._spawn(self._play_failed(guild_id, data["current"], generation, safe_error(error)))
        else:
            data["error_count"] = 0
            self.play_next(guild_id)

    def _qq_request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        data=None,
        headers: dict | None = None,
        timeout: float = 20,
    ):
        request_headers = self._qq_request_headers(headers)

        payload = None
        if data is not None:
            if not isinstance(data, (bytes, bytearray)):
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            else:
                payload = data
            request_headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(
            url, data=payload, headers=request_headers, method=method
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read(2 * 1024 * 1024 + 1)
            if len(raw_bytes) > 2 * 1024 * 1024:
                raise RuntimeError("QQ Music metadata response is too large")
            response_charset = resp.headers.get_content_charset()

        raw = self._decode_response_text(raw_bytes, response_charset)
        text = raw.strip()
        if text.startswith("callback(") and text.endswith(")"):
            text = text[len("callback(") : -1]
        if text.startswith("MusicJsonCallback(") and text.endswith(")"):
            text = text[len("MusicJsonCallback(") : -1]

        return json.loads(text)

    def _decode_response_text(self, raw_bytes: bytes, charset: str | None = None) -> str:
        encodings = []
        if charset:
            encodings.append(charset)
        encodings.extend(["utf-8-sig", "utf-8", "gb18030", "gbk"])

        tried = set()
        for encoding in encodings:
            if encoding in tried:
                continue
            tried.add(encoding)
            try:
                return raw_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue

        return raw_bytes.decode("utf-8", errors="replace")

    def _normalize_song_data(self, song: dict, requester: str | None = None):
        songmid = song.get("mid") or song.get("songmid") or song.get("id")
        song_id = song.get("id") or song.get("songid") or songmid
        singer_list = song.get("singer") or song.get("singers") or []
        if isinstance(singer_list, str):
            singer_list = [{"name": singer_list}]
        album_info = song.get("album") or {}
        album_mid = (
            album_info.get("mid")
            or album_info.get("albummid")
            or song.get("albummid")
            or ""
        )
        album_name = album_info.get("name") or song.get("albumname") or "未知"
        duration_sec = song.get("interval") or song.get("duration") or 0
        cover = ""
        if album_mid:
            cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg"

        return {
            "id": str(song_id),
            "mid": songmid,
            "media_mid": song.get("file", {}).get("media_mid") or song.get("strMediaMid"),
            "name": (
                song.get("title")
                or song.get("name")
                or song.get("songname")
                or song.get("songorig")
                or "未知歌曲"
            ),
            "ar": [
                {"name": singer.get("name", "未知")}
                for singer in singer_list
                if isinstance(singer, dict)
            ]
            or [{"name": "未知"}],
            "al": {
                "name": album_name,
                "picUrl": cover,
            },
            "dt": int(duration_sec) * 1000,
            "requester": requester,
        }

    def _get_song_detail(self, song_mid: str):
        result = self._qq_request_json(
            "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg?"
            f"songmid={urllib.parse.quote(song_mid)}&tpl=yqq_song_detail&format=json"
        )
        songs = result.get("data") or []
        if songs:
            return songs[0]
        return None

    def _search_song(self, query: str):
        candidates = self._search_song_candidates(query)
        return candidates[0] if candidates else None

    def _search_song_candidates(self, query: str, limit: int = 8):
        try:
            items = search_songs(self._qq_request_json, query, limit=limit)
            candidates = []
            for item in items:
                try:
                    # Full search already provides album/artwork, singers,
                    # duration and media MID. No N+1 detail lookups needed.
                    candidates.append(self._normalize_song_data(item))
                except (TypeError, ValueError, AttributeError):
                    continue
            return candidates
        except Exception as e:
            print(f"[WARN] QQ full song search failed: {type(e).__name__}")
            return []

    def _copy_song_for_queue(self, song: dict, requester: str):
        song_copy = copy.deepcopy(song)
        song_copy["requester"] = requester
        return song_copy

    def _get_song_url(self, song: dict):
        # A preview must not inherit the quality label from a previous attempt.
        song.pop("qq_filename", None)
        try:
            song_mid = song.get("mid")
            if not song_mid:
                return None

            login_uin = self._qq_login_uin()
            for filename, _suffix in self._qq_quality_candidates(song):
                payload = {
                    "comm": {
                        "ct": 24,
                        "cv": 0,
                        "uin": login_uin,
                        "format": "json",
                        "platform": "yqq.json",
                    },
                    "req_0": {
                        "module": "vkey.GetVkeyServer",
                        "method": "CgiGetVkey",
                        "param": {
                            "guid": self.qqmusic_guid,
                            "songmid": [song_mid],
                            "songtype": [0],
                            "uin": login_uin,
                            "loginflag": 1 if self.qqmusic_cookie else 0,
                            "platform": "20",
                            "filename": [filename],
                        },
                    },
                }
                result = self._qq_request_json(
                    "https://u.y.qq.com/cgi-bin/musicu.fcg",
                    method="POST",
                    data=payload,
                )
                info = result.get("req_0", {}).get("data", {}).get("midurlinfo", [])
                purl = info[0].get("purl") if info else ""
                if purl:
                    sip_list = result.get("req_0", {}).get("data", {}).get("sip", [])
                    base = (
                        sip_list[0]
                        if sip_list
                        else "https://isure.stream.qqmusic.qq.com/"
                    )
                    song["qq_filename"] = filename
                    song["qq_audio_base"] = base
                    return urllib.parse.urljoin(base, purl)
                song.setdefault("_qq_unavailable_files", set()).add(filename)

            if song.get("_qq_skip_preview"):
                return None
            detail_result = self._qq_request_json(
                "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg?"
                f"songmid={urllib.parse.quote(song_mid)}&tpl=yqq_song_detail&format=json"
            )
            detail_urls = detail_result.get("url", {})
            preview_url = detail_urls.get(str(song.get("id")))
            if preview_url:
                if preview_url.startswith("//"):
                    return f"https:{preview_url}"
                if preview_url.startswith("http://") or preview_url.startswith("https://"):
                    return preview_url
                return f"https://{preview_url.lstrip('/')}"
        except Exception as e:
            print(f"QQ 音乐获取播放链接失败: {safe_error(e)}")
        return None

    def _extract_collection_id(self, link: str, c_type: str):
        if c_type == "playlist":
            patterns = [
                r"(?:disstid|id)=(\d+)",
                r"/playlist/(\d+)",
                r"/playsquare/(\d+)",
            ]
            fallback = link.strip() if link.strip().isdigit() else None
        else:
            patterns = [
                r"(?:albummid|albumMid|id)=([A-Za-z0-9]+)",
                r"/album(?:Detail)?/([A-Za-z0-9]+)",
            ]
            fallback = link.strip() if re.fullmatch(r"[A-Za-z0-9]+", link.strip()) else None

        for pattern in patterns:
            match = re.search(pattern, link)
            if match:
                return match.group(1)
        return fallback

    def _get_playlist_songs(self, collection_id: str):
        url = (
            "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg?"
            f"type=1&utf8=1&disstid={collection_id}&loginUin=0&format=json"
        )
        result = self._qq_request_json(url)
        playlist_data = (result.get("cdlist") or [{}])[0]
        songs = playlist_data.get("songlist", [])
        return songs, f"歌单: {playlist_data.get('dissname', '未知')}", playlist_data.get("logo", "")

    def _get_album_songs(self, collection_id: str):
        url = (
            "https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg?"
            f"albummid={collection_id}&format=json"
        )
        result = self._qq_request_json(url)
        songs = result.get("data", {}).get("list", [])
        album_data = result.get("data", {})
        album_mid = album_data.get("mid") or collection_id
        cover = (
            f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg"
            if album_mid
            else ""
        )
        return songs, f"专辑: {album_data.get('name', '未知')}", cover

    # --- 下载与缓存（优化版） ---
    def _cache_key(self, song):
        identity = song.get("direct_url") if song.get("source") == "direct_url" else str(song.get("id"))
        identity = f"{song.get('source', 'qq')}:{identity}:opus128"
        return "v2_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    async def _download_qq_audio_url(self, url: str, song: dict, file_path: str):
        status = await self.audio.download(url, Path(file_path), cookie=self.qqmusic_cookie)
        if status == 200:
            self.qq_download_403_count = 0
            return file_path, status
        if status in {403, 429}:
            self.qq_download_403_count += 1
        print(f"[WARN] QQ audio rejected: status={status}")
        return None, status

    async def _download_direct_audio_url(self, url: str, song: dict, file_path: str):
        status = await self.audio.download(url, Path(file_path))
        return file_path if status == 200 else None

    async def _try_download_song(self, song):
        if not song:
            return None
        key = self._cache_key(song)
        path = self.cache_root / f"{key}.opus"
        if self.audio.valid_cache(path):
            os.utime(path, None)
            song["local_path"] = str(path)
            return str(path)
        if self.download_failures.get(key, 0) > time.monotonic():
            return None
        job = self.download_jobs.get(key)
        if job is None:
            lock = asyncio.Lock()
            self.download_locks[key] = lock
            job = asyncio.create_task(self._download_song_job(copy.deepcopy(song), path, lock))
            self.download_jobs[key] = job

            def done(task):
                # One owner cleans up only after ALL work has stopped. Waiters
                # never delete a lock or cancel a shared download.
                if self.download_jobs.get(key) is task:
                    self.download_jobs.pop(key, None)
                    self.download_locks.pop(key, None)
                if not task.cancelled() and task.exception() is not None:
                    print(f"[WARN] Music download task failed: {safe_error(task.exception())}")

            job.add_done_callback(done)
        result = await asyncio.shield(job)
        if result:
            song["local_path"] = result
        return result

    async def _download_song_job(self, song, path, lock):
        key = path.stem
        async with lock, self.download_slots:
            work_dir = Path(tempfile.mkdtemp(prefix="music_", dir=self.cache_root))
            try:
                result = await self._acquire_audio(song, work_dir, path)
                if not result:
                    self.download_failures[key] = time.monotonic() + 60
                    while len(self.download_failures) > 512:
                        self.download_failures.pop(next(iter(self.download_failures)))
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.download_failures[key] = time.monotonic() + 60
                while len(self.download_failures) > 512:
                    self.download_failures.pop(next(iter(self.download_failures)))
                print(f"[WARN] Audio acquisition failed: {safe_error(exc)}")
                return None
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

    async def _acquire_audio(self, song, directory, destination):
        raw = directory / "source.audio"
        prepared = directory / "prepared.opus"

        async def finish(source):
            await self.audio.prepare(Path(source), prepared)
            os.replace(prepared, destination)
            return str(destination)

        if song.get("source") == "direct_url":
            url = self._normalize_direct_audio_url(song.get("direct_url", ""))
            if not url:
                return None
            path = await self._download_direct_audio_url(url, song, str(raw))
            return await finish(path) if path else None

        if time.monotonic() >= self.qq_audio_blocked_until:
            try:
                async with asyncio.timeout(180):
                    result = await self._acquire_qq_audio(song, raw, finish)
                    if result:
                        return result
            except TimeoutError:
                print("[WARN] Official audio acquisition exceeded 180 seconds")
            except Exception as exc:
                print(f"[WARN] Official audio unavailable: {safe_error(exc)}")

        query = self._build_fallback_query(song) if yt_dlp is not None else ""
        duration = (song.get("dt") or 0) / 1000 or None
        for source in self.fallback_sources if query else ():
            # One source being blocked or returning a bad file must not stop
            # the next one; each attempt is bounded by the pipeline timeouts.
            try:
                path = await self.audio.fallback(
                    f"{query} audio" if source == "youtube" else query, directory,
                    source=source, duration=duration,
                    cookie_file=self._fallback_cookie_file(source, directory),
                )
                if path:
                    result = await finish(path)
                    print(f"[OK] Fallback audio prepared: source={source}")
                    return result
                print(f"[WARN] Fallback audio found no match: source={source}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[WARN] Fallback audio failed: source={source}; {safe_error(exc)}")
        return None

    async def _acquire_qq_audio(self, song, raw, finish):
        # Four auto qualities + legacy preview + ONE independent cookie refresh.
        # Never discard both MP3 variants when just the 320 kbps file is bad.
        max_attempts = len(self._qq_quality_candidates(song)) + 2
        refresh_attempted = False
        failed_urls = set()
        for _ in range(max_attempts):
            try:
                url = await asyncio.to_thread(self._get_song_url, song)
            except Exception as exc:
                print(f"[WARN] Official audio unavailable: {safe_error(exc)}")
                break
            if not url:
                if self.qqmusic_cookie and not refresh_attempted:
                    refresh_attempted = True
                    if await self._refresh_qqmusic_cookie_if_needed(force=True, reason="empty song url"):
                        song.pop("_qq_unavailable_files", None)
                        continue
                break

            filename = song.get("qq_filename", "")
            quality = filename[:4] if filename else "preview"
            # Ignore changing signatures: a bad URL must not be downloaded again
            # if another quality/preview points at the same physical file.
            parsed = urllib.parse.urlsplit(url)
            identity = (parsed.hostname, parsed.path)
            status = None
            try:
                if identity in failed_urls:
                    raise RuntimeError("QQ returned an already failed audio file")
                path, status = await self._download_qq_audio_url(url, song, str(raw))
                if path:
                    result = await finish(path)
                    print(f"[OK] Official audio prepared: quality={quality}")
                    return result
            except Exception as exc:
                print(f"[WARN] Official audio failed: quality={quality}; {safe_error(exc)}")

            if status in {403, 429}:
                if status == 403 and self.qqmusic_cookie and not refresh_attempted:
                    refresh_attempted = True
                    if await self._refresh_qqmusic_cookie_if_needed(force=True, reason="403 audio download"):
                        song.pop("_qq_unavailable_files", None)
                        continue
                # An authorization/rate-limit error is not a damaged file. Do
                # not hammer all qualities with the same rejected credential.
                self.qq_audio_blocked_until = time.monotonic() + 120
                break

            failed_urls.add(identity)
            if filename:
                song.setdefault("_qq_failed_files", set()).add(filename)
                print(f"[WARN] Official audio moving to next quality after {quality}; status={status}")
            else:
                song["_qq_skip_preview"] = True
                break
        return None

    def play_next(self, guild_id: int):
        if guild_id not in self.queues or self.queues[guild_id].get("stopping"):
            return

        self._stop_progress_task(guild_id)
        queue_data = self.queues[guild_id]

        if len(queue_data["queue"]) > 0:
            next_index = self._select_next_song_index(queue_data)
            if next_index is None:
                return
            next_song = queue_data["queue"].pop(next_index)
            queue_data["priority_next"] = None
            queue_data["shuffle_next"] = None
            queue_data["current"] = next_song
            queue_data["agent_phase"] = "loading"
            queue_data["playback_failed"] = False
            queue_data["agent_last_error"] = None
            self._spawn(self.update_player_ui(guild_id))
            self._spawn(self._play_music_task(guild_id, next_song))
        else:
            queue_data["current"] = None
            queue_data["start_time"] = None
            queue_data["paused_elapsed"] = 0
            queue_data["play_mode"] = "sequential"
            queue_data["priority_next"] = None
            queue_data["agent_phase"] = "idle"
            queue_data["playback_failed"] = False
            queue_data["agent_last_error"] = None
            self._spawn(self.update_player_ui(guild_id))

    def _play_is_current(self, guild_id, song, generation):
        data = self.queues.get(guild_id)
        return bool(data and not data.get("stopping") and data.get("current") is song
                    and data.get("voice_generation") == generation)

    async def _play_failed(self, guild_id, song, generation, error):
        if not self._play_is_current(guild_id, song, generation):
            return
        data = self.queues[guild_id]
        data["agent_phase"] = "error"
        data["playback_failed"] = True
        data["agent_last_error"] = safe_error(error)
        data["error_count"] += 1
        await self.update_player_ui(guild_id)
        if not self._play_is_current(guild_id, song, generation):
            return
        guild = self.bot.get_guild(guild_id)
        connected = guild and guild.voice_client and guild.voice_client.is_connected()
        if connected and data["queue"] and data["error_count"] < 3:
            await asyncio.sleep(min(data["error_count"] * 2, 6))
            if self._play_is_current(guild_id, song, generation):
                self.play_next(guild_id)
        elif (destination := panel_destination(guild, data)) is not None:
            await destination.send(
                "本曲播放未成功，已停止自动尝试；剩余队列保留。重新点歌会恢复播放，也可以点「跳过」。",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _play_music_task(self, guild_id: int, song):
        guild = self.bot.get_guild(guild_id)
        data = self.queues.get(guild_id)
        if not guild or not data or data.get("stopping") or data.get("current") is not song:
            return
        task = asyncio.current_task()
        previous = data.get("play_task")
        if previous and not previous.done() and previous is not task:
            if data.get("play_task_song") is song:
                return
            previous.cancel()
        data["play_task"] = task
        data["play_task_song"] = song
        generation = self._advance_voice_generation(guild_id)
        data["agent_phase"] = "loading"
        data["playback_failed"] = False
        data["agent_last_error"] = None
        data["start_time"] = None
        data["paused_elapsed"] = 0
        source = None
        handed_to_voice = False
        try:
            if not guild.voice_client or not guild.voice_client.is_connected():
                await self._play_failed(guild_id, song, generation, "语音连接不可用")
                return
            file_path = await self._try_download_song(song)
            if not self._play_is_current(guild_id, song, generation):
                return
            # Never use a VoiceClient captured before a potentially long download.
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                await self._play_failed(guild_id, song, generation, "语音连接已断开")
                return
            if not file_path:
                await self._play_failed(guild_id, song, generation, "音源获取或解码失败")
                return
            if vc.is_playing() or vc.is_paused():
                vc.stop()

            def after_callback(error):
                if self.loop is not None and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self._handle_track_finished, guild_id, generation, error)

            source = discord.FFmpegOpusAudio(
                file_path, codec="opus", executable=self.ffmpeg_executable or "ffmpeg",
                options="-vn", before_options="-nostdin -loglevel warning",
            )
            vc.play(source, after=after_callback)
            handed_to_voice = True
            data["agent_phase"] = "playing"
            data["start_time"] = time.time()
            data["paused_elapsed"] = 0
            self._start_progress_task(guild_id)
            self._begin_lyrics(guild_id, song)
            self._spawn(self._preload_next(guild_id))
            await self.update_player_ui(guild_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A UI/network edit failure must never skip a successfully started song.
            if not handed_to_voice:
                await self._play_failed(guild_id, song, generation, safe_error(exc))
            else:
                print(f"[WARN] Music UI update failed: {safe_error(exc)}")
        finally:
            if source is not None and not handed_to_voice:
                source.cleanup()
            if data.get("play_task") is task:
                data["play_task"] = None

    async def _play_music(self, interaction: discord.Interaction, song):
        guild_id = interaction.guild_id
        try:
            vc = await self._ensure_voice_client(interaction)
        except Exception as exc:
            print(f"[WARN] failed to connect voice for playback: {exc}")
            return

        if vc:
            await self._play_music_task(guild_id, song)

    async def _preload_next(self, guild_id):
        data = self.queues.get(guild_id)
        if not data or data.get("stopping"):
            return
        song = self._preview_next_song(guild_id)
        previous = data.get("preload_task")
        if previous and not previous.done():
            if data.get("preload_song") is song:
                return
            previous.cancel()
        data["preload_song"] = song
        data["preload_task"] = self._spawn(self._try_download_song(song)) if song else None
        self._prefetch_upcoming(data, song)

    def _prefetch_upcoming(self, data, next_song):
        # Shuffle only fixes the very next pick; sequential order is known, so
        # warm a few more tracks through the small shared prefetch budget.
        if next_song is None or data.get("play_mode") == "shuffle":
            return
        upcoming = [s for s in data["queue"][:self.preload_depth] if s is not next_song]
        for song in upcoming[:self.preload_depth - 1]:
            key = self._cache_key(song)
            if key in self.prefetch_keys or key in self.download_jobs:
                continue
            # While QQ is cooling down, prefetch would only burn fallback searches.
            if song.get("source") != "direct_url" and time.monotonic() < self.qq_audio_blocked_until:
                continue
            self.prefetch_keys.add(key)
            self._spawn(self._prefetch_song(key, song))

    async def _prefetch_song(self, key, song):
        try:
            async with self.prefetch_slots:
                await self._try_download_song(song)
        finally:
            self.prefetch_keys.discard(key)

    async def _fetch_lyrics(self, key, song):
        async with self.lyric_slots:
            try:
                result = await asyncio.to_thread(
                    self._qq_request_json,
                    "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?"
                    f"songmid={urllib.parse.quote(str(song['mid']))}&format=json&nobase64=1",
                )
                if result.get("code", 0) != 0:
                    raise RuntimeError("QQ lyrics endpoint unavailable")
                lines = parse_lrc(result.get("lyric"))
                state = "ready" if lines else "missing"
            except Exception as exc:
                print(f"[WARN] Lyrics unavailable: {type(exc).__name__}")
                lines, state = [], "error"
            self.lyric_cache[key] = (time.monotonic() + (86400 if lines else 300), lines, state)
            while len(self.lyric_cache) > 1024:
                self.lyric_cache.pop(next(iter(self.lyric_cache)))
            return lines, state

    def _begin_lyrics(self, guild_id, song):
        data = self.queues.get(guild_id)
        if not data:
            return
        task = data.get("lyrics_task")
        if task and not task.done():
            task.cancel()
        if data.get("lyrics_enabled", True):
            data["lyrics_task"] = self._spawn(self._load_song_lyrics(guild_id, song))

    async def _load_song_lyrics(self, guild_id, song):
        data = self.queues.get(guild_id)
        if not data or data.get("current") is not song or data.get("stopping"):
            return
        if not song.get("mid") or song.get("source") == "direct_url":
            song["lyrics"], song["lyrics_state"] = [], "missing"
            return
        key = str(song["mid"])
        cached = self.lyric_cache.get(key)
        if cached and cached[0] > time.monotonic():
            lines, state = cached[1:]
        else:
            song["lyrics_state"] = "loading"
            job = self.lyric_jobs.get(key)
            if job is None:
                job = self._spawn(self._fetch_lyrics(key, song))
                self.lyric_jobs[key] = job

                def done(finished):
                    if self.lyric_jobs.get(key) is finished:
                        self.lyric_jobs.pop(key, None)

                job.add_done_callback(done)
            lines, state = await asyncio.shield(job)
        if data is self.queues.get(guild_id) and data.get("current") is song and not data.get("stopping"):
            song["lyrics"], song["lyrics_state"] = lines, state
            if data.get("lyrics_enabled", True):
                await self.update_player_ui(guild_id)
                guild = self.bot.get_guild(guild_id)
                if (data.get("current") is song and guild and guild.voice_client
                        and guild.voice_client.is_playing()):
                    self._start_progress_task(guild_id)

    # --- 进度条任务 ---
    def _stop_progress_task(self, guild_id: int):
        if guild_id not in self.queues:
            return
        task = self.queues[guild_id].get("progress_task")
        if task and not task.done():
            task.cancel()
        self.queues[guild_id]["progress_task"] = None

    def _start_progress_task(self, guild_id: int):
        self._stop_progress_task(guild_id)
        task = asyncio.create_task(self._progress_updater(guild_id))
        self.queues[guild_id]["progress_task"] = task

    async def _progress_updater(self, guild_id: int):
        try:
            while True:
                data = self.queues.get(guild_id) or {}
                elapsed = data.get("paused_elapsed", 0)
                if data.get("start_time"):
                    elapsed += max(0, time.time() - data["start_time"])
                lines = (data.get("current") or {}).get("lyrics", []) if data.get("lyrics_enabled", True) else []
                await asyncio.sleep(next_refresh_delay(lines, elapsed))
                if guild_id not in self.queues:
                    break

                # 未在播放或当前暂停时，不刷新面板
                guild = self.bot.get_guild(guild_id)
                if not guild or not guild.voice_client:
                    break
                if guild.voice_client.is_paused():
                    continue
                if not guild.voice_client.is_playing():
                    continue

                await self.update_player_ui(guild_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"进度条任务异常: {e}")

    # --- 对外调用接口 ---

    async def stop_handling(self, guild_id: int):
        async with self.voice_locks.setdefault(guild_id, asyncio.Lock()):
            await self._stop_handling_locked(guild_id)

    async def _stop_handling_locked(self, guild_id: int):
        """Stop playback and clear the queue."""
        self._stop_progress_task(guild_id)
        if guild_id in self.queues:
            queue_data = self.queues[guild_id]
            queue_data["stopping"] = True
            for task in (queue_data.get("play_task"), queue_data.get("preload_task"), queue_data.get("lyrics_task")):
                if task and not task.done() and task is not asyncio.current_task():
                    task.cancel()
            queue_data["agent_phase"] = "stopping"
            queue_data["voice_generation"] = queue_data.get("voice_generation", 0) + 1
            queue_data["current"] = None
            queue_data["queue"] = []
            queue_data["start_time"] = None
            queue_data["paused_elapsed"] = 0
            queue_data["play_mode"] = "sequential"
            queue_data["priority_next"] = None
            queue_data["agent_phase"] = "idle"

            guild = self.bot.get_guild(guild_id)
            if guild and guild.voice_client:
                vc = guild.voice_client
                try:
                    if vc.is_playing() or vc.is_paused():
                        vc.stop()
                    await vc.disconnect(force=True)
                except Exception as exc:
                    print(f"[WARN] voice disconnect failed: {exc}")
                    try:
                        vc.cleanup()
                    except Exception:
                        pass
                finally:
                    self.voice_reconnect_after[guild_id] = time.monotonic() + 1.0

            queue_data["stopping"] = False
            queue_data["agent_phase"] = "disconnected"

            await self.update_player_ui(guild_id)

    async def prioritize_song(self, interaction: discord.Interaction, song_index: int, *, expected_song=None):
        """Move a queued song to play next."""
        guild_id = interaction.guild_id
        if guild_id not in self.queues:
            return False

        queue_list = self.queues[guild_id]["queue"]
        if expected_song is not None:
            song_index = next((i for i, item in enumerate(queue_list) if item is expected_song), -1)
        if song_index < 0 or song_index >= len(queue_list):
            return False

        song = queue_list.pop(song_index)
        queue_list.insert(0, song)
        self.queues[guild_id]["priority_next"] = song

        self._spawn(self._preload_next(guild_id))
        await self.update_player_ui(guild_id)
        return song["name"]

    async def remove_pending_songs(self, guild_id: int, expected_songs):
        """Remove only still-pending snapshot objects; never remove a new/current track."""
        data = self.queues.get(guild_id)
        if not data or data.get("stopping"):
            return 0
        identities = {id(song) for song in expected_songs}
        previous_count = len(data["queue"])
        data["queue"][:] = [song for song in data["queue"] if id(song) not in identities]
        removed = previous_count - len(data["queue"])
        if removed:
            for key in ("priority_next", "shuffle_next"):
                if self._priority_next_index(data["queue"], data.get(key)) is None:
                    data[key] = None
            self._spawn(self._preload_next(guild_id))
            await self.update_player_ui(guild_id)
        return removed

    async def skip_current(self, guild_id: int, expected_song):
        """Also allow skipping loading/failed tracks, without trusting an old panel."""
        data = self.queues.get(guild_id)
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if (not data or data.get("stopping") or expected_song is None
                or data.get("current") is not expected_song or not vc or not vc.is_connected()):
            return False
        # Invalidate the voice callback BEFORE stop, otherwise it may skip a second song.
        self._advance_voice_generation(guild_id)
        for key in ("play_task", "preload_task"):
            task = data.get(key)
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._stop_progress_task(guild_id)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        data["error_count"] = 0
        self.play_next(guild_id)
        await self.update_player_ui(guild_id)
        return True

    def enqueue_songs(self, guild_id: int, songs, *, position="end"):
        """One synchronous enqueue/start transition shared by the panel and Agent."""
        data = self._get_or_create_queue(guild_id)
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if data.get("stopping") or not vc or not vc.is_connected():
            raise RuntimeError("语音连接不可用，请重新加入语音频道后点歌。")
        if not is_voice_channel(vc.channel):
            raise RuntimeError('音乐播放目标必须是语音频道。')
        data['channel'] = vc.channel
        if position not in {"end", "next"}:
            raise ValueError("invalid queue position")
        if not songs:
            return {"added": 0, "rejected": 0, "started": False, "recovered": False}
        recovered = bool(data.get("current")
                         and (data.get("playback_failed") or data.get("agent_phase") == "error")
                         and not (vc.is_playing() or vc.is_paused()))
        if recovered:
            self._advance_voice_generation(guild_id)
            task = data.get("play_task")
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
            self._stop_progress_task(guild_id)
            data["current"] = None
            data["start_time"] = None
            data["paused_elapsed"] = 0
            data["error_count"] = 0
            data["playback_failed"] = False
        capacity = max(0, self.max_queue_length - len(data["queue"]) - bool(data.get("current")))
        accepted = list(songs[:capacity])
        if position == "next" and accepted:
            data["queue"][0:0] = accepted
            data["priority_next"] = accepted[0]
        else:
            data["queue"].extend(accepted)
        started = bool(not data.get("current") and data["queue"])
        if started:
            data["error_count"] = 0
            self.play_next(guild_id)
        elif accepted:
            self._spawn(self._preload_next(guild_id))
        return {"added": len(accepted), "rejected": len(songs) - len(accepted),
                "started": started, "recovered": recovered}

    # --- 批量 / 单曲添加 ---

    async def _add_songs_to_queue(
        self,
        interaction,
        songs,
        not_found_list=None,
        is_collection=False,
        collection_name="",
    ):
        if not await BaseView.interaction_check(self, interaction):
            return
        if not songs:
            return await interaction.followup.send("没有可添加的歌曲。", ephemeral=True)
        try:
            vc = await self._ensure_voice_client(interaction)
        except Exception as e:
            return await interaction.followup.send(
                f"Failed to connect voice: {e}", ephemeral=True
            )
        if not vc:
            return await interaction.followup.send(
                "You are not in a voice channel.", ephemeral=True
            )

        # Voice membership and the interaction origin can change while waiting
        # for search/connection. Revalidate immediately before the enqueue.
        if not await BaseView.interaction_check(self, interaction):
            return

        guild_id = interaction.guild_id
        queue_data = self._get_or_create_queue(guild_id)
        queue_data["stopping"] = False
        queue_data["channel"] = vc.channel

        result = self.enqueue_songs(guild_id, songs)
        added_count, rejected_count = result["added"], result["rejected"]

        await self.update_player_ui(guild_id)

        summary = []
        if is_collection:
            summary.append(f"💿 成功导入 **{collection_name}**")
            summary.append(f"✅ 共添加 **{added_count}** 首歌曲")
        else:
            summary.append(f"✅ 已添加 **{added_count}** 首歌曲")

        if rejected_count:
            summary.append(f"队列上限为 {self.max_queue_length} 首，未加入 {rejected_count} 首。")
        if result["recovered"]:
            summary.append("已结束上一首的失败状态，恢复队列播放。")
        if not_found_list:
            summary.append(f"❌ 未找到: {', '.join(not_found_list)}")

        await interaction.followup.send("\n".join(summary), ephemeral=True)

    async def add_search_selection(
        self, interaction: discord.Interaction, song: dict
    ) -> None:
        selected_song = self._copy_song_for_queue(song, interaction.user.display_name)
        await self._add_songs_to_queue(interaction, [selected_song])

    # --- 处理 Modal 提交入口 ---
    async def process_batch_request(
        self, interaction: discord.Interaction, queries: list
    ):
        if not await BaseView.interaction_check(self, interaction):
            return

        await interaction.response.defer(ephemeral=True)

        if len(queries) == 1:
            candidates = await asyncio.to_thread(self._search_song_candidates, queries[0])
            if not candidates:
                return await interaction.followup.send(
                    f"❌ 未找到: {queries[0]}", ephemeral=True
                )

            await interaction.followup.send(
                view=SearchResultView(self, interaction.guild_id, queries[0], candidates),
                ephemeral=True,
            )
            return

        songs_to_add = []
        not_found = []

        for query in queries:
            song = await asyncio.to_thread(self._search_song, query)
            if song:
                song["requester"] = interaction.user.display_name
                songs_to_add.append(song)
            else:
                not_found.append(query)

        await self._add_songs_to_queue(
            interaction, songs_to_add, not_found_list=not_found
        )

    async def process_direct_link_request(
        self, interaction: discord.Interaction, urls: list[str]
    ):
        if not await BaseView.interaction_check(self, interaction):
            return

        await interaction.response.defer(ephemeral=True)

        songs_to_add = []
        invalid_urls = []
        requester = interaction.user.display_name
        for raw_url in urls:
            url = self._normalize_direct_audio_url(raw_url)
            if not url:
                invalid_urls.append(raw_url)
                continue
            songs_to_add.append(self._direct_audio_song(url, requester))

        if not songs_to_add:
            return await interaction.followup.send(
                "❌ 没有有效的音频直链。请使用 http/https 开头的直接文件链接。",
                ephemeral=True,
            )

        await self._add_songs_to_queue(
            interaction,
            songs_to_add,
            not_found_list=invalid_urls,
            collection_name="直链音频",
        )

    async def process_collection_request(
        self, interaction: discord.Interaction, link: str, c_type: str
    ):
        if not await BaseView.interaction_check(self, interaction):
            return

        await interaction.response.defer(ephemeral=True)

        match = re.search(r"id=(\d+)|/(\d+)/?$", link)
        if match:
            collection_id = match.group(1) or match.group(2)
        else:
            collection_id = self._extract_collection_id(link, c_type)
            if not collection_id:
                return await interaction.followup.send(
                    "❌ 链接格式无法识别，QQ 音乐歌单需要数字 ID，专辑需要专辑 MID",
                    ephemeral=True,
                )

        try:
            tracks = []
            collection_name = ""
            global_cover = ""

            if c_type == "playlist":
                tracks, collection_name, global_cover = await asyncio.to_thread(
                    self._get_playlist_songs, collection_id
                )

            elif c_type == "album":
                tracks, collection_name, global_cover = await asyncio.to_thread(
                    self._get_album_songs, collection_id
                )

            if not tracks:
                return await interaction.followup.send(
                    f"❌ {collection_name} 获取为空", ephemeral=True
                )

        except Exception as e:
            print(f"API Error: {e}")
            return await interaction.followup.send(
                f"❌ 获取 QQ 音乐资源失败: {e}", ephemeral=True
            )

        song_queries = []
        requester = interaction.user.display_name

        for t in tracks:
            song_data = self._normalize_song_data(t, requester=requester)
            if not song_data["al"].get("picUrl") and global_cover:
                song_data["al"]["picUrl"] = global_cover
            song_queries.append(song_data)

        await self._add_songs_to_queue(
            interaction,
            song_queries,
            is_collection=True,
            collection_name=collection_name,
        )

    # --- 快捷指令 ---
    @app_commands.command(name="听歌", description="音乐控制面板")
    async def panel(self, interaction: discord.Interaction):
        if not await BaseView.interaction_check(self, interaction):
            return

        await interaction.response.defer(ephemeral=True)

        data = self._get_or_create_queue(interaction.guild_id)

        view = data["view"]
        try:
            async with data.setdefault("ui_lock", asyncio.Lock()):
                if not await BaseView.interaction_check(self, interaction):
                    return
                target = interaction.user.voice.channel
                data['channel'] = target
                view.update_container()
                await self._delete_panel_message(interaction.guild_id)
                data["message"] = await self._send_player_panel(target, view)
            await interaction.followup.send("✅ 已在当前频道底部刷新面板", ephemeral=True)
        except Exception as e:
            print(f"❌ 发送面板失败: {e}")
            await interaction.followup.send(f"❌ 发送面板失败: {e}", ephemeral=True)

