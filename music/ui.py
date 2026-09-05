# cogs/music/music_v.py
import os

import discord
from discord import ui
import random, time, math

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


class BaseView(ui.LayoutView):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if getattr(interaction.client, 'is_globally_blacklisted', lambda _user_id: False)(interaction.user.id):
            return False
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ 需要先进入语音频道才能进行操作！", ephemeral=True
            )
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
        self.queue = queue_data
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
            name = song["name"]
            ar = (song.get("ar") or [{}])[0].get("name", "未知")
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

        inset_content = "\n-# 可选择歌曲插队"
        container = ui.Container(
            ui.TextDisplay(content=f"### 📀 播放列表 - 共 {len(self.queue)} 首"),
            ui.TextDisplay(content=f"{list_content}{inset_content}"),
            ui.Separator(),
            ui.ActionRow(select_menu),
            ui.ActionRow(btn_prev, btn_page, btn_next, btn_confirm),
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

    async def on_confirm(self, interaction: discord.Interaction):
        if self.selected_index is None:
            return await interaction.response.defer()

        song_name = await self.cog.prioritize_song(interaction, self.selected_index)
        if song_name:
            self.clear_items()
            res_container = ui.Container(
                ui.TextDisplay(content="### ✅ 插队成功"),
                ui.TextDisplay(content=f"已将 **{song_name}** 设为下一首播放。"),
                accent_colour=discord.Color.blue(),
            )
            self.add_item(res_container)
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message(
                "❌ 操作失败，队列可能已变更", ephemeral=True
            )


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
        elif vc and vc.is_paused():
            if queue_data:
                queue_data["start_time"] = time.time()
            vc.resume()
            if cog:
                cog._start_progress_task(interaction.guild_id)
        else:
            return await interaction.response.send_message("❌ 无播放", ephemeral=True)

        self.update_container(interaction.guild_id)
        await interaction.response.edit_message(view=self)

    async def callback_skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.defer()
        else:
            await interaction.response.send_message("❌ 未连接或未播放", ephemeral=True)

    async def callback_stop(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Music")
        if cog:
            await cog.stop_handling(interaction.guild_id)
        await interaction.response.defer()

    async def callback_mode(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Music")
        if not cog:
            return await interaction.response.send_message(
                "\u64ad\u653e\u5668\u6682\u65f6\u4e0d\u53ef\u7528", ephemeral=True
            )

        cog.toggle_play_mode(interaction.guild_id)
        self.update_container(interaction.guild_id)
        await interaction.response.edit_message(view=self)

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

        vc = (
            self.bot.get_guild(guild_id).voice_client
            if guild_id and self.bot.get_guild(guild_id)
            else None
        )
        is_paused = vc.is_paused() if vc else False

        start_time = queue_data.get("start_time")
        paused_elapsed = queue_data.get("paused_elapsed", 0)

        if start_time:
            elapsed_sec = paused_elapsed + (time.time() - start_time)
        else:
            elapsed_sec = paused_elapsed

        def fmt(s):
            m, s = divmod(int(s), 60)
            return f"{m}:{s:02d}"

        if not duration_ms or duration_ms <= 0:
            status = PAUSE if is_paused else KNOB
            return f"- {status}\n> {fmt(elapsed_sec)} / --:--"

        total_sec = duration_ms / 1000
        elapsed_sec = min(elapsed_sec, total_sec)
        progress = elapsed_sec / total_sec if total_sec > 0 else 0

        bar_len = 10
        filled = int(progress * bar_len)
        bar = (
            PAUSE
            if is_paused
            else (FILL * filled + KNOB + FILL * (bar_len - filled - 1))
        )

        return f"- {bar}\n> {fmt(elapsed_sec)} / {fmt(total_sec)}"

    def _build_container(self):
        target_id = self.guild_id
        title = "### \u97f3\u4e50\u7cfb\u7edf\u5df2\u542f\u52a8"
        artist_content = "```md\n# \u7b49\u5f85\u6307\u4ee4...\n```"
        cover_url = (
            "https://raw.githubusercontent.com/atr1official/atri_official/main/%E6%97%B6%E5%A4%8F&%E6%A0%97%E5%8E%9F/ATRI_2.png"
        )
        progress_str = "\u70b9\u51fb\u6309\u94ae\u5f00\u59cb\u70b9\u6b4c"
        queue_text = "- \u6ca1\u6709\u66f4\u591a\u6b4c\u66f2"
        footer_text = "\u7b49\u5f85\u70b9\u6b4c..."
        is_paused = False
        play_mode = "sequential"

        if target_id:
            queue_data = self.get_queue(target_id)
            guild = self.bot.get_guild(target_id)
            vc = guild.voice_client if guild else None
            is_paused = vc.is_paused() if vc else False

            if queue_data:
                play_mode = queue_data.get("play_mode", "sequential")

            if not queue_data or not queue_data.get("current"):
                title = random.choice(["> 慢慢听", "> 放空一下", "> 跟着音乐飘", "> 信号不足，心情正好"])
                artist_content = (
                    "```md\n# \u97f3\u6e90\u81ea QQ \u97f3\u4e50\n"
                    "> \u5f53\u524d\u4f7f\u7528\u514d\u767b\u5f55\u9002\u914d\n```"
                )
            else:
                current = queue_data["current"]
                title = f"### \U0001F3B5 {current['name']}"
                ar = (current.get("ar") or [{}])[0].get("name", "\u672a\u77e5")
                al = (current.get("al") or {}).get("name", "\u672a\u77e5")
                artist_content = (
                    f"```py\n'\u6b4c\u624b:' # {ar}\n'\u4e13\u8f91:' # {al[:15]}\n```"
                )

                pic = (current.get("al") or {}).get("picUrl")
                if pic:
                    cover_url = f"{pic}?param=300y300"

                progress_str = self._build_progress_bar(target_id, current.get("dt", 0))

                next_len = len(queue_data["queue"])
                if next_len > 0:
                    priority_song = queue_data.get("priority_next")
                    next_song = None
                    if priority_song is not None:
                        for queued_song in queue_data["queue"]:
                            if queued_song is priority_song:
                                next_song = queued_song
                                break

                    if next_song is None and (play_mode == "sequential" or next_len == 1):
                        next_song = queue_data["queue"][0]

                    if next_song is not None:
                        s_name = next_song["name"]
                        s_ar = (next_song.get("ar") or [{}])[0].get("name", "\u672a\u77e5")
                        queue_text = f"- \u5171 `{next_len}` \u9996\n> **NEXT >** {s_name} - {s_ar}"
                    else:
                        queue_text = (
                            f"- \u5171 `{next_len}` \u9996\n"
                            "> **SHUFFLE >** \u4e0b\u4e00\u9996\u5c06\u4ece\u961f\u5217\u4e2d\u968f\u673a\u62bd\u53d6"
                        )
                else:
                    queue_text = "- \u6ca1\u6709\u66f4\u591a\u6b4c\u66f2"

                footer_text = f"\u7531 {current.get('requester')} \u70b9\u6b4c"

        mode_text = "\u968f\u673a\u64ad\u653e" if play_mode == "shuffle" else "\u987a\u5e8f\u64ad\u653e"
        footer_text = f"{footer_text} | \u5f53\u524d\u6a21\u5f0f: {mode_text}" if footer_text else f"\u5f53\u524d\u6a21\u5f0f: {mode_text}"

        btn_list = ui.Button(
            emoji="\U0001F4C0", style=discord.ButtonStyle.primary, custom_id="music:list"
        )
        btn_list.callback = self.callback_list

        btn_pause = ui.Button(
            emoji="\u25B6\uFE0F" if is_paused else "\u23F8\uFE0F",
            style=(
                discord.ButtonStyle.success
                if is_paused
                else discord.ButtonStyle.secondary
            ),
            custom_id="music:pause",
        )
        btn_pause.callback = self.callback_pause

        btn_skip = ui.Button(
            emoji="\u23ED\uFE0F", style=discord.ButtonStyle.secondary, custom_id="music:skip"
        )
        btn_skip.callback = self.callback_skip

        btn_mode = ui.Button(
            label="\u968f\u673a" if play_mode == "shuffle" else "\u987a\u5e8f",
            emoji="\U0001F500",
            style=(
                discord.ButtonStyle.success
                if play_mode == "shuffle"
                else discord.ButtonStyle.secondary
            ),
            custom_id="music:mode",
        )
        btn_mode.callback = self.callback_mode

        btn_stop = ui.Button(
            emoji="\u23F9\uFE0F", style=discord.ButtonStyle.danger, custom_id="music:stop"
        )
        btn_stop.callback = self.callback_stop

        btn_add = ui.Button(
            label="\u70b9\u6b4c",
            emoji=ADD,
            style=discord.ButtonStyle.success,
            custom_id="music:add",
        )
        btn_add.callback = self.callback_add

        container = ui.Container(
            ui.TextDisplay(content=title),
            ui.Section(
                ui.TextDisplay(content=artist_content),
                accessory=ui.Thumbnail(media=cover_url),
            ),
            ui.Section(
                ui.TextDisplay(content=progress_str),
                accessory=btn_add,
            ),
            ui.Separator(),
            ui.ActionRow(btn_list, btn_pause, btn_skip, btn_mode, btn_stop),
            ui.Separator(),
            ui.TextDisplay(content=queue_text),
            ui.TextDisplay(content=f"-# {footer_text}" if footer_text else ""),
            accent_color=discord.Color.from_rgb(255, 95, 95),
        )

        self.clear_items()
        self.add_item(container)

