"""Compatibility extension entrypoint for the fortune tool."""

try:
    from ..tools.fortune import DailyFortuneCog
except ImportError:  # Top-level compatibility for local tests and scripts.
    from tools.fortune import DailyFortuneCog


async def setup(bot):
    await bot.add_cog(DailyFortuneCog(bot))
