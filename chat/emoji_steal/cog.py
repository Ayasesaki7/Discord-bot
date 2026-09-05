from __future__ import annotations

import io
import re
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


CUSTOM_EMOJI_PATTERN = re.compile(r'<(?P<animated>a)?:(?P<name>[A-Za-z0-9_]+):(?P<id>\d+)>')
DOWNLOAD_TIMEOUT_SECONDS = 20
GUILD_RESOLUTION_TIMEOUT_SECONDS = 8
MAX_DM_FILES_PER_MESSAGE = 10
MAX_GUILD_OPTIONS = 25
STICKER_UPLOAD_EMOJI = '\U0001F642'
SUPPORTED_STICKER_FORMATS = {
    discord.StickerFormatType.png,
    discord.StickerFormatType.apng,
    discord.StickerFormatType.gif,
    discord.StickerFormatType.lottie,
}
AssetKind = Literal['emoji', 'sticker']


def _log_download_failure(asset: 'StolenAsset', attempts: list[str], error: str | None = None) -> None:
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    joined_attempts = ' | '.join(attempts) if attempts else 'no attempts'
    print(
        f'[WARN] Asset download failed at {timestamp}: '
        f'kind={asset.kind}, '
        f'name={asset.name}, '
        f'id={asset.source_id}, '
        f'animated={asset.animated}, '
        f'file_extension={asset.file_extension}, '
        f'display={asset.display_text}, '
        f'asset_url={asset.asset_url}, '
        f'attempts={joined_attempts}, '
        f'error={error or "none"}'
    )




def _log_download_attempt(asset: 'StolenAsset', message: str) -> None:
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(
        f'[INFO] Asset download attempt at {timestamp}: '
        f'kind={asset.kind}, '
        f'name={asset.name}, '
        f'id={asset.source_id}, '
        f'message={message}'
    )
@dataclass(slots=True)
class StolenAsset:
    kind: AssetKind
    source_id: int
    name: str
    animated: bool
    file_extension: str
    asset_url: str
    display_text: str
    sticker_format: discord.StickerFormatType | None = None

    @property
    def filename(self) -> str:
        safe_name = re.sub(r'[^A-Za-z0-9_]+', '_', self.name).strip('_') or f'{self.kind}_{self.source_id}'
        return f'{safe_name}_{self.source_id}.{self.file_extension}'

    @property
    def upload_name(self) -> str:
        safe_name = re.sub(r'[^A-Za-z0-9_]+', '_', self.name).strip('_') or f'{self.kind}_{self.source_id}'
        return safe_name[:30]

    @property
    def summary_line(self) -> str:
        kind_label = '\u8868\u60c5' if self.kind == 'emoji' else '\u8d34\u7eb8'
        motion_label = '\u52a8\u6001' if self.animated else '\u9759\u6001'
        return f'{kind_label} | {motion_label} | {self.display_text}'

    def candidate_urls(self) -> list[str]:
        if self.kind == 'emoji':
            partial = discord.PartialEmoji(name=self.name, animated=self.animated, id=self.source_id)
            canonical_url = str(partial.url)
            base = f'https://cdn.discordapp.com/emojis/{self.source_id}'
            candidates = [canonical_url]
            if self.animated:
                candidates.extend([
                    f'{base}.webp?animated=true',
                    f'{base}.gif',
                    f'https://media.discordapp.net/emojis/{self.source_id}.gif',
                    f'{base}.gif?quality=lossless',
                    f'{base}.png',
                ])
            else:
                candidates.extend([f'{base}.webp', f'{base}.png'])

            unique_candidates: list[str] = []
            seen: set[str] = set()
            for candidate in candidates:
                if candidate in seen:
                    continue
                seen.add(candidate)
                unique_candidates.append(candidate)
            return unique_candidates
        return [self.asset_url]




@dataclass(slots=True)
class DownloadedAsset:
    asset: StolenAsset
    raw: bytes
    file_extension: str
    animated: bool

    @property
    def filename(self) -> str:
        safe_name = re.sub(r'[^A-Za-z0-9_]+', '_', self.asset.name).strip('_') or f'{self.asset.kind}_{self.asset.source_id}'
        return f'{safe_name}_{self.asset.source_id}.{self.file_extension}'


class GuildTargetSelect(discord.ui.Select['AssetStealView']):
    def __init__(self, view: 'AssetStealView', options: list[discord.SelectOption]):
        super().__init__(
            placeholder='\u9009\u62e9\u8981\u6dfb\u52a0\u5230\u54ea\u4e2a\u670d\u52a1\u5668',
            min_values=1,
            max_values=1,
            options=options,
        )
        self._asset_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values:
            await interaction.response.send_message('\u8fd8\u6ca1\u6709\u9009\u670d\u52a1\u5668\u3002', ephemeral=True)
            return

        await self._asset_view.add_to_guild(interaction, int(self.values[0]))


class AssetStealView(discord.ui.View):
    def __init__(
        self,
        cog: 'EmojiStealCog',
        *,
        owner_id: int,
        source_message: discord.Message,
        assets: list[StolenAsset],
        guild_options: list[discord.SelectOption],
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = owner_id
        self.source_message = source_message
        self.assets = assets
        self.guild_options = guild_options
        self.message: discord.InteractionMessage | None = None
        self._select_shown = False

        if not guild_options:
            self.add_to_guild_button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            '\u8fd9\u4e2a\u9762\u677f\u53ea\u80fd\u7531\u53d1\u8d77\u5077\u53d6\u7684\u4eba\u64cd\u4f5c\u3002',
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, 'disabled'):
                item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    def _summary_text(self) -> str:
        emoji_count = sum(1 for asset in self.assets if asset.kind == 'emoji')
        sticker_count = len(self.assets) - emoji_count
        lines = [f'\u627e\u5230 {len(self.assets)} \u4e2a\u53ef\u5077\u8d44\u6e90\uff1a\u8868\u60c5 {emoji_count} \u4e2a\uff0c\u8d34\u7eb8 {sticker_count} \u4e2a']
        lines.extend(asset.summary_line for asset in self.assets[:10])
        if len(self.assets) > 10:
            lines.append(f'... \u5171 {len(self.assets)} \u4e2a')
        if self.source_message.jump_url:
            lines.append(f'\u6765\u6e90\u6d88\u606f\uff1a{self.source_message.jump_url}')
        return '\n'.join(lines)

    @discord.ui.button(label='\u79c1\u4fe1\u53d1\u6211', style=discord.ButtonStyle.primary)
    async def send_dm_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        files, failures = await self.cog.build_files(self.assets)
        if not files:
            detail = '\n'.join(failures[:5]) if failures else '\u6ca1\u6709\u62ff\u5230\u4efb\u4f55\u53ef\u53d1\u9001\u7684\u6587\u4ef6\u3002'
            await interaction.followup.send(f'\u79c1\u4fe1\u53d1\u9001\u5931\u8d25\u3002\n{detail}', ephemeral=True)
            return

        sent_count = 0
        try:
            for index in range(0, len(files), MAX_DM_FILES_PER_MESSAGE):
                chunk = files[index : index + MAX_DM_FILES_PER_MESSAGE]
                content = self._summary_text() if index == 0 else None
                await interaction.user.send(content=content, files=chunk)
                sent_count += len(chunk)
        except discord.Forbidden:
            await interaction.followup.send(
                '\u6ca1\u6cd5\u7ed9\u4f60\u53d1\u79c1\u4fe1\uff0c\u53ef\u80fd\u662f\u4f60\u628a\u964c\u751f\u4eba\u79c1\u4fe1\u5173\u6389\u4e86\u3002\u53ef\u4ee5\u5148\u5f00\u79c1\u4fe1\uff0c\u6216\u8005\u6539\u7528\u201c\u6dfb\u52a0\u5230\u670d\u52a1\u5668\u201d\u3002',
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f'\u79c1\u4fe1\u53d1\u9001\u5931\u8d25\uff1a{exc}', ephemeral=True)
            return

        notice = f'\u5df2\u7ecf\u901a\u8fc7\u4e9a\u6258\u8389\u79c1\u4fe1\u7ed9\u4f60\u53d1\u4e86 {sent_count} \u4e2a\u6587\u4ef6\u3002'
        if failures:
            notice = f'{notice}\n\u53e6\u5916\u6709 {len(failures)} \u4e2a\u6ca1\u4e0b\u8f7d\u6210\u529f\u3002'
        await interaction.followup.send(notice, ephemeral=True)

    @discord.ui.button(label='\u6dfb\u52a0\u5230\u670d\u52a1\u5668', style=discord.ButtonStyle.secondary)
    async def add_to_guild_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not self.guild_options:
            await interaction.response.send_message(
                '\u6682\u65f6\u6ca1\u627e\u5230\u4f60\u548c\u4e9a\u6258\u8389\u90fd\u5728\u3001\u800c\u4e14\u53cc\u65b9\u90fd\u6709\u8868\u8fbe\u8d44\u6e90\u7ba1\u7406\u6743\u9650\u7684\u670d\u52a1\u5668\u3002',
                ephemeral=True,
            )
            return

        if not self._select_shown:
            self._select_shown = True
            self.add_item(GuildTargetSelect(self, self.guild_options))

        await interaction.response.edit_message(
            content='\u9009\u4e00\u4e2a\u8981\u5b58\u8fdb\u53bb\u7684\u76ee\u6807\u670d\u52a1\u5668\u3002',
            view=self,
        )

    async def add_to_guild(self, interaction: discord.Interaction, guild_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = self.cog.bot.get_guild(guild_id)
        if guild is None:
            await interaction.followup.send('\u76ee\u6807\u670d\u52a1\u5668\u4e0d\u89c1\u4e86\uff0c\u8bf7\u91cd\u65b0\u6253\u5f00\u9762\u677f\u518d\u8bd5\u3002', ephemeral=True)
            return

        successes, failures = await self.cog.copy_assets_to_guild(guild, self.assets)
        if not successes:
            detail = '\n'.join(failures[:5]) if failures else '\u6ca1\u6709\u4efb\u4f55\u8d44\u6e90\u6dfb\u52a0\u6210\u529f\u3002'
            await interaction.followup.send(f'\u6dfb\u52a0\u5931\u8d25\u3002\n{detail}', ephemeral=True)
            return

        lines = [f'\u5df2\u6dfb\u52a0\u5230 **{guild.name}**\uff1a']
        lines.extend(successes[:10])
        if len(successes) > 10:
            lines.append(f'... \u5171 {len(successes)} \u4e2a\u6210\u529f')
        if failures:
            lines.append(f'\u53e6\u5916\u6709 {len(failures)} \u4e2a\u5931\u8d25\u3002')
            lines.extend(failures[:3])
        await interaction.followup.send('\n'.join(lines), ephemeral=True)


class EmojiStealCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.steal_context_menu = app_commands.ContextMenu(
            name='\u5077\u8868\u60c5\u5305',
            callback=self.steal_from_message,
            allowed_contexts=discord.app_commands.AppCommandContext(
                guild=True,
                dm_channel=False,
                private_channel=True,
            ),
            allowed_installs=discord.app_commands.AppInstallationType(
                guild=True,
                user=True,
            ),
        )
        self.bot.tree.add_command(self.steal_context_menu)

    def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.steal_context_menu.name, type=self.steal_context_menu.type)

    def extract_custom_emojis(self, content: str) -> list[StolenAsset]:
        seen_ids: set[int] = set()
        assets: list[StolenAsset] = []
        for match in CUSTOM_EMOJI_PATTERN.finditer(content or ''):
            source_id = int(match.group('id'))
            if source_id in seen_ids:
                continue
            seen_ids.add(source_id)
            animated = bool(match.group('animated'))
            extension = 'gif' if animated else 'png'
            markup = f"<{('a' if animated else '')}:{match.group('name')}:{source_id}>"
            assets.append(
                StolenAsset(
                    kind='emoji',
                    source_id=source_id,
                    name=match.group('name'),
                    animated=animated,
                    file_extension=extension,
                    asset_url=f'https://cdn.discordapp.com/emojis/{source_id}.{extension}',
                    display_text=markup,
                    sticker_format=None,
                )
            )
        return assets

    def extract_stickers(self, message: discord.Message) -> list[StolenAsset]:
        seen_ids: set[int] = set()
        assets: list[StolenAsset] = []
        for sticker in message.stickers:
            source_id = getattr(sticker, 'id', None)
            sticker_format = getattr(sticker, 'format', None)
            sticker_name = getattr(sticker, 'name', 'sticker')
            sticker_url = str(getattr(sticker, 'url', ''))
            if not source_id or source_id in seen_ids:
                continue
            if sticker_format not in SUPPORTED_STICKER_FORMATS:
                continue
            seen_ids.add(source_id)
            if sticker_format == discord.StickerFormatType.gif:
                extension = 'gif'
                animated = True
                asset_url = f'https://media.discordapp.net/stickers/{source_id}.gif'
            elif sticker_format == discord.StickerFormatType.lottie:
                extension = 'json'
                animated = True
                asset_url = f'https://cdn.discordapp.com/stickers/{source_id}.json'
            elif sticker_format == discord.StickerFormatType.apng:
                extension = 'png'
                animated = True
                asset_url = f'https://cdn.discordapp.com/stickers/{source_id}.png'
            else:
                extension = 'png'
                animated = False
                asset_url = f'https://cdn.discordapp.com/stickers/{source_id}.png'
            display_text = f'\u8d34\u7eb8:{sticker_name}'
            assets.append(
                StolenAsset(
                    kind='sticker',
                    source_id=source_id,
                    name=sticker_name,
                    animated=animated,
                    file_extension=extension,
                    asset_url=asset_url or sticker_url,
                    display_text=display_text,
                    sticker_format=sticker_format,
                )
            )
        return assets

    def _resolve_downloaded_extension(self, asset: StolenAsset, url: str, content_type: str | None, fallback: str) -> tuple[str, bool]:
        normalized = (content_type or '').split(';', 1)[0].strip().lower()
        lowered_url = url.lower()
        if normalized == 'image/gif' or '.gif' in lowered_url:
            return 'gif', True
        if normalized == 'application/json' or lowered_url.endswith('.json'):
            return 'json', True
        if normalized == 'image/webp' or '.webp' in lowered_url:
            is_animated_webp = asset.kind == 'emoji' and asset.animated and 'animated=true' in lowered_url
            return 'webp', is_animated_webp
        if normalized == 'image/png' or '.png' in lowered_url:
            return 'png', False
        return fallback, fallback in {'gif', 'json'}

    async def download_asset(self, asset: StolenAsset) -> DownloadedAsset:
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
        headers = {
            'User-Agent': 'AtriEmojiSteal/1.0',
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        }
        attempts: list[str] = []
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for url in asset.candidate_urls():
                try:
                    _log_download_attempt(asset, f'trying {url}')
                    async with session.get(url) as response:
                        content_type = response.headers.get('Content-Type')
                        attempts.append(f'{url} -> HTTP {response.status} ({content_type})')
                        _log_download_attempt(asset, f'response {url} -> HTTP {response.status} ({content_type})')
                        if response.status != 200:
                            continue
                        raw = await response.read()
                        if not raw:
                            attempts.append(f'{url} -> empty body')
                            _log_download_attempt(asset, f'empty body from {url}')
                            continue
                        file_extension, animated = self._resolve_downloaded_extension(
                            asset,
                            str(response.url),
                            content_type,
                            asset.file_extension,
                        )
                        _log_download_attempt(
                            asset,
                            f'success {url} -> ext={file_extension}, animated={animated}, bytes={len(raw)}',
                        )
                        return DownloadedAsset(
                            asset=asset,
                            raw=raw,
                            file_extension=file_extension,
                            animated=animated,
                        )
                except Exception as exc:
                    attempts.append(f'{url} -> EXC {exc.__class__.__name__}: {exc}')
                    _log_download_attempt(asset, f'exception {url} -> {exc.__class__.__name__}: {exc}')
                    continue
        attempt_text = '; '.join(attempts) if attempts else 'no candidate urls'
        _log_download_failure(asset, attempts, error=attempt_text)
        raise RuntimeError(f'{asset.display_text} ?????{attempt_text}')

    async def build_files(self, assets: list[StolenAsset]) -> tuple[list[discord.File], list[str]]:
        files: list[discord.File] = []
        failures: list[str] = []
        for asset in assets:
            try:
                downloaded = await self.download_asset(asset)
            except Exception as exc:
                failures.append(str(exc))
                continue
            if asset.kind == 'emoji' and asset.animated and not downloaded.animated:
                failures.append(f'{asset.display_text} ???????????????????????? {downloaded.file_extension.upper()}?')
            files.append(discord.File(io.BytesIO(downloaded.raw), filename=downloaded.filename))
        return files, failures

    def _emoji_slot_failure(self, guild: discord.Guild, asset: StolenAsset) -> str | None:
        if asset.kind != 'emoji':
            return None
        static_count = sum(1 for emoji in guild.emojis if not emoji.animated)
        animated_count = sum(1 for emoji in guild.emojis if emoji.animated)
        limit = guild.emoji_limit
        if asset.animated and animated_count >= limit:
            return f'{asset.display_text} ???????????????????{animated_count}/{limit}??'
        if not asset.animated and static_count >= limit:
            return f'{asset.display_text} ???????????????????{static_count}/{limit}??'
        return None

    def _sticker_slot_failure(self, guild: discord.Guild, asset: StolenAsset) -> str | None:
        if asset.kind != 'sticker':
            return None
        limit = guild.sticker_limit
        used = len(guild.stickers)
        if used >= limit:
            return f'{asset.display_text} ?????????????????{used}/{limit}??'
        return None

    def _asset_slot_failure(self, guild: discord.Guild, asset: StolenAsset) -> str | None:
        return self._emoji_slot_failure(guild, asset) or self._sticker_slot_failure(guild, asset)

    async def copy_assets_to_guild(
        self,
        guild: discord.Guild,
        assets: list[StolenAsset],
        *,
        reason_prefix: str = 'Stolen via app command',
    ) -> tuple[list[str], list[str]]:
        successes: list[str] = []
        failures: list[str] = []
        for asset in assets:
            slot_failure = self._asset_slot_failure(guild, asset)
            if slot_failure is not None:
                failures.append(slot_failure)
                continue
            try:
                downloaded = await self.download_asset(asset)
                if asset.kind == 'emoji':
                    if asset.animated and not downloaded.animated:
                        failures.append(f'{asset.display_text} ????????????????????????')
                        continue
                    created = await guild.create_custom_emoji(
                        name=asset.upload_name,
                        image=downloaded.raw,
                        reason=f'{reason_prefix} from emoji {asset.source_id}'[:512],
                    )
                    successes.append(str(created))
                    continue

                created_sticker = await guild.create_sticker(
                    name=asset.upload_name,
                    description='stolen by atri',
                    emoji=STICKER_UPLOAD_EMOJI,
                    file=discord.File(io.BytesIO(downloaded.raw), filename=downloaded.filename),
                    reason=f'{reason_prefix} from sticker {asset.source_id}'[:512],
                )
                successes.append(f'\u8d34\u7eb8:{created_sticker.name}')
            except discord.Forbidden:
                failures.append(f'{asset.display_text} ????????????????????????????')
            except discord.HTTPException as exc:
                failures.append(f'{asset.display_text} ?????{exc}')
            except Exception as exc:
                failures.append(f'{asset.display_text} ?????{exc}')
        return successes, failures

    async def _resolve_candidate_guilds(self, user_id: int) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for guild in self.bot.guilds:
            me = guild.me
            if me is None:
                continue
            bot_perms = me.guild_permissions
            bot_can_manage_emoji = bot_perms.create_expressions or bot_perms.manage_expressions
            bot_can_manage_sticker = bot_perms.manage_emojis_and_stickers or bot_perms.manage_expressions
            if not (bot_can_manage_emoji or bot_can_manage_sticker):
                continue

            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    continue

            user_perms = member.guild_permissions
            user_can_manage_emoji = user_perms.create_expressions or user_perms.manage_expressions
            user_can_manage_sticker = user_perms.manage_emojis_and_stickers or user_perms.manage_expressions
            if not (user_can_manage_emoji or user_can_manage_sticker):
                continue

            options.append(
                discord.SelectOption(
                    label=guild.name[:100],
                    value=str(guild.id),
                    description='\u4f60\u548c\u4e9a\u6258\u8389\u90fd\u80fd\u7ba1\u7406\u8868\u60c5\u548c\u8d34\u7eb8',
                )
            )
            if len(options) >= MAX_GUILD_OPTIONS:
                break
        return options

    async def steal_from_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        assets = [*self.extract_custom_emojis(message.content), *self.extract_stickers(message)]
        if not assets:
            await interaction.followup.send(
                '\u8fd9\u6761\u6d88\u606f\u91cc\u6ca1\u627e\u5230\u53ef\u5077\u7684\u81ea\u5b9a\u4e49\u8868\u60c5\u6216\u8d34\u7eb8\u3002\n\u73b0\u5728\u652f\u6301\u6b63\u6587\u91cc\u7684 custom emoji \u548c\u6d88\u606f\u9644\u5e26\u7684 png/apng/gif/lottie \u8d34\u7eb8\u3002',
                ephemeral=True,
            )
            return

        try:
            guild_options = await asyncio.wait_for(
                self._resolve_candidate_guilds(interaction.user.id),
                timeout=GUILD_RESOLUTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            print(
                '[WARN] Emoji steal guild resolution timed out; '
                'showing DM-only panel'
            )
            guild_options = []
        except Exception as exc:
            print(f'[WARN] Emoji steal guild resolution failed: {exc}')
            guild_options = []

        view = AssetStealView(
            self,
            owner_id=interaction.user.id,
            source_message=message,
            assets=assets,
            guild_options=guild_options,
        )
        try:
            view.message = await interaction.followup.send(
                content=view._summary_text(),
                view=view,
                ephemeral=True,
                wait=True,
            )
        except discord.HTTPException:
            view.message = None
            await interaction.followup.send(
                '\u5077\u8868\u60c5\u9762\u677f\u53d1\u9001\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002',
                ephemeral=True,
            )

