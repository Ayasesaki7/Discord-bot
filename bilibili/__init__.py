from .cog import BilibiliVideoCog


async def setup(bot):
    await bot.add_cog(BilibiliVideoCog(bot))
