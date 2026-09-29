from __future__ import annotations

import asyncio
import json
import time
from music.access import in_voice_chat


_MAX_AGENT_QUEUE_LENGTH = 200
_ACTION_ARGUMENTS = {
    "status": set(),
    "queue": set(),
    "add": {"query", "position"},
    "pause": set(),
    "resume": set(),
    "skip": set(),
    "stop": set(),
    "remove": {"queue_index"},
    "clear_queue": set(),
    "move_next": {"queue_index"},
    "set_mode": {"mode"},
}


class MusicToolError(RuntimeError):
    pass


class MusicToolHost:
    """Current-guild bridge to the bot's one authoritative music queue.

    The model never supplies a guild, member, text channel, or voice channel
    id. Mutations are bound to the Discord message author and require that user
    to be in the same voice channel as the bot. The state machine is derived
    from the existing Music Cog voice client and queue so the Agent and panel
    cannot drift into separate playback states.
    """

    def __init__(self, *, bot, message) -> None:
        self.bot = bot
        self.message = message

    async def execute(
        self,
        action: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        normalized_action = str(action or "").strip().casefold()
        allowed_arguments = _ACTION_ARGUMENTS.get(normalized_action)
        if allowed_arguments is None:
            raise MusicToolError(f"unsupported music action: {normalized_action}")
        unknown_arguments = sorted(set(arguments) - allowed_arguments)
        if unknown_arguments:
            raise MusicToolError(
                "unsupported music arguments: " + ", ".join(unknown_arguments)
            )

        guild = getattr(self.message, "guild", None)
        if guild is None:
            raise MusicToolError("music control is unavailable outside a guild")
        music = self.bot.get_cog("Music")
        if music is None:
            raise MusicToolError("the bot music module is not loaded")

        locks = getattr(music, "agent_locks", None)
        if not isinstance(locks, dict):
            locks = {}
            music.agent_locks = locks
        lock = locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            before_state = self._state_name(music, guild)
            if normalized_action == "status":
                summary = f"Music state is {before_state}."
            elif normalized_action == "queue":
                queue_length = len((music.queues.get(guild.id) or {}).get("queue", []))
                summary = f"Read {queue_length} pending music queue item(s)."
            else:
                summary = await self._mutate(
                    music,
                    guild,
                    normalized_action,
                    arguments,
                )
                self._record_transition(
                    music,
                    guild,
                    action=normalized_action,
                    previous=before_state,
                )

            payload = self._snapshot(music, guild)
            return {
                "summary": summary,
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "truncated": False,
            }

    async def _mutate(
        self,
        music,
        guild,
        action: str,
        arguments: dict[str, object],
    ) -> str:
        _requester_channel, voice_client = self._require_same_voice(music, guild)
        queue_data = music._get_or_create_queue(guild.id)

        if action == "add":
            query = self._required_string(arguments, "query", max_length=500)
            position = str(arguments.get("position") or "end").strip().casefold()
            if position not in {"end", "next"}:
                raise MusicToolError("position must be end or next")
            failed = queue_data.get("playback_failed") or queue_data.get("agent_phase") == "error"
            total_items = len(queue_data["queue"]) + bool(queue_data.get("current") and not failed)
            if total_items >= _MAX_AGENT_QUEUE_LENGTH:
                raise MusicToolError(
                    f"the Agent music queue is capped at {_MAX_AGENT_QUEUE_LENGTH} tracks"
                )
            # Searching for another song must not overwrite the active track's
            # failure/loading state; enqueue_songs needs it to recover playback.
            direct_url = music._normalize_direct_audio_url(query)
            if direct_url:
                song = music._direct_audio_song(
                    direct_url,
                    self.message.author.display_name,
                )
            else:
                song = await asyncio.to_thread(music._search_song, query)
                if song is not None:
                    song = music._copy_song_for_queue(
                        song,
                        self.message.author.display_name,
                    )
            if not song:
                raise MusicToolError(f"no song was found for: {query}")

            # Do not connect to a different voice if the user moved during search.
            _requester_channel, voice_client = self._require_same_voice(music, guild)
            if voice_client is None or not voice_client.is_connected():
                queue_data["agent_phase"] = "connecting"
                try:
                    voice_client = await music._ensure_voice_client_for(
                        guild,
                        self.message.author,
                        expected_channel_id=_requester_channel.id,
                    )
                except Exception as exc:
                    queue_data["agent_phase"] = "error"
                    queue_data["agent_last_error"] = exc.__class__.__name__
                    raise MusicToolError("failed to connect to the requester's voice channel") from exc
            if voice_client is None or not voice_client.is_connected():
                queue_data["agent_phase"] = "error"
                queue_data["agent_last_error"] = "voice connection unavailable"
                raise MusicToolError("voice connection is unavailable")

            # Search may take seconds: revalidate voice membership at mutation time.
            self._require_same_voice(music, guild)
            queue_data["channel"] = voice_client.channel
            queue_data["stopping"] = False
            result = music.enqueue_songs(guild.id, [song], position=position)
            if not result["added"]:
                raise MusicToolError("music queue is full")
            disposition = "starting now" if queue_data.get("current") is song else (
                "queued next" if position == "next" else "added to the queue")
            if result["recovered"]:
                disposition += "; playback resumed after the previous failure"
            await music.update_player_ui(guild.id)
            return f"{self._song_label(song)} was {disposition}."

        if action == "pause":
            if voice_client is None or not voice_client.is_playing():
                raise MusicToolError("pause is allowed only while music is playing")
            elapsed = time.time() - (queue_data.get("start_time") or time.time())
            queue_data["paused_elapsed"] = queue_data.get("paused_elapsed", 0) + elapsed
            queue_data["start_time"] = None
            queue_data["agent_phase"] = "paused"
            voice_client.pause()
            music._stop_progress_task(guild.id)
            await music.update_player_ui(guild.id)
            return "Paused the current track."

        if action == "resume":
            if voice_client is None or not voice_client.is_paused():
                raise MusicToolError("resume is allowed only while music is paused")
            queue_data["start_time"] = time.time()
            queue_data["agent_phase"] = "playing"
            voice_client.resume()
            music._start_progress_task(guild.id)
            await music.update_player_ui(guild.id)
            return "Resumed the current track."

        if action == "skip":
            if not await music.skip_current(guild.id, queue_data.get("current")):
                raise MusicToolError("skip requires a current track and a connected voice channel")
            return "Skipped the current track."

        if action == "stop":
            if not queue_data.get("current") and not queue_data["queue"] and voice_client is None:
                raise MusicToolError("the music player is already stopped")
            await music.stop_handling(guild.id)
            return "Stopped playback, cleared the queue, and disconnected voice."

        if action == "remove":
            index = self._queue_index(arguments, queue_data)
            song = queue_data["queue"].pop(index)
            if queue_data.get("priority_next") is song:
                queue_data["priority_next"] = None
            await music.update_player_ui(guild.id)
            return f"Removed {self._song_label(song)} from queue position {index + 1}."

        if action == "clear_queue":
            removed = len(queue_data["queue"])
            queue_data["queue"].clear()
            queue_data["priority_next"] = None
            await music.update_player_ui(guild.id)
            return f"Cleared {removed} pending track(s); the current track was left playing."

        if action == "move_next":
            index = self._queue_index(arguments, queue_data)
            song = queue_data["queue"].pop(index)
            queue_data["queue"].insert(0, song)
            queue_data["priority_next"] = song
            asyncio.create_task(music._preload_next(guild.id))
            await music.update_player_ui(guild.id)
            return f"Moved {self._song_label(song)} to play next."

        if action == "set_mode":
            mode = str(arguments.get("mode") or "").strip().casefold()
            if mode not in {"sequential", "shuffle"}:
                raise MusicToolError("mode must be sequential or shuffle")
            queue_data["play_mode"] = mode
            await music.update_player_ui(guild.id)
            return f"Playback mode is now {mode}."

        raise MusicToolError(f"unsupported music mutation: {action}")

    def _require_same_voice(self, music, guild):
        voice_state = getattr(self.message.author, "voice", None)
        requester_channel = getattr(voice_state, "channel", None)
        if requester_channel is None:
            raise MusicToolError("join a voice channel before controlling music")
        voice_client = getattr(guild, "voice_client", None)
        music._drop_stale_voice_client(voice_client)
        voice_client = getattr(guild, "voice_client", None)
        if (
            voice_client is not None
            and voice_client.is_connected()
            and voice_client.channel != requester_channel
        ):
            raise MusicToolError(
                "the requester must join the same voice channel as the bot"
            )
        if not in_voice_chat(getattr(self.message, 'channel', None), requester_channel):
            raise MusicToolError('请在你当前加入的语音频道的文字聊天区点歌或控制播放；普通文字频道不能操作音乐。')
        return requester_channel, voice_client

    def _snapshot(self, music, guild) -> dict[str, object]:
        queue_data = music.queues.get(guild.id)
        queue_list = list((queue_data or {}).get("queue", []))
        current = (queue_data or {}).get("current")
        requester_voice = getattr(getattr(self.message.author, "voice", None), "channel", None)
        voice_client = getattr(guild, "voice_client", None)
        bot_voice = getattr(voice_client, "channel", None)
        state = self._state_name(music, guild)
        return {
            "state": state,
            "stateVersion": int((queue_data or {}).get("agent_state_version", 0)),
            "allowedActions": self._allowed_actions(
                state,
                queue_data,
                requester_voice=requester_voice,
                bot_voice=bot_voice,
                in_voice_chat=in_voice_chat(getattr(self.message, 'channel', None), requester_voice),
            ),
            "voiceChannel": (
                {"id": str(bot_voice.id), "name": str(bot_voice.name)}
                if bot_voice is not None
                else None
            ),
            "requesterVoiceChannel": (
                {"id": str(requester_voice.id), "name": str(requester_voice.name)}
                if requester_voice is not None
                else None
            ),
            "current": self._song_view(current),
            "queue": [
                {"index": index, **(self._song_view(song) or {})}
                for index, song in enumerate(queue_list, start=1)
            ],
            "queueLength": len(queue_list),
            "playMode": str((queue_data or {}).get("play_mode", "sequential")),
            "lastError": (
                str((queue_data or {}).get("agent_last_error") or "") or None
            ),
            "lastTransition": (queue_data or {}).get("agent_last_transition"),
        }

    @staticmethod
    def _state_name(music, guild) -> str:
        queue_data = music.queues.get(guild.id)
        voice_client = getattr(guild, "voice_client", None)
        if queue_data and queue_data.get("stopping"):
            return "stopping"
        if voice_client is not None and voice_client.is_connected():
            if voice_client.is_paused():
                return "paused"
            if voice_client.is_playing():
                return "playing"
        if queue_data and queue_data.get("current"):
            if queue_data.get("agent_phase") == "error":
                return "error"
            return "loading" if voice_client is not None else "error"
        if voice_client is not None and voice_client.is_connected():
            return "idle"
        return "disconnected"

    @staticmethod
    def _allowed_actions(
        state: str,
        queue_data,
        *,
        requester_voice,
        bot_voice,
        in_voice_chat: bool,
    ) -> list[str]:
        actions = ["status", "queue"]
        same_voice = requester_voice is not None and (
            bot_voice is None or requester_voice == bot_voice
        )
        if not same_voice or not in_voice_chat:
            return actions
        actions.extend(["add", "set_mode"])
        pending = list((queue_data or {}).get("queue", []))
        if pending:
            actions.extend(["remove", "clear_queue", "move_next"])
        if state == "playing":
            actions.extend(["pause", "skip", "stop"])
        elif state == "paused":
            actions.extend(["resume", "skip", "stop"])
        elif state in {"loading", "error", "idle"} and (
            (queue_data or {}).get("current") or pending or bot_voice is not None
        ):
            actions.append("stop")
            if (queue_data or {}).get("current") and bot_voice is not None:
                actions.append("skip")
        return actions

    def _record_transition(
        self,
        music,
        guild,
        *,
        action: str,
        previous: str,
    ) -> None:
        queue_data = music._get_or_create_queue(guild.id)
        current = self._state_name(music, guild)
        version = int(queue_data.get("agent_state_version", 0)) + 1
        queue_data["agent_state_version"] = version
        queue_data["agent_last_transition"] = {
            "action": action,
            "from": previous,
            "to": current,
            "version": version,
        }

    @staticmethod
    def _song_view(song) -> dict[str, object] | None:
        if not isinstance(song, dict):
            return None
        artists = ", ".join(
            str(artist.get("name") or "").strip()
            for artist in song.get("ar", [])
            if isinstance(artist, dict) and str(artist.get("name") or "").strip()
        )
        duration_ms = song.get("dt")
        return {
            "title": str(song.get("name") or "Unknown"),
            "artist": artists or "Unknown",
            "durationSeconds": (
                max(int(duration_ms), 0) // 1000
                if isinstance(duration_ms, int) and not isinstance(duration_ms, bool)
                else None
            ),
            "requester": str(song.get("requester") or "") or None,
        }

    @classmethod
    def _song_label(cls, song) -> str:
        view = cls._song_view(song) or {}
        return f"{view.get('title', 'Unknown')} - {view.get('artist', 'Unknown')}"

    @staticmethod
    def _required_string(
        arguments: dict[str, object],
        key: str,
        *,
        max_length: int,
    ) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MusicToolError(f"{key} must be a non-empty string")
        normalized = value.strip()
        if len(normalized) > max_length:
            raise MusicToolError(f"{key} exceeds {max_length} characters")
        return normalized

    @staticmethod
    def _queue_index(arguments: dict[str, object], queue_data) -> int:
        raw_index = arguments.get("queue_index")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            raise MusicToolError("queue_index must be a 1-based integer")
        index = raw_index - 1
        if index < 0 or index >= len(queue_data["queue"]):
            raise MusicToolError("queue_index is outside the current pending queue")
        return index
