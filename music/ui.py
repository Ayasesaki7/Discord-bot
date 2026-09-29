# cogs/music/music_v.py
import os
from pathlib import Path

import discord
from discord import ui
import random, time, math
from .lyrics import lyric_window
from .access import in_voice_chat, reject_interaction

闲暇 = [
    "时光慢慢，心事懒懒",
    "走走，停停。路也没意见",
    "阳光里打个盹，慢慢做个梦",
    "世界呼叫中……这里信号不足",
]

PAUSE = os.getenv("MUSIC_PAUSE_EMOJI", "⏸️").strip() or "⏸️"
FILL = os.getenv("MUSIC_PROGRESS_FILL_EMOJI", "▬").strip() or "▬"
KNOB = os.getenv("MUSIC_PROGRESS_KNOB_EMOJI", "🔘").strip() or "🔘"
ADD = os.getenv("MUSIC_ADD_EMOJI", "🎵").strip() or "🎵"
DEFAULT_COVER_PATH = Path(__file__).with_name("assets") / "default-cover.png"
DEFAULT_COVER_FILENAME = "atri-music-default.png"
DEFAULT_COVER_URL = f"attachment://{DEFAULT_COVER_FILENAME}"


def panel_text(value, limit=120):
    """Keep externally supplied music metadata inside its own card field."""
    text = " ".join(str(value or "未知").split())
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text[:limit]))


def duration_text(milliseconds):
    seconds = max(0, int((milliseconds or 0) / 1000))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class BaseView(ui.LayoutView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if getattr(interaction.client, 'is_globally_blacklisted', lambda _user_id: False)(interaction.user.id):
            return False
        if not interaction.guild or not getattr(getattr(interaction.user, "voice", None), "channel", None):
            await reject_interaction(interaction, "❌ 需要先进入语音频道才能进行操作！")
            return False
        vc = interaction.guild.voice_client
        if vc and vc.is_connected() and vc.channel != interaction.user.voice.channel:
            await reject_interaction(interaction, "❌ 请加入 BOT 所在的语音频道后再操作。")
            return False
        if not in_voice_chat(getattr(interaction, 'channel', None), interaction.user.voice.channel):
            await reject_interaction(interaction, "❌ 请在你当前加入的语音频道的文字聊天区使用音乐功能，普通文字频道不能点歌或控制播放。")
            return False
        if getattr(self, "guild_id", None) not in {None, interaction.guild_id}:
            return False
        return True


class SearchModal(ui.Modal, title="点歌台"):
    mode_label = ui.Label(
        text="搜索模式",
        description="选择要搜索的类型",
        component=ui.Select(
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="搜索歌曲",
                    value="song",
                    description="输入歌名/关键词",
                    emoji="🎵",
                    default=True,
                ),
                discord.SelectOption(
                    label="导入歌单",
                    value="playlist",
                    description="粘贴歌单链接",
                    emoji="📜",
                ),
                discord.SelectOption(
                    label="导入专辑",
                    value="album",
                    description="粘贴专辑链接",
                    emoji="💿",
                ),
                discord.SelectOption(
                    label="播放直链",
                    value="direct",
                    description="粘贴 mp3/flac/m4a 等音频直链",
                    emoji="🔗",
                ),
            ],
        ),
    )

    query_label = ui.Label(
        text="关键词/链接",
        description="搜歌每行一首；导入粘贴分享链接",
        component=ui.TextInput(
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=1000,
        ),
    )

    def __init__(self, cog, guild_id):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        assert isinstance(self.mode_label.component, ui.Select)
        assert isinstance(self.query_label.component, ui.TextInput)

        # A modal can remain open after its author moves/leaves voice.
        if not await BaseView.interaction_check(self, interaction):
            return
        search_type = self.mode_label.component.values[0]
        content = self.query_label.component.value
        lines = [line.strip() for line in content.split("\n") if line.strip()]

        if not lines:
            return await interaction.response.send_message(
                "❌ 内容不能为空", ephemeral=True
            )

        if search_type == "song":
            await self.cog.process_batch_request(interaction, lines)
        elif search_type == "direct":
            await self.cog.process_direct_link_request(interaction, lines)
        else:
            await self.cog.process_collection_request(
                interaction, lines[0], search_type
            )


class SearchResultView(BaseView):
    def __init__(self, cog, guild_id, query, candidates):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.query = query
        self.candidates = candidates
        self.selected_index = 0 if candidates else None
        self._build_view()

    def _format_duration(self, duration_ms):
        total_seconds = max(int((duration_ms or 0) / 1000), 0)
        minutes, seconds = divmod(total_seconds, 60)
        return f"{minutes}:{seconds:02d}"

    def _build_lines(self):
        lines = []
        for idx, song in enumerate(self.candidates, start=1):
            marker = ">" if self.selected_index == idx - 1 else "-"
            artist = (song.get("ar") or [{}])[0].get("name", "未知")
            album = (song.get("al") or {}).get("name", "未知专辑")
            duration = self._format_duration(song.get("dt", 0))
            entry = f"{marker} {idx}. {song['name']} - {artist} [{duration}] / {album}"
            lines.append(entry[:80] + "..." if len(entry) > 80 else entry)
        return "```md\n" + "\n".join(lines) + "\n```"

    def _build_view(self):
        self.clear_items()

        if not self.candidates:
            container = ui.Container(
                ui.TextDisplay(content="### ❌ 没有找到候选歌曲"),
                accent_color=discord.Color.red(),
            )
            self.add_item(container)
            return

        options = []
        for idx, song in enumerate(self.candidates):
            artist = (song.get("ar") or [{}])[0].get("name", "未知")
            album = (song.get("al") or {}).get("name", "未知专辑")
            label = f"{idx + 1}. {song['name']}"[:100]
            description = f"{artist} | {album}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(idx),
                    default=(self.selected_index == idx),
                )
            )

        select_menu = ui.Select(
            placeholder="选择要添加的歌曲...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"search_sel_{time.time()}",
        )
        select_menu.callback = self.on_select_option

        btn_confirm = ui.Button(
            label="添加所选",
            style=discord.ButtonStyle.success,
            emoji="🎵",
            disabled=(self.selected_index is None),
        )
        btn_confirm.callback = self.on_confirm

        btn_cancel = ui.Button(
            label="取消",
            style=discord.ButtonStyle.secondary,
            emoji="✖️",
        )
        btn_cancel.callback = self.on_cancel

        container = ui.Container(
            ui.TextDisplay(content=f"### 搜索结果: {self.query}"),
            ui.TextDisplay(content=self._build_lines() + "\n-# 选择一首后再加入队列"),
            ui.Separator(),
            ui.ActionRow(select_menu),
            ui.ActionRow(btn_confirm, btn_cancel),
            accent_color=discord.Color.from_rgb(255, 95, 95),
        )
        self.add_item(container)

    async def on_select_option(self, interaction: discord.Interaction):
        self.selected_index = int(interaction.data["values"][0])
        self._build_view()
        await interaction.response.edit_message(view=self)

    async def on_confirm(self, interaction: discord.Interaction):
        if self.selected_index is None:
            return await interaction.response.defer()

        if getattr(self, "_submitted", False):
            return await interaction.response.send_message("这次选择已经提交。", ephemeral=True)
        self._submitted = True
        song = self.candidates[self.selected_index]
        await interaction.response.defer()
        await self.cog.add_search_selection(interaction, song)

        self.clear_items()
        result = ui.Container(
            ui.TextDisplay(content="### ✅ 已加入播放队列"),
            ui.TextDisplay(content=f"已选择 **{song['name']}**"),
            accent_color=discord.Color.green(),
        )
        self.add_item(result)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    async def on_cancel(self, interaction: discord.Interaction):
        self.clear_items()
        result = ui.Container(
            ui.TextDisplay(content="### 已取消本次搜索"),
            accent_color=discord.Color.greyple(),
        )
        self.add_item(result)
        await interaction.response.edit_message(view=self)


class QueueListView(BaseView):
    def __init__(self, cog, guild_id, queue_data):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.queue = list(queue_data)  # Stable song identities, not mutable numeric positions.
        self.page = 0
        self.items_per_page = 25
        self.selected_index = None
        self._build_view()

    def _build_view(self):
        self.clear_items()

        if not self.queue:
            container = ui.Container(
                ui.TextDisplay(content="### ❌ 播放队列为空"),
                accent_color=discord.Color.greyple(),
            )
            self.add_item(container)
            return

        total_pages = math.ceil(len(self.queue) / self.items_per_page)
        self.page = max(0, min(self.page, total_pages - 1))

        start = self.page * self.items_per_page
        end = start + self.items_per_page
        current_items = self.queue[start:end]

        lines = []
        for i, song in enumerate(current_items):
            abs_index = start + i
            prefix = "♥️" if abs_index == self.selected_index else f"{abs_index + 1}."
            name = panel_text(song["name"]).replace("`", "'")
            ar = panel_text((song.get("ar") or [{}])[0].get("name", "未知")).replace("`", "'")
            entry = f"{prefix} {name} - {ar}"
            clean_entry = entry[:35] + "..." if len(entry) > 35 else entry
            lines.append(clean_entry)

        list_content = "```md\n" + "\n".join(lines) + "\n```"

        options = []
        for i, song in enumerate(current_items):
            abs_index = start + i
            label = f"{abs_index + 1}. {song['name']}"[:20]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(abs_index),
                    default=(self.selected_index == abs_index),
                )
            )

        btn_prev = ui.Button(
            emoji="⬅️", style=discord.ButtonStyle.secondary, disabled=(self.page == 0)
        )
        btn_prev.callback = self.on_prev

        btn_page = ui.Button(
            label=f"{self.page + 1} / {total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )

        btn_next = ui.Button(
            emoji="➡️",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= total_pages - 1),
        )
        btn_next.callback = self.on_next

        confirm_label = "请先选择"
        confirm_style = discord.ButtonStyle.secondary
        confirm_disabled = True

        if self.selected_index is not None:
            confirm_label = "插队下一首"
            confirm_style = discord.ButtonStyle.success
            confirm_disabled = False

        select_menu = ui.Select(
            placeholder="选择歌曲进行操作...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"q_sel_{time.time()}",
        )
        select_menu.callback = self.on_select_option

        btn_confirm = ui.Button(
            label=confirm_label,
            style=confirm_style,
            emoji="🔝",
            disabled=confirm_disabled,
        )
        btn_confirm.callback = self.on_confirm

        btn_remove = ui.Button(label="移除所选", emoji="🗑️",
                               style=discord.ButtonStyle.danger, disabled=confirm_disabled)
        btn_remove.callback = self.on_remove
        inset_content = "\n-# 可插队或移除待播歌曲；不会影响当前播放"
        container = ui.Container(
            ui.TextDisplay(content=f"### 📀 播放列表 - 共 {len(self.queue)} 首"),
            ui.TextDisplay(content=f"{list_content}{inset_content}"),
            ui.Separator(),
            ui.ActionRow(select_menu),
            ui.ActionRow(btn_prev, btn_page, btn_next, btn_confirm, btn_remove),
            accent_color=discord.Color.from_rgb(255, 95, 95),
        )
        self.add_item(container)

    async def on_select_option(self, interaction: discord.Interaction):
        val = int(interaction.data["values"][0])
        self.selected_index = val
        self._build_view()
        await interaction.response.edit_message(view=self)

    async def on_prev(self, interaction: discord.Interaction):
        self.page -= 1
        self.selected_index = None
        self._build_view()
        await interaction.response.edit_message(view=self)

    async def on_next(self, interaction: discord.Interaction):
        self.page += 1
        self.selected_index = None
        self._build_view()
        await interaction.response.edit_message(view=self)

    async def on_remove(self, interaction: discord.Interaction):
        if self.selected_index is None:
            return await interaction.response.defer()
        selected = self.queue[self.selected_index]
        await interaction.response.defer()
        removed = await self.cog.remove_pending_songs(self.guild_id, [selected])
        self.queue = list(self.cog.queues.get(self.guild_id, {}).get("queue", []))
        self.selected_index = None
        self._build_view()
        await interaction.edit_original_response(view=self)
        await interaction.followup.send(
            "已移除所选待播歌曲。" if removed else "歌曲已开始播放或不在队列中，未作更改。",
            ephemeral=True,
        )

    async def on_confirm(self, interaction: discord.Interaction):
        if self.selected_index is None:
            return await interaction.response.defer()

        selected_index = self.selected_index
        selected_song = self.queue[selected_index]
        await interaction.response.defer()
        song_name = await self.cog.prioritize_song(
            interaction, selected_index, expected_song=selected_song,
        )
        if song_name:
            self.clear_items()
            res_container = ui.Container(
                ui.TextDisplay(content="### ✅ 插队成功"),
                ui.TextDisplay(content=f"已将 **{panel_text(song_name)}** 设为下一首播放。"),
                accent_colour=discord.Color.blue(),
            )
            self.add_item(res_container)
            await interaction.edit_original_response(view=self)
        else:
            await interaction.followup.send(
                "❌ 操作失败，队列可能已变更", ephemeral=True
            )


class ClearQueueView(BaseView):
    def __init__(self, cog, guild_id, user_id, songs):
        super().__init__(timeout=60)
        self.cog, self.guild_id, self.user_id = cog, guild_id, user_id
        self.songs = list(songs)
        self.submitted = False
        confirm = ui.Button(label="确认清空待播", style=discord.ButtonStyle.danger)
        cancel = ui.Button(label="保留队列", style=discord.ButtonStyle.secondary)
        confirm.callback, cancel.callback = self.confirm, self.cancel
        self.add_item(ui.Container(
            ui.TextDisplay(content=f"### 清空这 {len(self.songs)} 首待播歌曲？"),
            ui.TextDisplay(content="当前歌曲继续播放；打开此确认后新加入的歌曲会保留。"),
            ui.ActionRow(confirm, cancel), accent_colour=discord.Color.orange(),
        ))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("请自己打开队列管理面板。", ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    async def confirm(self, interaction):
        if self.submitted:
            return await interaction.response.defer()
        self.submitted = True
        await interaction.response.defer()
        count = await self.cog.remove_pending_songs(self.guild_id, self.songs)
        await self.finish(interaction, f"已移除 {count} 首待播歌曲，当前播放不受影响。")

    async def cancel(self, interaction):
        if self.submitted:
            return await interaction.response.defer()
        self.submitted = True
        await interaction.response.defer()
        await self.finish(interaction, "已保留队列。")

    async def finish(self, interaction, text):
        self.clear_items()
        self.add_item(ui.Container(ui.TextDisplay(content=text)))
        await interaction.edit_original_response(view=self)
        self.stop()


class MusicOptionsView(BaseView):
    def __init__(self, controls, guild_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        clear = ui.Button(label="清空待播", emoji="🗑️", style=discord.ButtonStyle.secondary)
        refresh = ui.Button(label="刷新面板", emoji="🔄", style=discord.ButtonStyle.secondary)
        clear.callback, refresh.callback = controls.callback_clear, controls.callback_refresh
        self.add_item(ui.Container(
            ui.TextDisplay(content="### 队列管理"),
            ui.TextDisplay(content="清空待播会保留当前歌曲；主面板的停止键会清空队列并离开语音。"),
            ui.ActionRow(clear, refresh),
        ))


class MusicInterface(BaseView):
    def __init__(self, bot, guild_id: int = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id

        try:
            self._build_container()
        except Exception as e:
            print(f"MusicInterface Init Error: {e}")

    # --- 按钮回调 ---
    async def callback_list(self, interaction: discord.Interaction):
        queue_data = self.get_queue(interaction.guild_id)
        cog = self.bot.get_cog("Music")
        empty_list = queue_data["queue"] if queue_data else []
        if cog:
            await interaction.response.send_message(
                view=QueueListView(cog, interaction.guild_id, empty_list),
                ephemeral=True,
            )

    async def callback_pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        queue_data = self.get_queue(interaction.guild_id)
        cog = self.bot.get_cog("Music")

        if vc and vc.is_playing():
            if queue_data:
                elapsed = time.time() - (queue_data.get("start_time") or time.time())
                queue_data["paused_elapsed"] = (
                    queue_data.get("paused_elapsed", 0) + elapsed
                )
                queue_data["start_time"] = None
            vc.pause()
            if cog:
                cog._stop_progress_task(interaction.guild_id)
                if queue_data:
                    queue_data["agent_phase"] = "paused"
        elif vc and vc.is_paused():
            if queue_data:
                queue_data["start_time"] = time.time()
            vc.resume()
            if cog:
                cog._start_progress_task(interaction.guild_id)
                if queue_data:
                    queue_data["agent_phase"] = "playing"
        else:
            return await interaction.response.send_message("❌ 无播放", ephemeral=True)

        await interaction.response.defer()
        if cog:
            await cog.update_player_ui(interaction.guild_id)

    async def callback_skip(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Music")
        data = self.get_queue(interaction.guild_id) or {}
        selected = self.current_song if self.guild_id else data.get("current")
        await interaction.response.defer()
        if not cog or not await cog.skip_current(interaction.guild_id, selected):
            await interaction.followup.send("歌曲已切换，或语音未连接；请刷新面板。", ephemeral=True)

    async def callback_clear(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Music")
        songs = (self.get_queue(interaction.guild_id) or {}).get("queue", [])
        if not cog or not songs:
            return await interaction.response.send_message("没有待播歌曲。", ephemeral=True)
        await interaction.response.send_message(
            view=ClearQueueView(cog, interaction.guild_id, interaction.user.id, songs), ephemeral=True,
        )

    async def callback_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cog = self.bot.get_cog("Music")
        if cog:
            await cog.update_player_ui(interaction.guild_id)

    async def callback_more(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=MusicOptionsView(self, interaction.guild_id), ephemeral=True)

    async def callback_lyrics(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Music")
        await interaction.response.defer()
        if not cog:
            return
        data = cog._get_or_create_queue(interaction.guild_id)
        data["lyrics_enabled"] = not data.get("lyrics_enabled", True)
        task = data.get("lyrics_task")
        if task and not task.done():
            task.cancel()
        if data["lyrics_enabled"] and data.get("current"):
            cog._begin_lyrics(interaction.guild_id, data["current"])
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            cog._start_progress_task(interaction.guild_id)
        await cog.update_player_ui(interaction.guild_id)

    async def callback_stop(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Music")
        await interaction.response.defer()
        if cog:
            await cog.stop_handling(interaction.guild_id)

    async def callback_mode(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Music")
        if not cog:
            return await interaction.response.send_message(
                "\u64ad\u653e\u5668\u6682\u65f6\u4e0d\u53ef\u7528", ephemeral=True
            )

        await interaction.response.defer()
        cog.toggle_play_mode(interaction.guild_id)
        await cog.update_player_ui(interaction.guild_id)

    async def callback_add(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Music")
        if cog:
            await interaction.response.send_modal(
                SearchModal(cog, interaction.guild_id)
            )

    def get_queue(self, guild_id):
        cog = self.bot.get_cog("Music")
        if cog and guild_id and guild_id in cog.queues:
            return cog.queues[guild_id]
        return None

    def update_container(self, guild_id: int = None):
        if guild_id:
            self.guild_id = guild_id
        self._build_container()

    def _build_progress_bar(self, guild_id, duration_ms):
        queue_data = self.get_queue(guild_id)
        if not queue_data:
            return "- 正在准备播放\n> 时长未知"
        if queue_data.get("agent_phase") == "loading":
            return "-# 正在获取音频，可用 ⏭ 跳过"
        if queue_data.get("agent_phase") == "error":
            return "-# 本曲播放失败 · 重新点歌或 ⏭ 跳过"

        vc = (
            self.bot.get_guild(guild_id).voice_client
            if guild_id and self.bot.get_guild(guild_id)
            else None
        )
        is_paused = vc.is_paused() if vc else False

        start_time = queue_data.get("start_time")
        paused_elapsed = queue_data.get("paused_elapsed", 0)

        if start_time:
            elapsed_sec = paused_elapsed + max(0, time.time() - start_time)
        else:
            elapsed_sec = paused_elapsed

        def fmt(s):
            m, s = divmod(int(s), 60)
            return f"{m}:{s:02d}"

        if not duration_ms or duration_ms <= 0:
            status = PAUSE if is_paused else KNOB
            return f"{status}\n-# {fmt(elapsed_sec)} / --:--"

        total_sec = duration_ms / 1000
        elapsed_sec = min(elapsed_sec, total_sec)
        progress = elapsed_sec / total_sec if total_sec > 0 else 0

        bar_len = 8
        filled = min(bar_len - 1, int(progress * bar_len))
        bar = (
            PAUSE
            if is_paused
            else (FILL * filled + KNOB + FILL * (bar_len - filled - 1))
        )

        return f"{bar}\n-# {fmt(elapsed_sec)} / {fmt(total_sec)}"

    def _lyric_text(self, data, current):
        if not current or not data.get("lyrics_enabled", True):
            return ""
        lines = current.get("lyrics") or []
        if not lines:
            if current.get("lyrics_state") == "loading":
                return "-# 正在获取歌词…"
            if current.get("lyrics_state") in {"missing", "error"}:
                return "-# 暂无可用逐句歌词"
            return ""
        elapsed = data.get("paused_elapsed", 0)
        if data.get("start_time"):
            elapsed += max(0, time.time() - data["start_time"])
        previous, active, following = lyric_window(lines, elapsed)
        rows = []
        if previous:
            rows.append(f"-# {panel_text(previous, 95)}")
        rows.append(f"**{panel_text(active, 110)}**")
        if following:
            rows.append(f"-# {panel_text(following, 95)}")
        return "\n".join(rows)

    def _build_container(self):
        data = self.get_queue(self.guild_id) or {}
        current, pending = data.get("current"), data.get("queue", [])
        self.current_song = current
        guild = self.bot.get_guild(self.guild_id) if self.guild_id else None
        vc = guild.voice_client if guild else None
        connected = bool(vc and vc.is_connected())
        playing, paused = bool(vc and vc.is_playing()), bool(vc and vc.is_paused())
        phase = "paused" if paused else "playing" if playing else data.get("agent_phase", "idle")
        if not connected and current:
            phase = "disconnected"
        status, color = {
            "playing": ("正在播放", 0xB0C6C1),
            "paused": ("已暂停", 0xD5C5A1),
            "loading": ("正在加载", 0xAFBDD3),
            "error": ("播放遇到问题", 0xD9ADA2),
            "stopping": ("正在停止", 0xB6B7BD),
            "disconnected": ("语音已断开", 0xB6B7BD),
        }.get(phase, ("音乐时光", 0xDBC0CB))
        cover = DEFAULT_COVER_URL
        if current:
            metadata = f"### {panel_text(current.get('name'), 100)}"
            artist = panel_text(" / ".join(str(item.get("name", "未知")) for item in (current.get("ar") or [{}])), 100)
            metadata += f"\n{artist}"
            album = (current.get("al") or {}).get("name")
            if album and album != "未知":
                metadata += f"\n-# {panel_text(album, 70)}"
            picture = (current.get("al") or {}).get("picUrl")
            if isinstance(picture, str) and picture.startswith(("https://", "http://")) and len(picture) < 1800:
                cover = picture
            progress = self._build_progress_bar(self.guild_id, current.get("dt", 0))
            if not connected:
                progress = "-# 语音已断开 · 重新点歌可连接"
        else:
            metadata = "### 今天想听什么？\n-# 歌名、歌单、专辑，或音频链接"
            progress = ""
        self.uses_default_cover = cover == DEFAULT_COVER_URL

        mode = data.get("play_mode", "sequential")
        cog = self.bot.get_cog("Music")
        next_song = cog._preview_next_song(self.guild_id) if pending and cog else None
        queue_line = ""
        if next_song:
            queue_line = f"-# 下一首 · {panel_text(next_song.get('name'), 65)}"
        durations = [item.get("dt") or 0 for item in pending]
        total = sum(max(0, value) for value in durations)
        estimate = duration_text(total) if total else "时长未知"
        if total and any(value <= 0 for value in durations):
            estimate = "至少 " + estimate
        footer = f"{'随机播放' if mode == 'shuffle' else '顺序播放'} · 待播 {len(pending)} 首"
        if pending:
            footer += f" · {estimate}"

        btn_add = ui.Button(label="点歌", emoji=ADD, style=discord.ButtonStyle.primary, custom_id="music:add")
        btn_add.callback = self.callback_add
        btn_pause = ui.Button(emoji="▶️" if paused else "⏸️", style=discord.ButtonStyle.secondary,
                              custom_id="music:pause", disabled=not (playing or paused))
        btn_pause.callback = self.callback_pause
        btn_skip = ui.Button(emoji="⏭️", style=discord.ButtonStyle.secondary,
                             custom_id="music:skip", disabled=not (current and connected))
        btn_skip.callback = self.callback_skip
        btn_mode = ui.Button(emoji="🔀" if mode == "shuffle" else "➡️", style=discord.ButtonStyle.secondary,
                             custom_id="music:mode")
        btn_mode.callback = self.callback_mode
        btn_stop = ui.Button(emoji="⏹️", style=discord.ButtonStyle.secondary,
                             custom_id="music:stop", disabled=not (current or pending or connected))
        btn_stop.callback = self.callback_stop
        btn_list = ui.Button(label=f"队列 · {len(pending)}", style=discord.ButtonStyle.secondary, custom_id="music:list")
        btn_list.callback = self.callback_list
        btn_lyrics = ui.Button(label="歌词 · 开" if data.get("lyrics_enabled", True) else "歌词 · 关",
                               style=discord.ButtonStyle.secondary, custom_id="music:lyrics")
        btn_lyrics.callback = self.callback_lyrics
        btn_more = ui.Button(label="更多", style=discord.ButtonStyle.secondary, custom_id="music:more")
        btn_more.callback = self.callback_more

        contents = [ui.TextDisplay(content=f"-# {status}")]
        if cover:
            contents.append(ui.Section(ui.TextDisplay(content=metadata), accessory=ui.Thumbnail(media=cover)))
        else:
            contents.append(ui.TextDisplay(content=metadata))
        if progress:
            contents.append(ui.TextDisplay(content=progress))
        lyrics = self._lyric_text(data, current)
        if lyrics:
            contents.extend([ui.Separator(spacing=discord.SeparatorSpacing.small), ui.TextDisplay(content=lyrics)])
        contents.extend([
            ui.ActionRow(btn_add, btn_pause, btn_skip, btn_mode, btn_stop),
            ui.ActionRow(btn_list, btn_lyrics, btn_more),
        ])
        if queue_line:
            contents.append(ui.TextDisplay(content=queue_line))
        contents.append(ui.TextDisplay(content=f"-# {footer}"))
        self.clear_items()
        self.add_item(ui.Container(*contents, accent_colour=discord.Color(color)))
