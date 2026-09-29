from __future__ import annotations

import tempfile
import os
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from chat.guild_settings import DEFAULTS
from chat.tts import (FISH_MODEL, VOICE_ID, FishSpeechService, SpeechError, SpeechLedger,
                      validate_text, validate_style, normalize_segments, fish_text)


class TtsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = FishSpeechService(Path(self.tmp.name))
        self.message = NS(id=123, guild=NS(id=1, filesize_limit=8*1024*1024, me=NS()),
                          channel=NS(id=10), author=NS(id=55, bot=False), reply=AsyncMock(return_value=NS(id=999)))

    def test_fixed_free_model_voice_and_strict_text_validation(self):
        self.assertEqual(FISH_MODEL, 's2.1-pro-free')
        self.assertEqual(VOICE_ID, '5303afc44f544728b9a45000bf632c9e')
        self.assertEqual(validate_text('你好，亚托莉。'), '你好，亚托莉。')
        for value in ('', 'x' * 501, 'https://example.com', 'token=secret-value-123456789'):
            with self.subTest(value=value), self.assertRaises(SpeechError):
                validate_text(value)
        with self.assertRaises(SpeechError):
            validate_text('hello only', japanese=True)

    def test_ledger_idempotence_cooldown_and_message_scope(self):
        ledger = self.service.ledger
        first = ledger.reserve(self.message)
        self.assertIsNone(first)
        with self.assertRaises(SpeechError):
            ledger.reserve(self.message)
        ledger.finish(self.message.id, 'sent', 999)
        self.assertEqual(ledger.reserve(self.message), '999')
        other = NS(id=123, guild=NS(id=2), channel=NS(id=10), author=NS(id=55))
        with self.assertRaises(SpeechError):
            ledger.reserve(other)

    async def test_service_rejects_without_key_before_upstream(self):
        chat = NS(bot=NS(is_globally_blacklisted=lambda _: False), _is_chat_guild_whitelisted=lambda _: True,
                  client=NS(config=NS()), _wait_for_agent_api_slot=AsyncMock())
        with patch.dict('os.environ', {'FISH_API_KEY': ''}, clear=False):
            with patch('chat.tts.policy_for', return_value={**DEFAULTS, 'tts_enabled': True}):
                with self.assertRaises(SpeechError):
                    await self.service.execute(chat, self.message, {'text': '请说你好'})

    async def test_tool_rejects_extra_target_and_duplicate_message(self):
        chat = NS(bot=NS(is_globally_blacklisted=lambda _: False), _is_chat_guild_whitelisted=lambda _: True,
                  client=NS(config=NS()), _wait_for_agent_api_slot=AsyncMock())
        with self.assertRaises(SpeechError):
            await self.service.execute(chat, self.message, {'text': '你好', 'channel_id': '11'})
        with patch.dict('os.environ', {'FISH_API_KEY': 'fake'}, clear=False), \
             patch('chat.tts.policy_for', return_value={**DEFAULTS, 'tts_enabled': True}), \
             patch.object(self.service, 'authorize', new=AsyncMock()), \
             patch.object(self.service, 'translate', new=AsyncMock(return_value=[{'text': 'こんにちは', 'style': ''}])), \
             patch.object(self.service, 'synthesize', new=AsyncMock(return_value=b'ID3' + b'x' * 200)), \
             patch.object(self.message, 'reply', new=AsyncMock(return_value=NS(id=999))):
            self.message.channel.permissions_for = lambda member: NS(view_channel=True, read_message_history=True,
                                                                       send_messages=True, attach_files=True)
            # Missing guild.send identity is irrelevant to the reservation and fake authorization.
            result = await self.service.execute(chat, self.message, {'text': '你好'})
            self.assertIn('sent_message_id', result['content'])
            again = await self.service.execute(chat, self.message, {'text': '再说一次'})
            self.assertTrue(json.loads(again['content'])['already_sent'])
            self.message.reply.assert_awaited_once()
            self.service.synthesize.assert_awaited_once()

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions')
    def test_ledger_is_private_file(self):
        self.assertEqual(self.service.ledger.path.stat().st_mode & 0o777, 0o600)

    async def test_failure_has_no_retry_even_after_reopen(self):
        with patch.dict('os.environ', {'FISH_API_KEY': 'fake'}), \
             patch.object(self.service, 'authorize', new=AsyncMock()), \
             patch.object(self.service, 'translate', new=AsyncMock(side_effect=SpeechError('翻译失败'))):
            with self.assertRaises(SpeechError):
                await self.service.execute(NS(), self.message, {'text': '你好'})
            other = SpeechLedger(self.service.ledger.path)
            with self.assertRaises(SpeechError):
                other.reserve(self.message)
            self.service.translate.assert_awaited_once()

    async def test_cancel_does_not_unlock_duplicate_request(self):
        async def cancel(*args, **kwargs):
            raise asyncio.CancelledError()
        with patch.dict('os.environ', {'FISH_API_KEY': 'fake'}), \
             patch.object(self.service, 'authorize', new=AsyncMock()), \
             patch.object(self.service, 'translate', side_effect=cancel):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.execute(NS(), self.message, {'text': '你好'})
            self.assertFalse(self.service.lock.locked())
            with self.assertRaises(SpeechError):
                self.service.ledger.reserve(self.message)

    async def test_permission_rechecked_before_generation_and_sending(self):
        with patch.dict('os.environ', {'FISH_API_KEY': 'fake'}), \
             patch.object(self.service, 'authorize', new=AsyncMock(side_effect=[None, SpeechError('已禁用')])), \
             patch.object(self.service, 'translate', new=AsyncMock(return_value=[{'text': 'こんにちは', 'style': ''}])), \
             patch.object(self.service, 'synthesize', new=AsyncMock()) as synth:
            with self.assertRaises(SpeechError):
                await self.service.execute(NS(), self.message, {'text': '你好'})
            synth.assert_not_awaited()
            self.message.reply.assert_not_awaited()

    async def test_authorization_checks_current_channel_and_requester(self):
        permissions = NS(view_channel=True, read_message_history=True, send_messages=True, attach_files=True)
        self.message.channel.permissions_for = lambda _: permissions
        self.message.guild.fetch_member = AsyncMock(return_value=self.message.author)
        chat = NS(bot=NS(is_globally_blacklisted=lambda _: False), _is_chat_guild_whitelisted=lambda _: True)
        with patch('chat.tts.policy_for', return_value=dict(DEFAULTS)):
            await self.service.authorize(chat, self.message)
            permissions.attach_files = False
            with self.assertRaises(SpeechError):
                await self.service.authorize(chat, self.message)
        with patch('chat.tts.policy_for', return_value=dict(DEFAULTS, tts_enabled=False)):
            with self.assertRaises(SpeechError):
                await self.service.authorize(chat, self.message)

    async def test_translation_is_bounded_tool_free_and_uses_current_model(self):
        from chat.client import OpenAICompatibleConfig
        chat = NS(client=NS(config=OpenAICompatibleConfig('https://example.invalid', 'fake', 'current', enable_web_search=True)),
                  _wait_for_agent_api_slot=AsyncMock(), _record_agent_api_failure=AsyncMock())
        fake = NS(create_chat_completion=AsyncMock(return_value='{"segments":["こんにちは。"]}'))
        with patch('chat.tts.OpenAICompatibleClient', return_value=fake) as factory:
            self.assertEqual(await self.service.translate(chat, '你好'), [{'text': 'こんにちは。', 'style': ''}])
        cfg = factory.call_args.args[0]
        self.assertEqual((cfg.model, cfg.retry_count, cfg.timeout_seconds), ('current', 0, 35))
        self.assertFalse(cfg.enable_web_search)
        self.assertTrue(chat.client.config.enable_web_search)
        messages = fake.create_chat_completion.await_args.args[0]
        self.assertEqual(len(messages), 2)
        self.assertEqual(json.loads(messages[1]['content']), {'segments': [{'text': '你好', 'style': ''}]})

    async def test_multiple_emotions_translate_together_without_changing_styles(self):
        from chat.client import OpenAICompatibleConfig
        chat = NS(client=NS(config=OpenAICompatibleConfig('https://example.invalid', 'fake', 'current')),
                  _wait_for_agent_api_slot=AsyncMock(), _record_agent_api_failure=AsyncMock())
        source = [{'text': '真的？', 'style': 'surprised but worried'},
                  {'text': '太好了。', 'style': 'joyful with bittersweet relief'},
                  {'text': '晚安。', 'style': 'gentle whisper'}]
        fake = NS(create_chat_completion=AsyncMock(return_value='{"segments":["本当？","よかった。","おやすみ。"]}'))
        with patch('chat.tts.OpenAICompatibleClient', return_value=fake):
            result = await self.service.translate(chat, '真的？太好了。晚安。', segments=source)
        self.assertEqual([s['style'] for s in result], [s['style'] for s in source])
        self.assertEqual(''.join(s['text'] for s in result), '本当？よかった。おやすみ。')
        fake.create_chat_completion.assert_awaited_once()
        self.assertEqual(json.loads(fake.create_chat_completion.await_args.args[0][1]['content'])['segments'], source)

    async def test_invalid_translation_alignment_stops_before_fish_without_retry(self):
        from chat.client import OpenAICompatibleConfig
        chat = NS(client=NS(config=OpenAICompatibleConfig('https://example.invalid', 'fake', 'current')),
                  _wait_for_agent_api_slot=AsyncMock(), _record_agent_api_failure=AsyncMock())
        source = [{'text': '真的？', 'style': 'surprised'}, {'text': '太好了。', 'style': 'relieved'}]
        for value in ('not json', 'null', '[]', '{}', '{"segments":["こんにちは"]}',
                      '{"segments":["はい", ""]}', '{"segments":[5, "はい"]}',
                      '{"segments":["hello", "world"]}', '{"segments":["はい", "はい"],"extra":1}'):
            fake = NS(create_chat_completion=AsyncMock(return_value=value))
            with self.subTest(value=value), patch('chat.tts.OpenAICompatibleClient', return_value=fake):
                with self.assertRaises(SpeechError):
                    await self.service.translate(chat, '真的？太好了。', segments=source)
                fake.create_chat_completion.assert_awaited_once()

    async def test_multiple_styles_send_one_attachment_with_clean_subtitles(self):
        source = [{'text': '真的？', 'style': 'surprised'}, {'text': '太好了。', 'style': 'happy but shy'}]
        translated = [{'text': '本当？', 'style': 'surprised'}, {'text': 'よかった。', 'style': 'happy but shy'}]
        with patch.dict('os.environ', {'FISH_API_KEY': 'fake'}), \
             patch.object(self.service, 'authorize', new=AsyncMock()), \
             patch.object(self.service, 'translate', new=AsyncMock(return_value=translated)) as translate, \
             patch.object(self.service, 'synthesize', new=AsyncMock(return_value=b'ID3' + b'x'*200)) as synth:
            result = await self.service.execute(NS(), self.message, {'text': '真的？太好了。', 'segments': source})
        translate.assert_awaited_once()
        synth.assert_awaited_once_with(segments=translated, max_bytes=8*1024*1024)
        self.message.reply.assert_awaited_once()
        self.assertEqual(json.loads(result['content'])['speaking_styles'], ['surprised', 'happy but shy'])
        subtitle = self.message.reply.await_args.kwargs['content']
        self.assertIn('本当？よかった。', subtitle)
        self.assertNotIn('surprised', subtitle)


class StyleTests(unittest.TestCase):
    def test_free_form_mixed_emotions_and_changes_within_one_sentence(self):
        parts = [{'text': 'えっ、', 'style': 'surprised and doubtful'},
                 {'text': 'うれしいけど', 'style': 'trying to sound brave while worried'},
                 {'text': '大丈夫かな。', 'style': 'softly, bittersweet and uncertain'}]
        rendered = fish_text(segments=parts)
        self.assertEqual(rendered, '[surprised and doubtful] えっ、[trying to sound brave while worried] うれしいけど[softly, bittersweet and uncertain] 大丈夫かな。')
        self.assertEqual(validate_style('害羞又有点期待、轻声说'), '害羞又有点期待、轻声说')

    def test_plain_speech_and_reset_after_styled_segment(self):
        self.assertEqual(fish_text('こんにちは。'), 'こんにちは。')
        self.assertEqual(fish_text(segments=[{'text': 'うん', 'style': 'angry'}, {'text': 'ありがとう。'}]),
                         '[angry] うん[neutral] ありがとう。')
        self.assertEqual(fish_text(segments=[{'text': '本当？', 'style': 'surprised'}, {'text': 'はい。'}]),
                         '[surprised] 本当？[neutral] はい。')

    def test_styles_and_literal_brackets_cannot_inject_control_tags(self):
        for value in ('[happy]', 'hello\nshout', 'x'*97, 'https://example.com', 123):
            with self.subTest(value=value), self.assertRaises(SpeechError):
                validate_style(value)
        self.assertEqual(fish_text('これは[引用]です。'), 'これは「引用」です。')

    def test_source_alignment_retains_spaces_and_rejects_missing_or_repeated_spans(self):
        source = [{'text': 'Hello, ', 'style': 'warm'}, {'text': 'world!'}]
        normalized = normalize_segments('Hello, world!', source)
        self.assertEqual(''.join(s['text'] for s in normalized), 'Hello, world!')
        for parts in ([], [{'text': '你好'}], [{'text': '你好。', 'extra': True}], '你好。',
                      [{'text': 1}], [{'text': '你好。'}]*13, [{'style': 'happy'}]):
            with self.subTest(parts=parts), self.assertRaises(SpeechError):
                normalize_segments('你好。', parts)

    def test_global_japanese_limit_cannot_be_bypassed_by_splitting(self):
        with self.assertRaises(SpeechError):
            fish_text(segments=[{'text': 'あ'*400}, {'text': 'い'*400}])
        with self.assertRaises(SpeechError):
            fish_text(segments=[{'text': 'あ'}]*13)


class Response:
    def __init__(self, status=200, data=None, content_type='audio/mpeg', declared=None):
        self.status = status
        self.headers = {'Content-Type': content_type}
        self.content_length = declared
        self.data = data if data is not None else b'ID3' + b'x'*200
        self.content = self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def iter_chunked(self, limit):
        for offset in range(0, len(self.data), 100):
            yield self.data[offset:offset+100]


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = FishSpeechService(Path(self.tmp.name))

    async def synth(self, response, max_bytes=1000, segments=None):
        from unittest.mock import Mock
        session = Response()
        session.post = Mock(return_value=response)
        with patch.dict('os.environ', {'FISH_API_KEY': 'fake-secret'}), \
             patch('chat.tts.build_verified_connector', return_value=None), \
             patch('chat.tts.aiohttp.ClientSession', return_value=session):
            data = await self.service.synthesize('こんにちは。', max_bytes=max_bytes, segments=segments)
        self.assertEqual(session.post.call_count, 1)
        return data, session.post.call_args

    async def test_fixed_url_free_model_no_redirect_and_only_speech(self):
        data, call = await self.synth(Response())
        self.assertTrue(data.startswith(b'ID3'))
        self.assertEqual(call.args, ('https://api.fish.audio/v1/tts',))
        self.assertEqual(call.kwargs['headers']['model'], FISH_MODEL)
        self.assertEqual(call.kwargs['json']['reference_id'], VOICE_ID)
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertNotIn('messages', call.kwargs['json'])

    async def test_provider_errors_cool_down_without_echoing_response_or_key(self):
        for status in (401, 402, 403, 429, 500, 302):
            with self.subTest(status=status), self.assertRaises(SpeechError) as error:
                await self.synth(Response(status=status, data=b'fake-secret'))
            self.assertNotIn('fake-secret', str(error.exception))
        with self.service.ledger.connect() as db:
            self.assertIsNotNone(db.execute('SELECT until FROM speech_cooldown').fetchone())

    async def test_all_emotion_spans_are_sent_in_one_fish_request(self):
        parts = [{'text': '本当？', 'style': 'surprised'}, {'text': 'よかった。', 'style': 'relieved, almost laughing'}]
        _, call = await self.synth(Response(), segments=parts)
        self.assertEqual(call.kwargs['json']['text'], '[surprised] 本当？[relieved, almost laughing] よかった。')
        self.assertNotIn('emotion', call.kwargs['json'])

    async def test_reject_non_audio_invalid_magic_and_oversize(self):
        for response, size in [(Response(content_type='application/json'), 1000), (Response(data=b'broken'*100), 1000),
                               (Response(data=b'ID3'+b'x'*1000), 200), (Response(declared=5000), 1000)]:
            with self.subTest(size=size), self.assertRaises(SpeechError):
                await self.synth(response, size)


if __name__ == '__main__':
    unittest.main()
