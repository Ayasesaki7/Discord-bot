from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from chat.agent.music_tools import MusicToolError, MusicToolHost


def song(name: str) -> dict[str, object]:
    return {
        "name": name,
        "ar": [{"name": "Artist"}],
        "dt": 180_000,
        "requester": "tester",
    }


class FakeVoiceClient:
    def __init__(self, channel) -> None:
        self.channel = channel
        self.connected = True
        self.playing = False
        self.paused = False
        self.stopped = False

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing

    def is_paused(self) -> bool:
        return self.paused

    def pause(self) -> None:
        self.playing = False
        self.paused = True

    def resume(self) -> None:
        self.playing = True
        self.paused = False

    def stop(self) -> None:
        self.playing = False
        self.paused = False
        self.stopped = True


class FakeMusic:
    def __init__(self, guild) -> None:
        self.guild = guild
        self.queues: dict[int, dict[str, object]] = {}
        self.agent_locks: dict[int, asyncio.Lock] = {}
        self.progress_stopped = False
        self.progress_started = False

    def _get_or_create_queue(self, guild_id: int):
        return self.queues.setdefault(
            guild_id,
            {
                "current": None,
                "queue": [],
                "channel": None,
                "play_mode": "sequential",
                "priority_next": None,
                "start_time": None,
                "paused_elapsed": 0,
                "stopping": False,
                "agent_phase": "idle",
                "agent_last_error": None,
                "agent_state_version": 0,
                "agent_last_transition": None,
            },
        )

    def _drop_stale_voice_client(self, _voice_client) -> None:
        return None

    def _normalize_direct_audio_url(self, _query: str):
        return None

    def _search_song(self, query: str):
        return song(query) if query != "missing" else None

    def _copy_song_for_queue(self, value, requester: str):
        copied = dict(value)
        copied["requester"] = requester
        return copied

    async def _ensure_voice_client_for(self, guild, member):
        guild.voice_client = FakeVoiceClient(member.voice.channel)
        return guild.voice_client

    async def _play_music_task(self, _guild_id: int, _song) -> None:
        return None

    async def _preload_next(self, _guild_id: int) -> None:
        return None

    async def update_player_ui(self, _guild_id: int) -> None:
        return None

    def _stop_progress_task(self, _guild_id: int) -> None:
        self.progress_stopped = True

    def _start_progress_task(self, _guild_id: int) -> None:
        self.progress_started = True

    async def stop_handling(self, guild_id: int) -> None:
        queue = self._get_or_create_queue(guild_id)
        queue["current"] = None
        queue["queue"] = []
        queue["agent_phase"] = "disconnected"
        self.guild.voice_client = None


class FakeBot:
    def __init__(self, music) -> None:
        self.music = music

    def get_cog(self, name: str):
        return self.music if name == "Music" else None


class MusicToolHostTests(unittest.IsolatedAsyncioTestCase):
    def make_host(self, *, user_channel=True, bot_channel=None):
        voice_channel = SimpleNamespace(id=10, name="Music")
        requester_channel = voice_channel if user_channel else None
        author = SimpleNamespace(
            id=20,
            display_name="tester",
            voice=(
                SimpleNamespace(channel=requester_channel)
                if requester_channel is not None
                else None
            ),
        )
        guild = SimpleNamespace(id=30, voice_client=None)
        if bot_channel is not None:
            guild.voice_client = FakeVoiceClient(bot_channel)
        music = FakeMusic(guild)
        message = SimpleNamespace(
            guild=guild,
            author=author,
            channel=SimpleNamespace(id=40),
        )
        return MusicToolHost(bot=FakeBot(music), message=message), music, guild

    async def test_status_exposes_state_machine_and_allowed_actions(self) -> None:
        host, _music, _guild = self.make_host()

        result = await host.execute("status", {})
        payload = json.loads(str(result["content"]))

        self.assertEqual(payload["state"], "disconnected")
        self.assertIn("add", payload["allowedActions"])
        self.assertIn("status", payload["allowedActions"])
        self.assertEqual(payload["queue"], [])

    async def test_add_uses_requester_voice_and_authoritative_queue(self) -> None:
        host, music, guild = self.make_host()

        result = await host.execute(
            "add",
            {"query": "Song A", "position": "end"},
        )
        await asyncio.sleep(0)
        payload = json.loads(str(result["content"]))
        queue = music.queues[guild.id]

        self.assertEqual(payload["state"], "loading")
        self.assertEqual(payload["current"]["title"], "Song A")
        self.assertEqual(queue["current"]["name"], "Song A")
        self.assertEqual(payload["lastTransition"]["action"], "add")
        self.assertEqual(payload["stateVersion"], 1)

    async def test_pending_queue_is_one_based_for_move_and_remove(self) -> None:
        host, music, guild = self.make_host()
        queue = music._get_or_create_queue(guild.id)
        queue["current"] = song("Current")
        queue["queue"] = [song("A"), song("B"), song("C")]
        guild.voice_client = FakeVoiceClient(host.message.author.voice.channel)
        guild.voice_client.playing = True

        moved = await host.execute("move_next", {"queue_index": 3})
        moved_payload = json.loads(str(moved["content"]))
        removed = await host.execute("remove", {"queue_index": 2})
        removed_payload = json.loads(str(removed["content"]))

        self.assertEqual([item["title"] for item in moved_payload["queue"]], ["C", "A", "B"])
        self.assertEqual([item["title"] for item in removed_payload["queue"]], ["C", "B"])

    async def test_mutations_require_same_voice_channel(self) -> None:
        requester_channel = SimpleNamespace(id=10, name="Requester")
        bot_channel = SimpleNamespace(id=11, name="Other")
        host, _music, guild = self.make_host(bot_channel=bot_channel)
        host.message.author.voice.channel = requester_channel
        guild.voice_client.channel = bot_channel

        with self.assertRaisesRegex(MusicToolError, "same voice channel"):
            await host.execute("pause", {})

    async def test_pause_resume_and_stop_follow_valid_transitions(self) -> None:
        host, music, guild = self.make_host()
        queue = music._get_or_create_queue(guild.id)
        queue["current"] = song("Current")
        queue["start_time"] = 1.0
        guild.voice_client = FakeVoiceClient(host.message.author.voice.channel)
        guild.voice_client.playing = True

        paused = json.loads(str((await host.execute("pause", {}))["content"]))
        resumed = json.loads(str((await host.execute("resume", {}))["content"]))
        stopped = json.loads(str((await host.execute("stop", {}))["content"]))

        self.assertEqual(paused["state"], "paused")
        self.assertEqual(resumed["state"], "playing")
        self.assertEqual(stopped["state"], "disconnected")
        self.assertEqual(stopped["stateVersion"], 3)

    async def test_model_cannot_supply_cross_guild_or_voice_ids(self) -> None:
        host, _music, _guild = self.make_host()

        with self.assertRaisesRegex(MusicToolError, "unsupported music arguments"):
            await host.execute(
                "add",
                {"query": "Song", "guild_id": "999", "voice_channel_id": "888"},
            )


if __name__ == "__main__":
    unittest.main()
