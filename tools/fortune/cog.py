from __future__ import annotations

from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .service import BEIJING_TZ, DailyFortuneService
from .storage import FortuneRecord


FORTUNE_COLOR = 0xC08A3E
FORTUNE_DATA_PATH = (
    Path(__file__).resolve().parents[2]
    / "fortune"
    / "data"
    / "daily_fortunes.json"
)


def _score_bar(score: int) -> str:
    filled = max(1, min(10, round(score / 10)))
    return "█" * filled + "░" * (10 - filled)


def _join_items(items: list[str]) -> str:
    return "、".join(items) if items else "静候时机"


class DailyFortuneView(discord.ui.View):
    def __init__(self, cog: "DailyFortuneCog", user_id: int, *, disabled: bool) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.cast_button.disabled = disabled

    @discord.ui.button(label="摇卦起运", style=discord.ButtonStyle.primary, emoji="🩷")
    async def cast_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "这道签只给本人自己抽，劳烦你起一份自己的卦吧。",
                ephemeral=True,
            )
            return

        await self.cog.handle_cast(interaction, self)


class DailyFortuneCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.service = DailyFortuneService(FORTUNE_DATA_PATH)

    def reload_client_from_env(self) -> None:
        self.service.reload_client_from_env()

    @app_commands.command(name="每日运势", description="抽取今天的专属运势签文")
    async def daily_fortune(self, interaction: discord.Interaction) -> None:
        timing = self.service.current_timing()
        cached = await self.service.peek_fortune(
            user_id=interaction.user.id,
            timing=timing,
        )
        view = DailyFortuneView(
            self,
            interaction.user.id,
            disabled=cached is not None,
        )

        if cached is not None:
            await interaction.response.send_message(
                embed=self._build_result_embed(interaction.user, cached, cached_notice=True),
                view=view,
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=self._build_intro_embed(interaction.user, timing.reset_text),
            view=view,
            ephemeral=True,
        )

    async def handle_cast(
        self,
        interaction: discord.Interaction,
        view: DailyFortuneView,
    ) -> None:
        if not self.service.is_configured():
            await interaction.response.send_message(
                "每日运势会跟随聊天主 API；请先在 `.env` 里填写 `OPENAI_*` 配置。",
                ephemeral=True,
            )
            return

        view.cast_button.disabled = True
        await interaction.response.edit_message(
            embed=self._build_pending_embed(interaction.user),
            view=view,
        )
        try:
            result = await self.service.get_or_create_fortune(
                user_id=interaction.user.id,
                display_name=interaction.user.display_name,
            )
        except Exception as exc:
            view.cast_button.disabled = False
            await interaction.edit_original_response(
                embed=self._build_intro_embed(
                    interaction.user,
                    self.service.current_timing().reset_text,
                    status_text=(
                        "亚托莉刚刚顺着灵线去问上游了，可那边的卦盘忽然断了一下。\n"
                        f"这次没能顺利落签：{exc}"
                    ),
                ),
                view=view,
            )
            return

        await interaction.edit_original_response(
            embed=self._build_result_embed(
                interaction.user,
                result.record,
                cached_notice=result.from_cache,
            ),
            view=view,
        )

    def _build_intro_embed(
        self,
        user: discord.abc.User,
        reset_text: str,
        *,
        status_text: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="亚托莉的今日运势签",
            description=(
                "```text\n"
                "    亚托莉把铜钱排好了\n"
                "    一天一签，慢慢看就好\n"
                "```"
            ),
            color=FORTUNE_COLOR,
        )
        embed.add_field(name="签主", value=user.mention, inline=True)
        embed.add_field(name="规矩", value="每人每日仅可起卦一次", inline=True)
        embed.add_field(name="重置", value=f"北京时间 {reset_text}", inline=False)
        if status_text:
            embed.add_field(name="刚才的情况", value=status_text[:1024], inline=False)
        embed.set_footer(text="轻按下方按钮，亚托莉就去替你摇一签")
        return embed

    def _build_pending_embed(self, user: discord.abc.User) -> discord.Embed:
        embed = discord.Embed(
            title="亚托莉正在起卦",
            description=(
                f"{user.mention}，先别急，我正在把今天的潮声、风向和一点点运气整理给你。\n\n"
                "```text\n"
                "灵线接入中...\n"
                "签面推演中...\n"
                "```"
            ),
            color=FORTUNE_COLOR,
        )
        embed.set_footer(text="这条签只会悄悄显示给你")
        return embed

    def _build_result_embed(
        self,
        user: discord.abc.User,
        record: FortuneRecord,
        *,
        cached_notice: bool,
    ) -> discord.Embed:
        reset_text = (
            discord.utils.parse_time(record.reset_at)
            .astimezone(BEIJING_TZ)
            .strftime("%Y-%m-%d %H:%M")
        )
        subtitle = "今天的签文已经替你留好了" if cached_notice else "签火亮起来了，亚托莉已经看过啦"
        embed = discord.Embed(
            title=f"{record.sign} · {record.omen}",
            description=(
                f"**{record.summary}**\n\n"
                "```text\n"
                f"运势   {record.luck_score:>3}/100  {_score_bar(record.luck_score)}\n"
                f"吉色   {record.lucky_color}\n"
                f"吉向   {record.lucky_direction}\n"
                f"吉时   {record.lucky_time}\n"
                "```"
            ),
            color=FORTUNE_COLOR,
        )
        embed.add_field(name="签诗", value=f"「{record.poem}」", inline=False)
        embed.add_field(name="宜", value=_join_items(record.suitable), inline=True)
        embed.add_field(name="忌", value=_join_items(record.avoid), inline=True)
        embed.add_field(name="解签", value=record.detail, inline=False)
        embed.add_field(
            name="签注",
            value=f"{subtitle}\n次日北京时间 {reset_text} 可再来找亚托莉起新卦。",
            inline=False,
        )
        embed.set_author(name=f"{user.display_name} 的今日运势")
        if getattr(user, "display_avatar", None) is not None:
            embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="亚托莉的签只能陪你参考，真正的方向还是要靠你自己判断")
        return embed
