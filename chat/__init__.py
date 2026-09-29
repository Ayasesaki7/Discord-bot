import sqlite3
import json

from .cog import AtriChat
from .emoji_steal import EmojiStealCog
from .memory_commands import ChannelMemory
from .guild_settings import PolicyError


async def setup(bot):
    await bot.add_cog(AtriChat(bot))
    await bot.add_cog(EmojiStealCog(bot))
    try:
        await bot.add_cog(ChannelMemory(bot))
        bot._guild_policy_failed = False
    except (sqlite3.Error, OSError, PolicyError, json.JSONDecodeError) as exc:
        # Guild opt-outs live in the same DB. Never silently enable everything
        # when their persistent policy cannot be loaded. Other modules can run.
        bot._guild_policy_failed = True
        print(f'[ERROR] Guild policy/memory unavailable; chat fails closed: {type(exc).__name__}')
