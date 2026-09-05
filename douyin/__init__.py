from .cog import DouyinVideoCog


async def setup(bot):
    await bot.add_cog(DouyinVideoCog(bot))
