from .cog import GlobalBlacklistCog
from .store import is_global_blacklisted, read_global_blacklist_ids


async def setup(bot):
    await bot.add_cog(GlobalBlacklistCog(bot))
