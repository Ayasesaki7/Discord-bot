import asyncio
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from global_blacklist import is_global_blacklisted, read_global_blacklist_ids
from global_blacklist.store import read_owner_discord_id

BASE_DIR = Path(__file__).resolve().parent
PARENT_DIR = BASE_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')


class SilentGlobalBlacklist(commands.CheckFailure):
    pass


class SilentDirectMessage(commands.CheckFailure):
    pass


class BlacklistingCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            return False
        if interaction.user.id == read_owner_discord_id():
            return True
        return not is_global_blacklisted(self.client, interaction.user.id)


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.dm_messages = False
        intents.voice_states = True
        intents.message_content = True

        super().__init__(
            command_prefix='!',
            intents=intents,
            tree_cls=BlacklistingCommandTree,
            allowed_contexts=app_commands.AppCommandContext(
                guild=True,
                dm_channel=False,
                private_channel=False,
            ),
        )
        self.test_guild_id = self._parse_test_guild_id(os.getenv('TEST_GUILD_ID'))
        self.punishment_guild_id = self._parse_optional_guild_id(
            'PUNISHMENT_GUILD_ID'
        )
        self.global_blacklist_ids = read_global_blacklist_ids()
        self.add_check(self._global_prefix_command_check)

    @staticmethod
    def _parse_test_guild_id(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise RuntimeError('TEST_GUILD_ID must be numeric.') from exc

    @staticmethod
    def _parse_optional_guild_id(name: str) -> int | None:
        value = os.getenv(name, '').strip()
        if not value:
            return None
        try:
            guild_id = int(value)
        except ValueError as exc:
            raise RuntimeError(f'{name} must be numeric.') from exc
        return guild_id if guild_id > 0 else None

    def is_globally_blacklisted(self, user_id: int | None) -> bool:
        return is_global_blacklisted(self, user_id)

    async def _global_prefix_command_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise SilentDirectMessage()
        if getattr(ctx.author, 'id', None) == read_owner_discord_id():
            return True
        if self.is_globally_blacklisted(getattr(ctx.author, 'id', None)):
            raise SilentGlobalBlacklist()
        return True

    async def setup_hook(self) -> None:
        await self.load_extension('global_blacklist')
        await self.load_extension('music')
        await self.load_extension('chat')
        await self.load_extension('fortune')
        await self.load_extension('roles')
        await self.load_extension('bilibili')
        await self.load_extension('douyin')
        if self.punishment_guild_id:
            await self.load_extension('punishments')
        else:
            print('[INFO] Punishment extension disabled: PUNISHMENT_GUILD_ID is not set')
        await self.load_extension('system_admin')

        if self.test_guild_id:
            guild = discord.Object(id=self.test_guild_id)
            synced = await self.tree.sync(guild=guild)
            print(f'[OK] Synced {len(synced)} commands to test guild {self.test_guild_id}')
            if self.punishment_guild_id != self.test_guild_id:
                await self._sync_punishment_commands()
            return

        synced = await self.tree.sync()
        print(f'[OK] Synced {len(synced)} global commands')
        await self._sync_punishment_commands()

    async def _sync_punishment_commands(self) -> None:
        if not self.punishment_guild_id:
            return
        punishment_guild = discord.Object(id=self.punishment_guild_id)
        punishment_synced = await self.tree.sync(guild=punishment_guild)
        print(
            f'[OK] Synced {len(punishment_synced)} punishment commands '
            f'to guild {self.punishment_guild_id}'
        )

    async def on_ready(self) -> None:
        if self.user is None:
            return
        print(f'[READY] Logged in as {self.user} (ID: {self.user.id})')

    async def on_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, (SilentGlobalBlacklist, SilentDirectMessage)):
            return
        raise error


async def main() -> None:
    load_dotenv(BASE_DIR / '.env', encoding='utf-8-sig')
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise RuntimeError('Missing DISCORD_TOKEN in .env')

    bot = MusicBot()
    async with bot:
        await bot.start(token)


if __name__ == '__main__':
    asyncio.run(main())

