from .cog import PUNISHMENT_GUILD_ID, PunishmentCog


async def setup(bot):
    await bot.add_cog(PunishmentCog(bot))
