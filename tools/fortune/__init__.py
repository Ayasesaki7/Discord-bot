from .cog import DailyFortuneCog


async def setup(bot):
    await bot.add_cog(DailyFortuneCog(bot))
