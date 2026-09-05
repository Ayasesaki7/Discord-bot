# cogs/music/music.py
import discord
from discord import app_commands
from discord.ext import commands

import os, aiohttp, time, asyncio, re, json, shutil, random, hashlib
import urllib.parse
import urllib.request
import copy
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from .ui import MusicInterface, SearchResultView
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


DIRECT_AUDIO_TIMEOUT_SECONDS = 60
MAX_DIRECT_AUDIO_BYTES = 200 * 1024 * 1024


class Music(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.loop = None
        self.queues = {}
        self.agent_locks = {}
        self.download_locks = {}
        self.music_logged_in = False  # QQ ?????????????????????????
        self.cache_root = Path(__file__).resolve().parents[1] / "music_cache"
        self.qqmusic_cookie_file = Path(
            os.getenv("QQMUSIC_COOKIE_FILE", "").strip()
            or (Path(__file__).resolve().parents[1] / "config" / "credentials" / "qqmusic_cookie.txt")
        )
        self.ffmpeg_executable = self._resolve_ffmpeg_executable()
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

    def cog_unload(self):
        if self.cleaner_task:
            self.cleaner_task.cancel()
        if self.qqmusic_refresh_task:
            self.qqmusic_refresh_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Handle async music setup on bot ready"""
        if not self.music_logged_in:
            self.music_logged_in = await setup_async(
                self.bot, getattr(config, "OWNER_ID", 0)
            )

    async def _periodic_cache_cleaner(self):
        await self.bot.wait_until_ready()
        EXPIRE_TIME = 3600

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(60)
                now = time.time()
                removed_count = 0
                total_size_freed = 0

                if self.cache_root.exists():
                    for file_path in self.cache_root.glob("*.mp3"):
                        if file_path.stem in self.download_locks:
                            continue

                        try:
                            stats = file_path.stat()
                            if now - stats.st_mtime > EXPIRE_TIME:
                                file_size = stats.st_size
                                os.remove(file_path)
                                removed_count += 1
                                total_size_freed += file_size
                        except Exception:
                            pass

                if removed_count > 0:
                    mb_freed = total_size_freed / (1024 * 1024)
                    print(
                        f"[OK] 缓存清理完成：删除 {removed_count} 个文件，释放 {mb_freed:.2f} MB 空间"
                    )
                await asyncio.sleep(600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[WARN] 缓存清理任务出错: {e}")
                await asyncio.sleep(60)

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
        return [(f"{prefix}{target}{suffix}", suffix) for prefix, suffix, target in selected]

    def _build_fallback_query(self, song: dict) -> str:
        artists = ", ".join(
            artist.get("name", "")
            for artist in song.get("ar", [])
            if isinstance(artist, dict) and artist.get("name")
        )
        return " ".join(part for part in [song.get("name"), artists, "audio"] if part)

    def _normalize_direct_audio_url(self, raw_url: str) -> str | None:
        url = raw_url.strip()
        if not url:
            return None
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return url

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

    def _download_song_with_ytdlp(self, song: dict, file_path_obj: Path):
        if yt_dlp is None:
            return None

        search_query = self._build_fallback_query(song)
        if not search_query:
            return None

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "noprogress": True,
            "overwrites": True,
            "outtmpl": str(file_path_obj.with_suffix(".%(ext)s")),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        if self.ffmpeg_executable:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_executable

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(f"ytsearch1:{search_query}", download=True)
        except Exception as exc:
            print(f"[WARN] yt-dlp fallback failed for {song.get('name')}: {exc}")
            return None

        if file_path_obj.exists() and file_path_obj.stat().st_size > 1024:
            file_path = str(file_path_obj)
            song["local_path"] = file_path
            return file_path
        return None

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
                "agent_phase": "idle",
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
            return None

        priority_index = self._priority_next_index(
            queue_list, queue_data.get("priority_next")
        )
        if priority_index is not None:
            return priority_index

        queue_data["priority_next"] = None
        if queue_data.get("play_mode", "sequential") == "shuffle":
            return random.randrange(len(queue_list))
        return 0

    def _preview_next_song(self, guild_id: int):
        queue_data = self._get_or_create_queue(guild_id)
        queue_list = queue_data["queue"]
        if not queue_list:
            queue_data["priority_next"] = None
            return None

        priority_index = self._priority_next_index(
            queue_list, queue_data.get("priority_next")
        )
        if priority_index is not None:
            return queue_list[priority_index]

        queue_data["priority_next"] = None
        if queue_data.get("play_mode", "sequential") == "shuffle":
            return random.choice(queue_list)
        return queue_list[0]

    def toggle_play_mode(self, guild_id: int):
        queue_data = self._get_or_create_queue(guild_id)
        current_mode = queue_data.get("play_mode", "sequential")
        queue_data["play_mode"] = (
            "shuffle" if current_mode == "sequential" else "sequential"
        )
        return queue_data["play_mode"]

    # --- UI 更新逻辑 ---
    async def update_player_ui(self, guild_id: int):
        if guild_id not in self.queues:
            return

        data = self.queues[guild_id]
        view: MusicInterface = data["view"]
        view.update_container()

        message = data.get("message")
        channel = data.get("channel")

        # 优先尝试编辑现有消息
        if message:
            try:
                await message.edit(view=view)
                return  # 编辑成功后直接返回
            except (discord.NotFound, discord.HTTPException):
                # 消息已删除或不可用，清空引用
                data["message"] = None

        # 如果消息不存在但频道还在，就补发一条新消息
        if not data["message"] and channel:
            try:
                new_msg = await channel.send(view=view)
                data["message"] = new_msg
            except Exception as send_err:
                print(f"[WARN] 重新发送面板消息失败: {send_err}")

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

    async def _ensure_voice_client_for(self, guild, member):
        if not guild or not getattr(member, "voice", None):
            return None

        guild_id = guild.id
        target_channel = member.voice.channel
        vc = guild.voice_client
        self._drop_stale_voice_client(vc)
        vc = guild.voice_client

        if vc and vc.channel != target_channel:
            await vc.move_to(target_channel)
            return vc

        if not vc:
            await self._respect_voice_reconnect_delay(guild_id)
            vc = guild.voice_client
            self._drop_stale_voice_client(vc)
            vc = guild.voice_client
            if vc:
                if vc.channel != target_channel:
                    await vc.move_to(target_channel)
                return vc
            return await target_channel.connect()

        return vc

    async def _ensure_voice_client(self, interaction: discord.Interaction):
        return await self._ensure_voice_client_for(
            interaction.guild,
            interaction.user,
        )

    def _handle_track_finished(
        self, guild_id: int, generation: int, error: Exception | None
    ) -> None:
        if error:
            print(f"FFmpeg Error: {error}")

        queue_data = self.queues.get(guild_id)
        if not queue_data:
            return
        if queue_data.get("stopping"):
            return
        if queue_data.get("voice_generation") != generation:
            return

        self.play_next(guild_id)

    def _qq_request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        data=None,
        headers: dict | None = None,
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw_bytes = resp.read()
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
                song.get("name")
                or song.get("title")
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
        try:
            keyword = query.strip()
            if not keyword:
                return None

            result = self._qq_request_json(
                "https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?"
                f"key={urllib.parse.quote(keyword)}&format=json"
            )
            songs = result.get("data", {}).get("song", {}).get("itemlist", [])
            for song in songs:
                song_mid = song.get("mid")
                if not song_mid:
                    continue
                detail = self._get_song_detail(song_mid)
                if detail:
                    return self._normalize_song_data(detail)

            if songs:
                return self._normalize_song_data(songs[0])
        except Exception as e:
            print(f"QQ 音乐搜索失败: {e}")
        return None

    def _search_song_candidates(self, query: str, limit: int = 8):
        try:
            keyword = query.strip()
            if not keyword:
                return []

            result = self._qq_request_json(
                "https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?"
                f"key={urllib.parse.quote(keyword)}&format=json"
            )
            items = result.get("data", {}).get("song", {}).get("itemlist", [])
            candidates = []
            seen_mids = set()

            for item in items:
                song_mid = item.get("mid")
                if not song_mid or song_mid in seen_mids:
                    continue
                seen_mids.add(song_mid)

                detail = self._get_song_detail(song_mid)
                if detail:
                    candidates.append(self._normalize_song_data(detail))
                else:
                    candidates.append(self._normalize_song_data(item))

                if len(candidates) >= limit:
                    break

            return candidates
        except Exception as e:
            print(f"QQ 音乐候选搜索失败: {e}")
            return []

    def _copy_song_for_queue(self, song: dict, requester: str):
        song_copy = copy.deepcopy(song)
        song_copy["requester"] = requester
        return song_copy

    def _get_song_url(self, song: dict):
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
            print(f"QQ 音乐获取播放链接失败: {e}")
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
    async def _download_qq_audio_url(self, url: str, song: dict, file_path: str):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Referer": "https://y.qq.com/",
            "Origin": "https://y.qq.com",
        }
        if self.qqmusic_cookie:
            headers["Cookie"] = self.qqmusic_cookie

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    if len(content) > 1024:
                        temp_path = file_path + ".tmp"
                        with open(temp_path, "wb") as f:
                            f.write(content)
                        os.rename(temp_path, file_path)

                        song["local_path"] = file_path
                        self.qq_download_403_count = 0
                        return file_path, resp.status

                if resp.status == 403:
                    self.qq_download_403_count += 1
                print(
                    "[WARN] QQ audio request returned status "
                    f"{resp.status} for {song.get('name')} "
                    f"(filename={song.get('qq_filename', 'unknown')}, "
                    f"base={song.get('qq_audio_base', 'unknown')}, "
                    f"consecutive_403={self.qq_download_403_count})"
                )
                if resp.status == 403 and self.qq_download_403_count >= 2:
                    print(
                        "[WARN] QQ Music audio was rejected repeatedly; cookie may be expired or risk-controlled. "
                        "Trying automatic cookie refresh before falling back."
                    )
                return None, resp.status

    async def _download_direct_audio_url(self, url: str, song: dict, file_path: str):
        timeout = aiohttp.ClientTimeout(total=DIRECT_AUDIO_TIMEOUT_SECONDS)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": "audio/*,*/*;q=0.8",
        }

        temp_path = file_path + ".tmp"
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        print(
                            "[WARN] direct audio request returned status "
                            f"{resp.status} for {song.get('name')} ({url})"
                        )
                        return None

                    content_length = resp.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > MAX_DIRECT_AUDIO_BYTES:
                                print(
                                    "[WARN] direct audio file is too large: "
                                    f"{content_length} bytes ({url})"
                                )
                                return None
                        except ValueError:
                            pass

                    total_size = 0
                    with open(temp_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            if not chunk:
                                continue
                            total_size += len(chunk)
                            if total_size > MAX_DIRECT_AUDIO_BYTES:
                                print(
                                    "[WARN] direct audio download exceeded size limit "
                                    f"({MAX_DIRECT_AUDIO_BYTES} bytes): {url}"
                                )
                                return None
                            f.write(chunk)

                    if total_size <= 1024:
                        print(f"[WARN] direct audio response was too small: {url}")
                        return None

            os.replace(temp_path, file_path)
            song["local_path"] = file_path
            return file_path
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    async def _try_download_song(self, song):
        """
        下载歌曲，使用全局缓存和并发锁。
        """
        if not song:
            return None

        song_id = str(song["id"])
        file_path_obj = self.cache_root / f"{song_id}.mp3"
        file_path = str(file_path_obj)

        if file_path_obj.exists() and file_path_obj.stat().st_size > 1024:
            song["local_path"] = file_path
            return file_path

        if song_id not in self.download_locks:
            self.download_locks[song_id] = asyncio.Lock()

        async with self.download_locks[song_id]:
            if file_path_obj.exists() and file_path_obj.stat().st_size > 1024:
                song["local_path"] = file_path
                return file_path

            if song.get("source") == "direct_url":
                direct_url = self._normalize_direct_audio_url(song.get("direct_url", ""))
                if not direct_url:
                    print(f"[WARN] invalid direct audio URL for {song.get('name')}")
                    return None
                try:
                    return await self._download_direct_audio_url(
                        direct_url, song, file_path
                    )
                except Exception as e:
                    print(
                        f"[WARN] direct audio download failed for "
                        f"{song.get('name')}: {e}"
                    )
                    return None

            url = await asyncio.to_thread(self._get_song_url, song)
            if not url and self.qqmusic_cookie:
                refreshed = await self._refresh_qqmusic_cookie_if_needed(
                    force=True, reason="empty song url"
                )
                if refreshed:
                    url = await asyncio.to_thread(self._get_song_url, song)
                else:
                    await self._send_qqmusic_cookie_dm(
                        "empty_song_url_refresh_failed",
                        self._qqmusic_cookie_status_message(
                            "QQ Music could not return an official playback URL, "
                            "and automatic cookie refresh did not recover it."
                        ),
                    )

            try:
                if url:
                    downloaded_path, status = await self._download_qq_audio_url(
                        url, song, file_path
                    )
                    if downloaded_path:
                        return downloaded_path

                    if status == 403 and self.qqmusic_cookie:
                        refreshed = await self._refresh_qqmusic_cookie_if_needed(
                            force=True, reason="403 audio download"
                        )
                        if refreshed:
                            retry_url = await asyncio.to_thread(self._get_song_url, song)
                            if retry_url:
                                (
                                    downloaded_path,
                                    retry_status,
                                ) = await self._download_qq_audio_url(
                                    retry_url, song, file_path
                                )
                                if downloaded_path:
                                    return downloaded_path
                                if retry_status == 403:
                                    await self._send_qqmusic_cookie_dm(
                                        "audio_403_after_refresh",
                                        self._qqmusic_cookie_status_message(
                                            "QQ Music audio still returned 403 after automatic cookie refresh."
                                        ),
                                    )
                        else:
                            await self._send_qqmusic_cookie_dm(
                                "audio_403_refresh_failed",
                                self._qqmusic_cookie_status_message(
                                    "QQ Music audio returned 403, "
                                    "and automatic cookie refresh did not recover it."
                                ),
                            )

                fallback_path = await asyncio.to_thread(
                    self._download_song_with_ytdlp, song, file_path_obj
                )
                if fallback_path:
                    return fallback_path
            except Exception as e:
                print(f"[WARN] audio download failed for {song.get('name')}: {e}")
                if os.path.exists(file_path):
                    os.remove(file_path)
                if os.path.exists(file_path + ".tmp"):
                    os.remove(file_path + ".tmp")
            finally:
                if song_id in self.download_locks:
                    del self.download_locks[song_id]

        return None

    async def _smart_cache_clean(self, guild_id: int):
        pass

    def play_next(self, guild_id: int):
        if guild_id not in self.queues:
            return

        self._stop_progress_task(guild_id)
        queue_data = self.queues[guild_id]

        if len(queue_data["queue"]) > 0:
            next_index = self._select_next_song_index(queue_data)
            if next_index is None:
                return
            next_song = queue_data["queue"].pop(next_index)
            queue_data["priority_next"] = None
            queue_data["current"] = next_song
            queue_data["agent_phase"] = "loading"
            queue_data["agent_last_error"] = None
            asyncio.create_task(self.update_player_ui(guild_id))
            asyncio.create_task(self._play_music_task(guild_id, next_song))
        else:
            queue_data["current"] = None
            queue_data["start_time"] = None
            queue_data["paused_elapsed"] = 0
            queue_data["play_mode"] = "sequential"
            queue_data["priority_next"] = None
            queue_data["agent_phase"] = "idle"
            queue_data["agent_last_error"] = None
            asyncio.create_task(self.update_player_ui(guild_id))

    async def _play_music_task(self, guild_id: int, song):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        queue_data = self.queues.get(guild_id)
        if not queue_data or queue_data.get("stopping"):
            return
        queue_data["agent_phase"] = "loading"
        queue_data["agent_last_error"] = None
        vc = guild.voice_client
        self._drop_stale_voice_client(vc)
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            queue_data["agent_phase"] = "error"
            queue_data["agent_last_error"] = "voice connection unavailable"
            return

        file_path = song.get("local_path")
        if not file_path or not os.path.exists(file_path):
            file_path = await self._try_download_song(song)

        if queue_data.get("stopping"):
            return

        if not file_path:
            print(f"[WARN] unable to acquire audio file for {song['name']}")
            queue_data["agent_phase"] = "error"
            queue_data["agent_last_error"] = "audio acquisition failed"
            self.queues[guild_id]["error_count"] += 1
            if self.queues[guild_id]["error_count"] < 5:
                await asyncio.sleep(1)
                self.play_next(guild_id)
            else:
                if self.queues[guild_id]["channel"]:
                    await self.queues[guild_id]["channel"].send(
                        "连续播放失败次数过多，队列已停止。"
                    )
            return
        try:
            os.utime(file_path, None)
        except Exception as e:
            print(f"[WARN] failed to update cache timestamp: {e}")

        # 重置连续错误计数
        self.queues[guild_id]["error_count"] = 0
        ffmpeg_opts = {"options": "-vn"}
        generation = self._advance_voice_generation(guild_id)

        def after_callback(error):
            if self.loop is not None:
                self.loop.call_soon_threadsafe(
                    self._handle_track_finished, guild_id, generation, error
                )

        if vc.is_playing() or vc.is_paused():
            vc.stop()

        try:
            if self.ffmpeg_executable:
                source = discord.FFmpegPCMAudio(
                    file_path, executable=self.ffmpeg_executable, **ffmpeg_opts
                )
            else:
                source = discord.FFmpegPCMAudio(file_path, **ffmpeg_opts)
            vc.play(source, after=after_callback)

            self.queues[guild_id]["agent_phase"] = "playing"
            self.queues[guild_id]["agent_last_error"] = None

            self.queues[guild_id]["start_time"] = time.time()
            self.queues[guild_id]["paused_elapsed"] = 0

            await self.update_player_ui(guild_id)
            self._start_progress_task(guild_id)
            asyncio.create_task(self._preload_next(guild_id))

        except Exception as e:
            print(f"播放异常: {e}")
            self.queues[guild_id]["agent_phase"] = "error"
            self.queues[guild_id]["agent_last_error"] = e.__class__.__name__
            await asyncio.sleep(1)
            self.play_next(guild_id)

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
        """Preload the next track in the queue."""
        if guild_id not in self.queues:
            return
        next_song = self._preview_next_song(guild_id)
        if next_song:
            asyncio.create_task(self._try_download_song(next_song))

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
                await asyncio.sleep(10)  # 每 10 秒刷新一次
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
        """Stop playback and clear the queue."""
        self._stop_progress_task(guild_id)
        if guild_id in self.queues:
            queue_data = self.queues[guild_id]
            queue_data["stopping"] = True
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

    async def prioritize_song(self, interaction: discord.Interaction, song_index: int):
        """Move a queued song to play next."""
        guild_id = interaction.guild_id
        if guild_id not in self.queues:
            return False

        queue_list = self.queues[guild_id]["queue"]
        if song_index < 0 or song_index >= len(queue_list):
            return False

        song = queue_list.pop(song_index)
        queue_list.insert(0, song)
        self.queues[guild_id]["priority_next"] = song

        asyncio.create_task(self._preload_next(guild_id))
        await self.update_player_ui(guild_id)
        return song["name"]

    # --- 批量 / 单曲添加 ---

    async def _add_songs_to_queue(
        self,
        interaction,
        songs,
        not_found_list=None,
        is_collection=False,
        collection_name="",
    ):
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

        guild_id = interaction.guild_id
        queue_data = self._get_or_create_queue(guild_id)
        queue_data["stopping"] = False
        queue_data["channel"] = interaction.channel
        if queue_data["message"] is None and interaction.message:
            queue_data["message"] = interaction.message

        added_count = 0
        for song in songs:
            if not queue_data["current"]:
                queue_data["current"] = song
                asyncio.create_task(self._play_music(interaction, song))
            else:
                queue_data["queue"].append(song)
            added_count += 1
            if added_count == 1 and len(queue_data["queue"]) > 0:
                asyncio.create_task(self._preload_next(guild_id))

        await self.update_player_ui(guild_id)

        summary = []
        if is_collection:
            summary.append(f"💿 成功导入 **{collection_name}**")
            summary.append(f"✅ 共添加 **{added_count}** 首歌曲")
        else:
            summary.append(f"✅ 已添加 **{added_count}** 首歌曲")

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
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "❌ 先进入语音频道", ephemeral=True
            )

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
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "❌ 需要先进入语音频道", ephemeral=True
            )

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
        if not interaction.user.voice:
            return await interaction.response.send_message(
                "❌ 需要先进入语音频道", ephemeral=True
            )

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
        if not interaction.user.voice:
            await interaction.response.send_message(
                "❌ 请先加入语音频道", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        data = self._get_or_create_queue(interaction.guild_id)
        data["channel"] = interaction.channel

        view = data["view"]
        view.update_container()
        try:
            await self._delete_panel_message(interaction.guild_id)
            data["message"] = await interaction.channel.send(view=view)
            await interaction.followup.send("✅ 已在当前频道底部刷新面板", ephemeral=True)
        except Exception as e:
            print(f"❌ 发送面板失败: {e}")
            await interaction.followup.send(f"❌ 发送面板失败: {e}", ephemeral=True)

