from .cog import AtriChat
from .emoji_steal import EmojiStealCog


async def setup(bot):
    await bot.add_cog(AtriChat(bot))
    await bot.add_cog(EmojiStealCog(bot))
