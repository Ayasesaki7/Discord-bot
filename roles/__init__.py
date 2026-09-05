from .cog import RoleClaimCog, RoleClaimPanelView


async def setup(bot):
    cog = RoleClaimCog(bot)
    await bot.add_cog(cog)
    bot.add_view(RoleClaimPanelView(cog))
