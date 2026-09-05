"""Music feature package and discord.py extension entrypoint."""

from .auth import setup_sync
from .cog import Music
from .ui import MusicInterface


async def setup(bot):
    print("[INFO] Initializing music extension...")
    music_ready = setup_sync()
    print(f"[{'OK' if music_ready else 'WARN'}] Music auth initialized")

    bot.add_view(MusicInterface(bot))

    cog = Music(bot)
    cog.music_logged_in = music_ready
    await bot.add_cog(cog)
