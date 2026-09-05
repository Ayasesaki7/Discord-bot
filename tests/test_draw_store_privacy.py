from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat.draw.agent import AtriDrawAgent
from chat.draw.models import ArtistString, CharacterProfile, LastGeneration, UserDrawProfile
from chat.draw.store import DrawStore


class DrawStorePrivacyTests(unittest.TestCase):
    def test_presets_are_scoped_but_artist_strings_follow_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DrawStore(Path(temp_dir) / "draw-memory.json")
            private_scope = "guild:100:channel:200"
            public_scope = "guild:101:channel:201"

            store.upsert_preset(
                300,
                private_scope,
                name="private-preset",
                positive_prefix="private visual detail",
            )
            store.upsert_artist_string(
                300,
                private_scope,
                name="private-artist",
                content="artist:private",
            )

            private_profile = store.get_user(300, private_scope)
            public_profile = store.get_user(300, public_scope)
            self.assertIn("private-preset", private_profile.presets)
            self.assertIn("private-artist", private_profile.artist_strings)
            self.assertNotIn("private-preset", public_profile.presets)
            self.assertIn("private-artist", public_profile.artist_strings)
            self.assertEqual(public_profile.active_artist, "private-artist")

    def test_user_without_artist_string_uses_owner_active_artist(self) -> None:
        user_profile = UserDrawProfile(user_id=300)
        owner_profile = UserDrawProfile(
            user_id=100,
            active_artist="owner-default",
            artist_strings={
                "owner-default": ArtistString(
                    name="owner-default",
                    content="artist:owner",
                )
            },
        )
        agent = object.__new__(AtriDrawAgent)

        selected = agent._select_artist_strings(
            user_profile,
            fallback_profile=owner_profile,
        )

        self.assertEqual([artist.name for artist in selected], ["owner-default"])

        user_profile.artist_strings["personal"] = ArtistString(
            name="personal",
            content="artist:personal",
        )
        selected_with_personal = agent._select_artist_strings(
            user_profile,
            fallback_profile=owner_profile,
        )
        self.assertEqual(selected_with_personal, [])

    def test_previous_generation_does_not_cross_channels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DrawStore(Path(temp_dir) / "draw-memory.json")
            private_scope = "guild:100:channel:200"
            other_scope = "guild:100:channel:201"
            generation = LastGeneration(
                user_request="private request",
                prompt="private prompt",
                negative_prompt="",
                model="test-model",
            )

            store.save_scoped_last_generation(300, private_scope, generation)

            self.assertIsNotNone(store.get_last_generation(300, private_scope))
            self.assertIsNone(store.get_last_generation(300, other_scope))

    def test_character_cache_is_conversation_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = DrawStore(Path(temp_dir) / "draw-memory.json")
            private_scope = "guild:100:channel:200"
            public_scope = "guild:101:channel:201"
            profile = CharacterProfile(
                name="private character",
                work="private work",
                traits=["private trait"],
                confidence=0.9,
            )

            store.save_character(profile, private_scope)

            self.assertIsNotNone(store.find_character("private character", private_scope))
            self.assertIsNone(store.find_character("private character", public_scope))


if __name__ == "__main__":
    unittest.main()
