import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import discord
from chat.cog import AtriChat
from chat.guild_settings import DEFAULTS


class SpeechPromptTests(unittest.TestCase):
    def metadata(self, *, enabled=True):
        author = NS(id=123, guild_permissions=discord.Permissions.none())
        guild = NS(id=100, owner_id=999, get_member=lambda _: author)
        message = NS(id=300, guild=guild, channel=NS(id=200), author=author, reference=None)
        cog = NS(owner_user_id=10, speech_service=NS(configured=True))
        with patch('chat.cog.policy_for', return_value=dict(DEFAULTS, tts_enabled=enabled)):
            return AtriChat._build_dsh_turn_host_metadata(cog, message)

    def test_each_turn_carries_implicit_speech_intent_examples_and_exclusions(self):
        metadata = self.metadata()
        self.assertIn('tts_configured=true', metadata)
        for phrase in ('说一句晚安', '说：你好', 'direct imperative “说”', '跟我说句话',
                       '你是说……吗', '别说了', '只用文字说一下', 'text-only/quiet instructions override'):
            self.assertIn(phrase, metadata)
        self.assertIn('host never converts keywords into tool calls', metadata)
        self.assertIn('never bypass host cooldowns', metadata)

    def test_intent_mapping_does_not_override_server_disable(self):
        metadata = self.metadata(enabled=False)
        self.assertIn('tts_configured=false', metadata)
        self.assertNotIn('tts_configured=true\n', metadata)

    def test_emotions_are_free_form_and_can_change_within_one_sentence(self):
        metadata = self.metadata()
        for phrase in ('free-form', 'WITHIN one sentence', 'not an enum', 'combine feelings',
                       'ONE synthesis request', 'ONE attachment'):
            self.assertIn(phrase, metadata)
        self.assertNotIn('up to two S2 bracket cues', metadata)


if __name__ == '__main__':
    unittest.main()
