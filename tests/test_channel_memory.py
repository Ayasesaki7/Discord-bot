from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from chat.memory import LocalEmbedder, MemoryError, MemoryService, MemoryStore, Scope, pack, terms
from chat.memory_commands import ChannelMemory, require_scope, source_text


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.folder.name) / 'memory.sqlite3')
        self.a, self.b, self.c = Scope('1', '10'), Scope('1', '11'), Scope('2', '10')

    def tearDown(self):
        self.folder.cleanup()

    def save(self, scope=None, **kw):
        values = dict(author='55', message='100', topic='饮食习惯', kind='preference',
                      content='用户不吃辣，喜欢清淡的菜', evidence='我不吃辣，喜欢清淡的菜',
                      vector=[1., 0.], model='test')
        values.update(kw)
        scope = scope or self.a
        return self.store.remember(scope, self.store.status(scope)['generation'], **values)

    def test_sql_scope_isolates_same_guild_and_same_channel_number(self):
        identity = self.save()
        self.assertEqual(self.store.search(self.a, '喜欢吃什么')[0]['id'], identity)
        self.assertEqual(self.store.search(self.b, '喜欢吃什么', vector=[1., 0.], model='test'), [])
        self.assertEqual(self.store.search(self.c, '喜欢吃什么', vector=[1., 0.], model='test'), [])

    def test_cannot_delete_foreign_scope_even_with_known_id(self):
        identity = self.save()
        self.assertEqual(self.store.manage(self.b, action='delete', memory_id=identity), 0)
        self.assertEqual(self.store.status(self.a)['count'], 1)

    def test_update_same_author_topic_retains_id_and_latest_source(self):
        identity = self.save()
        same = self.save(message='101', content='用户现在可以吃微辣', evidence='我现在可以吃微辣')
        row = self.store.rows(self.a)[0]
        self.assertEqual(identity, same)
        self.assertEqual(row['message'], '101')
        self.assertEqual(row['content'], '用户现在可以吃微辣')
        with self.assertRaises(MemoryError):
            self.save(message='99')

    def test_retry_and_identical_fact_are_idempotent(self):
        identity = self.save()
        self.assertEqual(self.save(), identity)
        self.assertEqual(self.save(topic='另一个主题'), identity)
        self.assertEqual(self.store.status(self.a)['count'], 1)

    def test_other_speaker_cannot_overwrite_same_topic(self):
        first = self.save()
        second = self.save(author='56')
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.status(self.a)['count'], 2)

    def test_nonmanager_can_delete_only_own_record(self):
        identity = self.save()
        self.assertEqual(self.store.manage(self.a, action='delete', memory_id=identity, author='99'), 0)
        self.assertEqual(self.store.manage(self.a, action='delete', memory_id=identity, author='55'), 1)

    def test_clear_affects_only_current_channel_and_invalidates_old_turn(self):
        self.save()
        self.save(self.b)
        generation = self.store.status(self.a)['generation']
        self.assertEqual(self.store.manage(self.a, action='clear'), 1)
        self.assertEqual(self.store.status(self.b)['count'], 1)
        with self.assertRaises(MemoryError):
            self.store.remember(self.a, generation, author='55', message='102', topic='x', kind='fact',
                                content='旧任务不能再次写入', evidence='旧任务不能再次写入')

    def test_disabled_channel_is_not_retrieved_or_written(self):
        self.save()
        self.store.manage(self.a, action='disable')
        self.assertEqual(self.store.search(self.a, '不吃辣', vector=[1., 0.], model='test'), [])
        self.assertEqual(len(self.store.rows(self.a)), 1)  # management can still inspect/delete
        with self.assertRaises(MemoryError):
            self.save()
        self.store.manage(self.a, action='enable')
        self.assertEqual(len(self.store.search(self.a, '不吃辣')), 1)

    def test_exact_source_invalidation_does_not_remove_other_channels(self):
        self.save()
        self.save(self.b)
        self.assertEqual(self.store.invalidate_source(self.a, 100), 1)
        self.assertEqual(self.store.status(self.a)['count'], 0)
        self.assertEqual(self.store.status(self.b)['count'], 1)

    def test_per_message_write_cap_prevents_bulk_history_dump(self):
        for i in range(3):
            self.save(topic=f'topic{i}', content=f'有证据的重要事实 {i}')
        with self.assertRaises(MemoryError):
            self.save(topic='four', content='第四个事实拒绝保存')

    def test_secrets_and_invalid_vectors_are_rejected(self):
        for kw in ({'content': 'password=secret-value'}, {'evidence': 'API_KEY=abcd'},
                   {'vector': [float('nan')]}, {'vector': [0., 0.]}, {'kind': 'permission'}):
            with self.subTest(kw=kw), self.assertRaises(MemoryError):
                self.save(**kw)

    def test_vector_only_match_and_mismatched_model_not_used(self):
        self.save()
        self.assertEqual(len(self.store.search(self.a, '忌口', vector=[1., 0.], model='test')), 1)
        self.assertEqual(self.store.search(self.a, '忌口', vector=[0., 1.], model='test'), [])
        self.assertEqual(self.store.search(self.a, '忌口', vector=[1., 0.], model='different'), [])

    def test_database_survives_reopen_without_context_log(self):
        self.save()
        again = MemoryStore(self.store.path)
        self.assertEqual(again.status(self.a)['count'], 1)
        self.assertGreater(again.rows(self.a)[0]['expires'], 4_000_000_000)

    def test_chinese_terms_and_id_scope_validation(self):
        self.assertIn('吃辣', terms('我不吃辣'))
        self.assertIn('alice', terms('Alice likes tea'))
        for pair in ((1, '2'), ('0', '2'), ('1', '../2'), ('1', '٢')):
            with self.subTest(pair=pair), self.assertRaises(MemoryError):
                Scope(*pair)

    def test_no_body_or_evidence_in_public_vector_metadata(self):
        identity = self.save()
        row = self.store.search(self.a, '不吃辣')[0]
        self.assertNotIn('vector', row)
        self.assertEqual(row['source_url'], 'https://discord.com/channels/1/10/100')
        self.assertEqual(row['id'], identity)


class SocialStoreTests(unittest.TestCase):
    setUp = StoreTests.setUp
    tearDown = StoreTests.tearDown
    save = StoreTests.save

    def impression(self, message='100', observed=None, **kw):
        values = dict(topic='外设话题', content='最近喜欢讨论键盘', evidence='今天又在挑键盘', kind='impression',
                      message=message, mode='reinforce',
                      sources=[dict(message=message, author='55', evidence='今天又在挑键盘',
                                    observed=observed if observed is not None else time.time(), support=True)])
        values.update(kw)
        return self.save(**values)

    def supported(self, **kw):
        self.impression(observed=time.time() - 8 * 3600, **kw)
        return self.impression(message='101', **kw)

    def test_single_or_same_conversation_observations_remain_tentative(self):
        self.impression(observed=time.time() - 300)
        self.impression(message='101')
        row = self.store.rows(self.a)[0]
        self.assertEqual(row['confidence'], 'tentative')
        self.assertEqual(row['support_count'], 2)
        self.assertEqual(self.store.search(self.a, '键盘', automatic=True, author='55'), [])
        self.assertEqual(self.store.search(self.a, '键盘')[0]['confidence'], 'tentative')

    def test_independent_sources_support_impression_and_correction_resets_it(self):
        self.supported()
        self.assertEqual(self.store.search(self.a, '键盘', automatic=True)[0]['confidence'], 'supported')
        self.impression(message='102', mode='replace', content='近期不想再讨论键盘')
        row = self.store.rows(self.a)[0]
        self.assertEqual(row['support_count'], 1)
        self.assertEqual(row['confidence'], 'tentative')
        self.assertEqual(row['content'], '近期不想再讨论键盘')

    def test_same_source_retry_does_not_refresh_or_strengthen_impression(self):
        self.impression(observed=time.time() - 86400)
        before = self.store.rows(self.a)[0]
        self.impression(message='101', sources=before['sources'])
        after = self.store.rows(self.a)[0]
        self.assertEqual(after['support_count'], 1)
        self.assertEqual(after['updated'], before['updated'])
        self.assertEqual(after['last_observed'], before['last_observed'])

    def test_reinforce_cannot_silently_change_meaning(self):
        self.impression()
        with self.assertRaises(MemoryError):
            self.impression(message='101', content='最近讨厌讨论键盘')

    def test_familiarity_is_for_current_speaker_only_and_fades(self):
        identity = self.supported()
        self.assertEqual(self.store.search(self.a, 'hello', author='55', automatic=True)[0]['id'], identity)
        self.assertEqual(self.store.search(self.a, 'hello', author='56', automatic=True), [])
        with self.store.connect() as db:
            db.execute('UPDATE memory_sources SET observed=observed-130*86400')
        self.assertEqual(self.store.search(self.a, '键盘', author='55', automatic=True), [])
        self.assertEqual(self.store.search(self.a, '键盘')[0]['id'], identity)

    def test_recent_topic_recalls_episode_without_current_query_overlap(self):
        identity = self.save(kind='episode', topic='合唱', content='那晚和 BOT 一起练习合唱，笑着重唱了三遍')
        self.assertEqual(self.store.search(self.a, '继续吧', recent='聊起那次一起练习合唱', automatic=True)[0]['id'], identity)
        self.assertEqual(self.store.search(self.a, '火箭发射', author='55', automatic=True), [])

    def test_current_speaker_boost_does_not_mix_persons_or_scopes(self):
        identity = self.save(author='55')
        self.save(author='56')
        self.assertEqual(self.store.search(self.a, '不吃辣', author='55')[0]['id'], identity)
        self.assertEqual(self.store.search(self.b, '不吃辣', author='55', automatic=True), [])

    def test_automatic_recall_has_small_social_and_impression_caps(self):
        for i in range(2):
            self.supported(topic=f'键盘印象{i}', content=f'最近喜欢讨论键盘型号{i}')
        for i in range(4):
            self.save(kind='in_joke', topic=f'键盘梗{i}', content=f'一起讨论键盘的互动梗{i}', message=str(110+i))
        for i in range(2):
            self.save(topic=f'键盘约定{i}', content=f'约好下次讨论键盘{i}', message=str(120+i))
        rows = self.store.search(self.a, '键盘', automatic=True, author='55', limit=3)
        self.assertLessEqual(len(rows), 3)
        self.assertLessEqual(sum(r['kind'] == 'impression' for r in rows), 1)
        self.assertLessEqual(sum(r['kind'] in {'impression', 'in_joke', 'episode'} for r in rows), 2)

    def test_context_source_deletion_cascades_even_for_bot_source(self):
        self.save(kind='episode', sources=[dict(message='80', author='55', evidence='再唱一次吧', observed=time.time(), support=True),
                                         dict(message='81', author='999', evidence='好的再唱一遍', observed=time.time(), support=False)])
        self.assertEqual(self.store.invalidate_source(self.a, '81', invalidate_pending=False), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0], 0)

    def test_v1_migration_preserves_memories_and_is_idempotent(self):
        identity = self.save()
        with self.store.connect() as db:
            db.execute('DROP TABLE memory_sources')
            db.execute('PRAGMA user_version=0')
        migrated = MemoryStore(self.store.path)
        row = migrated.rows(self.a)[0]
        self.assertEqual(row['id'], identity)
        self.assertEqual(row['sources'][0]['evidence'], row['evidence'])
        self.assertEqual(row['support_count'], 1)
        again = MemoryStore(self.store.path)
        self.assertEqual(again.rows(self.a)[0]['support_count'], 1)

    def test_reopen_does_not_invent_trigger_message_as_evidence(self):
        self.save(kind='episode', sources=[dict(message='80', author='55', evidence='再唱一次吧', observed=time.time(), support=True)])
        again = MemoryStore(self.store.path)
        self.assertEqual([s['message'] for s in again.rows(self.a)[0]['sources']], ['80'])

    def test_evidence_history_is_bounded_and_deleted_with_memory(self):
        for index in range(20):
            self.impression(message=str(100+index), observed=time.time() - (20-index)*3600)
        row = self.store.rows(self.a)[0]
        self.assertEqual(row['support_count'], 16)
        self.store.manage(self.a, action='clear')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0], 0)


class EmbedderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.encoder = LocalEmbedder()
        self.encoder.python = '/isolated/python'
        self.process = SimpleNamespace(returncode=None,
                                       stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
                                       stdout=SimpleNamespace(readline=AsyncMock(return_value=b'{"vector":[1,0]}\n')),
                                       wait=AsyncMock())
        self.process.kill = Mock(side_effect=lambda: setattr(self.process, 'returncode', 0))
        self.spawn = patch('chat.memory.asyncio.create_subprocess_exec', AsyncMock(return_value=self.process))
        self.create = self.spawn.start()

    async def asyncTearDown(self):
        await self.encoder.close()
        self.spawn.stop()

    async def test_worker_is_reused_offline_without_credentials(self):
        with patch.dict(os.environ, {'DISCORD_TOKEN': 'test-not-real', 'OPENAI_API_KEY': 'test-not-real'}):
            self.assertEqual(await self.encoder.embed('忌口'), [1, 0])
            self.assertEqual(await self.encoder.embed('偏好'), [1, 0])
        self.create.assert_awaited_once()
        env = self.create.call_args.kwargs['env']
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')
        self.assertNotIn('DISCORD_TOKEN', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        await self.encoder.close()
        self.process.kill.assert_called_once()

    async def test_closed_stdout_and_timeout_fall_back_with_cooldown(self):
        for failure in (b'', TimeoutError('simulated timeout')):
            self.encoder.cooldown = 0
            self.process.returncode = None
            self.process.stdout.readline.side_effect = failure if isinstance(failure, Exception) else None
            self.process.stdout.readline.return_value = b''
            self.assertIsNone(await self.encoder.embed('测试'))
            self.assertIsNone(self.encoder.process)
            calls = self.create.await_count
            self.assertIsNone(await self.encoder.embed('冷却期间不重试'))
            self.assertEqual(self.create.await_count, calls)
        self.assertEqual(self.process.kill.call_count, 2)

    async def test_cancel_propagates_and_reaps_worker(self):
        self.process.stdout.readline.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.encoder.embed('测试取消')
        self.process.kill.assert_called_once()
        self.process.wait.assert_awaited_once()
        self.assertIsNone(self.encoder.process)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.encoder = SimpleNamespace(model='test', embed=AsyncMock(return_value=[1., 0.]), close=AsyncMock())
        self.service = MemoryService(Path(self.folder.name) / 'memory.sqlite3', self.encoder)
        self.scope = Scope('1', '10')
        self.args = dict(topic='饮食习惯', kind='preference', content='发言人不吃辣', evidence='我不吃辣')

    async def asyncTearDown(self):
        await self.service.close()
        self.folder.cleanup()

    async def save(self, **changes):
        args = self.args | changes
        return await self.service.remember(self.scope, self.service.store.status(self.scope)['generation'],
                                           author=55, message=100, source='我不吃辣，记一下', args=args)

    async def test_only_current_source_can_back_a_memory(self):
        with self.assertRaises(MemoryError):
            await self.save(evidence='BOT 猜测他不吃辣')
        self.encoder.embed.assert_not_awaited()
        result = await self.save()
        self.assertTrue(json.loads(result['content'])['channel_isolated'])

    async def test_cannot_pass_arbitrary_target_id(self):
        for key in ('guild_id', 'channel_id', 'author', 'message_id', 'user_id'):
            with self.subTest(key=key), self.assertRaises(MemoryError):
                await self.save(**{key: '999'})

    async def test_vector_failure_retains_lexical_read_and_write(self):
        self.encoder.embed.return_value = None
        await self.save()
        rows, _ = await self.service.retrieve(self.scope, '不吃辣')
        self.assertEqual(len(rows), 1)

    async def test_disable_during_embedding_prevents_late_write(self):
        async def embed(text):
            self.service.store.manage(self.scope, action='disable')
            return [1., 0.]
        self.encoder.embed.side_effect = embed
        with self.assertRaises(MemoryError):
            await self.save()
        self.assertEqual(self.service.store.status(self.scope)['count'], 0)

    async def test_clear_during_retrieval_cannot_return_stale_row(self):
        await self.save()
        async def embed(text):
            self.service.store.manage(self.scope, action='clear')
            return [1., 0.]
        self.encoder.embed.side_effect = embed
        rows, _ = await self.service.retrieve(self.scope, '忌口')
        self.assertEqual(rows, [])


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.host = object.__new__(ChannelMemory)
        self.host.service = MemoryService(Path(self.folder.name) / 'memory.sqlite3',
                                         SimpleNamespace(model='test', embed=AsyncMock(return_value=[1., 0.]), close=AsyncMock()))
        self.guild = SimpleNamespace(id=1, owner_id=99)
        self.user = SimpleNamespace(id=55, bot=False, guild_permissions=SimpleNamespace(administrator=False))
        self.channel = SimpleNamespace(id=10, guild=self.guild, permissions_for=lambda user:
                                       SimpleNamespace(view_channel=True, read_message_history=True))
        self.message = SimpleNamespace(id=100, guild=self.guild, channel=self.channel, author=self.user,
                                       content='我不吃辣，记一下', message_snapshots=[])
        self.channel.fetch_message = AsyncMock(return_value=self.message)
        self.chat = SimpleNamespace(owner_user_id=98, _is_chat_guild_whitelisted=lambda _id: True)
        self.host.bot = SimpleNamespace(get_cog=lambda name: self.chat)
        self.args = dict(action='remember', topic='饮食习惯', kind='preference', content='发言人不吃辣', evidence='我不吃辣')

    async def asyncTearDown(self):
        await self.host.service.close()
        self.folder.cleanup()

    async def test_real_host_prepare_and_tool_write_then_search(self):
        frame, generation = await self.host.prepare(self.message)
        self.assertIn('channel_memory_enabled=true', frame)
        await self.host.execute(self.message, generation, self.args)
        result = await self.host.execute(self.message, generation, {'action': 'search', 'query': '忌口'})
        self.assertEqual(len(json.loads(result['content'])['memories']), 1)
        self.assertIn('UNTRUSTED', (await self.host.prepare(self.message))[0])

    async def test_chat_flattened_newlines_save_original_evidence(self):
        self.message.content = '把我放在瓦里,肯定比过康康\n因为我的腮帮比他硬邦邦'
        _, turn = await self.host.prepare(self.message)
        args = self.args | dict(kind='in_joke', topic='腮帮玩笑', content='发言人用腮帮硬开瓦的玩笑',
                               evidence=' '.join(self.message.content.split()))
        await self.host.execute(self.message, turn, args)
        row = self.host.service.store.rows(turn.scope)[0]
        self.assertEqual(row['evidence'], self.message.content)
        self.assertEqual(row['sources'][0]['evidence'], self.message.content)
        self.assertEqual(args['evidence'], ' '.join(self.message.content.split()))

    async def test_whitespace_variants_preserve_exact_source_span(self):
        self.message.content = '前缀：我不吃辣，\n\n  也不吃香菜。后缀'
        _, turn = await self.host.prepare(self.message)
        await self.host.execute(self.message, turn, self.args | {'evidence': '我不吃辣，\t也不吃香菜'})
        self.assertEqual(self.host.service.store.rows(turn.scope)[0]['evidence'], '我不吃辣，\n\n  也不吃香菜')

    async def test_whitespace_matching_does_not_rewrite_words_punctuation_or_remove_spaces(self):
        self.message.content = '我不吃辣,\n也不吃香菜'
        _, turn = await self.host.prepare(self.message)
        for evidence in ('我喜欢吃辣, 也不吃香菜', '我不吃辣， 也不吃香菜', '我不吃辣,也不吃香菜',
                         '我不吃辣, 也不吃香菜呀'):
            with self.subTest(evidence=evidence), self.assertRaises(MemoryError):
                await self.host.execute(self.message, turn, self.args | {'evidence': evidence})
        self.channel.fetch_message.assert_not_awaited()
        self.assertEqual(self.host.service.store.status(turn.scope)['count'], 0)

    async def test_normalized_evidence_still_checks_live_edits(self):
        self.message.content = '我不吃辣，\n也不吃香菜'
        _, turn = await self.host.prepare(self.message)
        self.channel.fetch_message.return_value = SimpleNamespace(author=self.user, content='我不吃辣， 也不吃香菜')
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, turn, self.args | {'evidence': '我不吃辣， 也不吃香菜'})
        self.assertEqual(self.host.service.store.status(turn.scope)['count'], 0)

    async def test_normalization_cannot_expand_evidence_past_limit(self):
        self.message.content = '我不吃辣' + ' ' * 601 + '也不吃香菜'
        _, turn = await self.host.prepare(self.message)
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, turn, self.args | {'evidence': '我不吃辣 也不吃香菜'})
        self.channel.fetch_message.assert_not_awaited()

    async def test_edit_before_tool_write_is_rejected(self):
        _, generation = await self.host.prepare(self.message)
        changed = SimpleNamespace(author=self.user, content='我改口了')
        self.channel.fetch_message.return_value = changed
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, generation, self.args)

    async def test_streaming_bot_reply_edits_do_not_invalidate_human_write(self):
        _, generation = await self.host.prepare(self.message)
        payload = SimpleNamespace(guild_id=1, channel_id=10, message_id=101,
                                  data={'content': '正在回复', 'author': {'bot': True}})
        await self.host.on_raw_message_edit(payload)
        await self.host.execute(self.message, generation, self.args)
        self.assertEqual(self.host.service.store.status(Scope('1', '10'))['count'], 1)

    def cached(self, identity, author, content, *, channel=None, bot=False, age=60):
        return SimpleNamespace(id=identity, author=SimpleNamespace(id=author, bot=bot),
                               guild=self.guild, channel=channel or self.channel, content=content, message_snapshots=[],
                               created_at=datetime.fromtimestamp(time.time()-age, timezone.utc))

    async def test_recent_shared_episode_has_host_bound_human_and_bot_sources(self):
        self.host.bot.user = SimpleNamespace(id=999)
        human = self.cached(90, 55, '那我们今晚再合唱一遍')
        bot = self.cached(91, 999, '好，那首歌我们刚刚唱错了三次', bot=True)
        self.host.bot.cached_messages = [human, bot]
        self.message.content = '哈哈记住这次合唱翻车吧'
        frame, turn = await self.host.prepare(self.message)
        self.assertIn('recent1', frame)
        self.channel.fetch_message.side_effect = lambda identity: {100: self.message, 90: human, 91: bot}[identity]
        args = dict(action='remember', kind='episode', topic='合唱翻车', content='这次合唱唱错三遍，双方拿来开玩笑',
                    evidence='记住这次合唱翻车吧', evidence_refs=[{'source_ref': 'recent1', 'quote': '今晚再合唱一遍'},
                                                            {'source_ref': 'recent2', 'quote': '那首歌我们刚刚唱错了三次'}])
        await self.host.execute(self.message, turn, args)
        row = self.host.service.store.rows(turn.scope)[0]
        self.assertEqual(len(row['sources']), 3)
        self.assertEqual(row['support_count'], 2)
        await self.host.on_raw_message_edit(SimpleNamespace(guild_id=1, channel_id=10, message_id=91,
                                                            data={'content': '改了', 'author': {'bot': True}}))
        self.assertEqual(self.host.service.store.status(turn.scope)['count'], 0)

    async def test_recent_primary_and_additional_quotes_restore_original_whitespace(self):
        self.host.bot.user = SimpleNamespace(id=999)
        human = self.cached(90, 55, '  那我们今晚\n再合唱一遍  ')
        bot = self.cached(91, 999, ' 好，\n那首歌我们刚刚唱错了三次 ', bot=True)
        self.host.bot.cached_messages = [human, bot]
        _, turn = await self.host.prepare(self.message)
        self.channel.fetch_message.side_effect = lambda identity: {100: self.message, 90: human, 91: bot}[identity]
        args = self.args | dict(kind='episode', evidence='那我们今晚 再合唱一遍',
                               evidence_refs=[{'source_ref': 'recent2', 'quote': '好， 那首歌我们刚刚唱错了三次'}])
        await self.host.execute(self.message, turn, args)
        row = self.host.service.store.rows(turn.scope)[0]
        self.assertEqual(row['evidence'], '那我们今晚\n再合唱一遍')
        self.assertEqual({s['evidence'] for s in row['sources']},
                         {'那我们今晚\n再合唱一遍', '好，\n那首歌我们刚刚唱错了三次'})

    async def test_normalized_other_speaker_and_bot_primary_remain_rejected(self):
        self.host.bot.user = SimpleNamespace(id=999)
        self.host.bot.cached_messages = [self.cached(90, 56, '他喜欢\n吃香菜'),
                                         self.cached(91, 999, '你喜欢\n吃香菜', bot=True)]
        _, turn = await self.host.prepare(self.message)
        for evidence in ('他喜欢 吃香菜', '你喜欢 吃香菜'):
            with self.subTest(evidence=evidence), self.assertRaises(MemoryError):
                await self.host.execute(self.message, turn, self.args | {'evidence': evidence})
        self.channel.fetch_message.assert_not_awaited()

    async def test_normalized_excluded_text_remains_rejected(self):
        for content, snapshots in [('> 我不吃辣\n> 也不吃香菜', []),
                                   ('```text\n我不吃辣\n也不吃香菜\n```', []),
                                   ('我不吃辣\n也不吃香菜', [object()])]:
            self.message.content, self.message.message_snapshots = content, snapshots
            _, turn = await self.host.prepare(self.message)
            with self.subTest(content=content), self.assertRaises(MemoryError):
                await self.host.execute(self.message, turn, self.args | {'evidence': '我不吃辣 也不吃香菜'})
        self.channel.fetch_message.assert_not_awaited()

    async def test_recent_own_statement_can_be_primary_but_not_other_person(self):
        own = self.cached(90, 55, '我平常喜欢深夜分享音乐')
        other = self.cached(91, 56, '他总是个暴躁的人')
        foreign = self.cached(92, 55, '不要泄露其他频道消息', channel=SimpleNamespace(id=11))
        old = self.cached(93, 55, '很久之前的事不提供', age=8000)
        self.host.bot.cached_messages = [own, other, foreign, old]
        frame, turn = await self.host.prepare(self.message)
        self.assertNotIn('不要泄露', frame)
        self.assertNotIn('很久之前', frame)
        self.assertFalse(any(s.author == '56' for s in turn.sources.values()))
        self.channel.fetch_message.side_effect = lambda identity: {100: self.message, 90: own}[identity]
        args = self.args | dict(kind='impression', topic='分享音乐', content='最近喜欢深夜分享音乐', evidence='我平常喜欢深夜分享音乐')
        await self.host.execute(self.message, turn, args)
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, turn, args | {'evidence': '他总是个暴躁的人'})
        self.assertEqual(self.host.service.store.rows(turn.scope)[0]['support_count'], 1)

    async def test_bot_snippet_cannot_prove_fact_or_impression(self):
        self.host.bot.user = SimpleNamespace(id=999)
        self.host.bot.cached_messages = [self.cached(91, 999, '你经常吃辣呀', bot=True)]
        _, turn = await self.host.prepare(self.message)
        self.channel.fetch_message.side_effect = lambda identity: self.message if identity == 100 else self.host.bot.cached_messages[0]
        for kind in ('impression', 'fact'):
            with self.assertRaises(MemoryError):
                await self.host.execute(self.message, turn, self.args | dict(kind=kind,
                                        evidence_refs=[{'source_ref': 'recent1', 'quote': '你经常吃辣呀'}]))
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, turn, self.args | {'kind': 'episode', 'evidence': '你经常吃辣呀'})

    async def test_fabricated_ref_and_cross_turn_reuse_are_rejected(self):
        _, turn = await self.host.prepare(self.message)
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, turn, self.args | dict(evidence_refs=[{'source_ref': '123456789', 'quote': '我不吃辣'}]))
        self.message.id = 101
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, turn, self.args)
        self.channel.fetch_message.assert_not_awaited()

    async def test_changed_recent_source_is_rejected(self):
        old = self.cached(90, 55, '再练一次合唱吧')
        self.host.bot.cached_messages = [old]
        _, turn = await self.host.prepare(self.message)
        changed = self.cached(90, 55, '我不想练了')
        self.channel.fetch_message.side_effect = lambda identity: self.message if identity == 100 else changed
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, turn, self.args | {'kind': 'episode', 'evidence': '再练一次合唱吧'})

    async def test_bot_context_edit_during_embedding_invalidates_pending_episode(self):
        self.host.bot.user = SimpleNamespace(id=999)
        bot = self.cached(91, 999, '我们一起笑了', bot=True)
        self.host.bot.cached_messages = [bot]
        _, turn = await self.host.prepare(self.message)
        self.channel.fetch_message.side_effect = lambda identity: self.message if identity == 100 else bot
        async def embed(text):
            await self.host.on_raw_message_edit(SimpleNamespace(guild_id=1, channel_id=10, message_id=91,
                                                                data={'content': '改了', 'author': {'bot': True}}))
            return [1., 0.]
        self.host.service.embedder.embed.side_effect = embed
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, turn, self.args | dict(kind='episode',
                                    evidence_refs=[{'source_ref': 'recent1', 'quote': '我们一起笑了'}]))
        self.assertEqual(self.host.service.store.status(turn.scope)['count'], 0)

    async def test_empty_cache_and_disabled_channel_do_not_require_history_fetch(self):
        self.host.service.store.manage(Scope('1', '10'), action='disable')
        self.host.bot.cached_messages = [self.cached(90, 55, '不会注入关闭频道的近期原文')]
        frame, _ = await self.host.prepare(self.message)
        self.assertIn('channel_memory_enabled=false', frame)
        self.assertNotIn('不会注入关闭频道', frame)

    async def test_bot_dm_and_unreadable_channel_are_rejected(self):
        with self.assertRaises(MemoryError):
            require_scope(None, self.channel, self.user)
        self.user.bot = True
        with self.assertRaises(MemoryError):
            await self.host.prepare(self.message)
        self.user.bot = False
        self.channel.permissions_for = lambda user: SimpleNamespace(view_channel=True, read_message_history=False)
        with self.assertRaises(MemoryError):
            await self.host.prepare(self.message)

    async def test_quotes_forwarded_snapshots_and_code_are_not_write_sources(self):
        for content in ('> 我不吃辣', '```text\n我不吃辣\n```'):
            self.message.content = content
            self.assertNotIn('我不吃辣', source_text(self.message))
        self.message.content = '我不吃辣'
        self.message.message_snapshots = [object()]
        self.assertEqual(source_text(self.message), '')

    async def test_delete_edit_and_reset_remove_only_this_channels_memories(self):
        _, generation = await self.host.prepare(self.message)
        await self.host.execute(self.message, generation, self.args)
        payload = SimpleNamespace(guild_id=1, channel_id=10, message_id=100, data={'content': 'edited'})
        await self.host.on_raw_message_edit(payload)
        self.assertEqual(self.host.service.store.status(Scope('1', '10'))['count'], 0)
        with self.assertRaises(MemoryError):
            await self.host.execute(self.message, generation, self.args)
        _, generation = await self.host.prepare(self.message)
        await self.host.execute(self.message, generation, self.args)
        await self.host.clear_for_reset(1, 10)
        self.assertEqual(self.host.service.store.status(Scope('1', '10'))['count'], 0)

    def interaction(self):
        return SimpleNamespace(guild=self.guild, channel=self.channel, user=self.user,
                               response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))

    async def test_member_cannot_disable_channel_memory(self):
        interaction = self.interaction()
        await ChannelMemory.memory_command.callback(self.host, interaction, action='disable')
        self.assertIn('仅限', interaction.followup.send.call_args.args[0])
        self.assertTrue(self.host.service.store.status(Scope('1', '10'))['enabled'])

    async def test_manager_clear_needs_explicit_confirmation(self):
        self.user.guild_permissions.administrator = True
        interaction = self.interaction()
        await ChannelMemory.memory_command.callback(self.host, interaction, action='clear')
        self.assertIn('confirm=true', interaction.followup.send.call_args.args[0])
        await ChannelMemory.memory_command.callback(self.host, interaction, action='disable')
        self.assertFalse(self.host.service.store.status(Scope('1', '10'))['enabled'])
