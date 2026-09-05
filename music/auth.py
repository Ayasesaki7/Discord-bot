# cogs/music/music_u.py
import os

import discord


def _cookie_enabled() -> bool:
    cookie_path = os.getenv("QQMUSIC_COOKIE_FILE", "").strip()
    if cookie_path:
        return os.path.isfile(cookie_path)
    default_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "config",
        "credentials",
        "qqmusic_cookie.txt",
    )
    return bool(os.getenv("QQMUSIC_COOKIE", "").strip()) or os.path.isfile(default_path)


async def setup_async(bot: discord.Client, owner_id: int):
    """Initialize QQ Music mode and print the active auth status."""
    if _cookie_enabled():
        print("[INFO] QQ Music login cookie detected; official audio sources will be preferred")
    else:
        print("[WARN] QQ Music login cookie not found; guest mode and fallback sources will be used")
    return True


def setup_sync():
    """Keep compatibility with the extension loader and emit auth mode info."""
    if _cookie_enabled():
        print("[INFO] QQ Music cookie loaded; attempting higher-quality official audio")
    else:
        print("[WARN] QQ Music cookie is not configured; still running in guest mode")
    return True
