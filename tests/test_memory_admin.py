from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from chat.client import OpenAICompatibleConfig
from chat.guild_settings import DEFAULTS, PolicyError, channel_allowed, policy_for, validate_policy
from chat.memory import MemoryError, MemoryStore, Scope
from chat.memory_maintenance import MemoryOrganizer, fingerprint, parse_review, plan_local, review_messages
from chat.server_panel import PolicyModal, ServerSettingsView, require_manager


class StoreFixture:
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = MemoryStore(Path(self.folder.name) / 'test.sqlite3')
        self.scope = Scope('1', '10')

    def save(self, scope=None, **kw):
        scope = scope or self.scope
        values = dict(author='55', message='100', topic='饮食习惯', kind='preference',
                      content='用户喜欢清淡的菜', evidence='我喜欢清淡的菜', vector=[1., 0.], model='test')
        values.update(kw)
        return self.store.remember(scope, self.store.status(scope)['generation'], **values)

    def configure(self, **kw):
        p = self.store.policies
        return p.update('1', '99', kw, expected_revision=p.get('1')['revision'])


class PolicyTests(StoreFixture, unittest.TestCase):
    def test_defaults_copy_and_persistence_are_guild_local(self):
        default = self.store.policies.get('1')
        default['channel_ids'].append('10')
        self.assertEqual(self.store.policies.get('1')['channel_ids'], [])
        ids = ['10']
        self.configure(channel_mode='allowlist', channel_ids=ids, web_search_enabled=False)
        ids.append('11')
        self.assertEqual(self.store.policies.get('1')['channel_ids'], ['10'])
        self.assertTrue(self.store.policies.get('2')['web_search_enabled'])
        again = MemoryStore(self.store.path)
        self.assertFalse(again.policies.get('1')['web_search_enabled'])
        self.assertEqual(len(again.policies.audit('1')), 1)
        self.assertEqual(again.policies.audit('2'), [])

    def test_stale_panel_is_not_allowed_to_overwrite(self):
        self.configure(memory_enabled=False)
        with self.assertRaises(PolicyError):
            self.store.policies.update('1', '22', {'memory_enabled': True}, expected_revision=0)
        self.assertFalse(self.store.policies.get('1')['memory_enabled'])

    def test_invalid_secret_or_unbounded_settings_rejected(self):
        cases = [{'api_key': 'nope'}, {'recall_limit': 7}, {'recall_limit': True},
                 {'semantic_daily_limit': -1}, {'semantic_daily_limit': 7}, {'organize_interval_hours': 49},
                 {'memory_enabled': 1}, {'memory_style': 'anything'}, {'channel_ids': [10]},
                 {'channel_ids': [{}]}, {'channel_ids': ['0']}, {'channel_ids': ['10', '10']},
                 {'channel_ids': [str(i+1) for i in range(26)]}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PolicyError):
                self.configure(**value)
        self.assertEqual(self.store.policies.get('1')['revision'], 0)

    def test_scope_exclusion_and_disable_invalidate_inflight_writes(self):
        self.save()
        self.save(Scope('1', '11'))
        self.save(Scope('2', '10'))
        before = self.store.status(self.scope)['generation']
        self.configure(channel_mode='allowlist', channel_ids=['11'])
        self.assertFalse(self.store.status(self.scope)['enabled'])
        self.assertTrue(self.store.status(Scope('1', '11'))['enabled'])
        self.assertEqual(self.store.status(Scope('2', '10'))['generation'], 0)
        self.assertEqual(self.store.search(self.scope, '清淡'), [])
        self.assertEqual(len(self.store.rows(self.scope)), 1)
        with self.assertRaises(MemoryError):
            self.store.remember(self.scope, before, author='55', message='101', topic='t', kind='fact',
                                content='今天的事实', evidence='今天的事实')
        self.configure(memory_enabled=False)
        self.store.manage(Scope('1', '11'), action='enable')
        self.assertFalse(self.store.status(Scope('1', '11'))['enabled'])

    def test_facts_mode_keeps_but_does_not_recall_or_write_social(self):
        self.save(kind='episode')
        self.configure(memory_style='facts')
        self.assertEqual(len(self.store.rows(self.scope)), 1)
        self.assertEqual(self.store.search(self.scope, '清淡'), [])
        with self.assertRaises(MemoryError):
            self.save(message='101', kind='episode')
        self.configure(memory_style='social')
        self.assertEqual(len(self.store.search(self.scope, '清淡')), 1)

    def test_policy_lookup_and_fail_closed(self):
        self.configure(web_search_enabled=False)
        bot = NS(get_cog=lambda name: NS(service=NS(store=self.store)))
        self.assertFalse(policy_for(bot, 1)['web_search_enabled'])
        self.assertTrue(policy_for(bot, 2)['web_search_enabled'])
        bot._guild_policy_failed = True
        self.assertFalse(channel_allowed(policy_for(bot, 2), 10))


class OrganizerTests(StoreFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.guild = NS(id=1, me=NS(id=777))
        self.channel = NS(id=10, guild=self.guild, permissions_for=lambda who: NS(view_channel=True, read_message_history=True))
        self.chat = NS(_is_chat_guild_whitelisted=lambda identity: identity == 1,
                       _wait_for_agent_api_slot=AsyncMock(), _record_agent_api_failure=AsyncMock(),
                       client=NS(is_configured=lambda: True, config=OpenAICompatibleConfig('https://invalid.example', 'fake', 'test')))
        self.bot = NS(get_cog=lambda name: self.chat, get_guild=lambda identity: self.guild if identity == 1 else None,
                      get_channel=lambda identity: self.channel if identity == 10 else None)
        self.cog = NS(bot=self.bot, service=NS(store=self.store))
        self.organizer = MemoryOrganizer(self.cog)
        self.organizer.model_review = AsyncMock(return_value='{"pairs":[]}')

    def pair(self):
        a = self.save()
        b = self.save(topic='近期饮食', message='101', content='用户最近想吃辣菜', evidence='我最近想吃辣菜')
        return a, b

    async def test_exact_duplicate_fold_never_deletes_sources_and_undo_on_delete(self):
        a = self.save(content='用户喜欢 清淡的菜')
        b = self.save(topic='吃饭约定', message='101', content='用户喜欢  清淡的菜')
        before = [(r['id'], r['content'], r['vector'], r['sources']) for r in self.store.rows(self.scope)]
        self.configure(semantic_review_enabled=False)
        report = await self.organizer.run(self.scope)
        self.assertEqual(report['duplicates'], 1)
        after = [(r['id'], r['content'], r['vector'], r['sources']) for r in self.store.rows(self.scope)]
        self.assertEqual(before, after)
        self.assertEqual(len(self.store.search(self.scope, '清淡')), 1)
        self.store.manage(self.scope, action='delete', memory_id=b)
        self.assertEqual(self.store.search(self.scope, '清淡')[0]['id'], a)
        self.organizer.model_review.assert_not_awaited()

    async def test_semantic_relationships_are_only_scoped_hints(self):
        a, b = self.pair()
        foreign = self.save(Scope('1', '11'))
        self.organizer.model_review.return_value = json.dumps({'pairs': [dict(left=a, right=b, reason='conflict')]})
        report = await self.organizer.run(self.scope)
        self.assertEqual(report['status'], 'reviewed')
        self.assertEqual(self.store.reviews(self.scope)[0]['reason'], 'conflict')
        self.assertEqual(self.store.reviews(Scope('1', '11')), [])
        self.assertEqual(self.store.rows(self.scope)[0]['review_flags'], ['conflict'])
        self.assertEqual(len(self.store.rows(self.scope)), 2)
        batch = self.organizer.model_review.await_args.args[1]
        self.assertNotIn(foreign, {r['id'] for r in batch})

    async def test_semantic_equivalence_folds_reversibly_and_keeps_sources(self):
        a = self.save(kind='in_joke', content='昨天合唱唱错三次，大家笑了')
        b = self.save(kind='in_joke', topic='合唱笑场', message='101', content='昨日一起合唱错了三遍，大家一起笑场')
        originals = {r['id']: (r['content'], r['sources']) for r in self.store.rows(self.scope)}
        self.organizer.model_review.return_value = json.dumps({'pairs':[dict(left=a,right=b,reason='equivalent')]})
        await self.organizer.run(self.scope)
        rows = self.store.rows(self.scope)
        folded = next(r for r in rows if r['duplicate_of'])
        self.assertTrue(folded['archived'])
        self.assertEqual({r['id']: (r['content'],r['sources']) for r in rows}, originals)
        self.assertEqual(plan_local(rows, time.time())[folded['id']]['duplicate_of'], folded['duplicate_of'])
        self.assertEqual(len(self.store.search(self.scope, '合唱')), 1)
        self.store.manage(self.scope, action='restore', memory_id=folded['id'])
        self.assertFalse(any(r['duplicate_of'] for r in self.store.rows(self.scope)))

    async def test_equivalence_never_folds_two_pins_or_a_conflict_pair(self):
        a, b = self.pair()
        for identity in (a,b):
            self.store.manage(self.scope, action='pin', memory_id=identity)
        self.organizer.model_review.return_value = json.dumps({'pairs':[dict(left=a,right=b,reason='equivalent')]})
        await self.organizer.run(self.scope)
        self.assertFalse(any(r['archived'] for r in self.store.rows(self.scope)))
        self.assertFalse(any(r['duplicate_of'] for r in self.store.rows(self.scope)))

    async def test_uncertain_duplicate_does_not_auto_fold(self):
        a, b = self.pair()
        self.organizer.model_review.return_value = json.dumps({'pairs':[dict(left=a,right=b,reason='possible_duplicate')]})
        await self.organizer.run(self.scope)
        self.assertFalse(any(r['duplicate_of'] for r in self.store.rows(self.scope)))

    async def test_invalid_foreign_pair_does_not_write_or_retry(self):
        a, b = self.pair()
        foreign = self.save(Scope('2', '10'))
        self.organizer.model_review.return_value = json.dumps({'pairs': [dict(left=a, right=foreign, reason='conflict')]})
        report = await self.organizer.run(self.scope)
        self.assertEqual(report['status'], 'model_failed')
        self.assertEqual(self.store.reviews(self.scope), [])
        self.assertEqual(self.store.status(self.scope)['count'], 2)
        self.assertEqual((await self.organizer.run(self.scope, force=True))['status'], 'cooldown')
        self.organizer.model_review.assert_awaited_once()

    async def test_mutations_during_review_discard_entire_result(self):
        for mutation in ('disable', 'change', 'clear', 'policy'):
            with self.subTest(mutation=mutation):
                self.store.manage(self.scope, action='clear')
                self.store.manage(self.scope, action='enable')
                self.configure(memory_enabled=True)
                with self.store.connect() as db:
                    db.execute('DELETE FROM memory_review_calls')
                a, b = self.pair()
                async def change(chat, rows):
                    if mutation == 'change':
                        self.save(message='102', content='新的饮食偏好')
                    elif mutation == 'policy':
                        self.configure(memory_enabled=False)
                    else:
                        self.store.manage(self.scope, action=mutation)
                    return json.dumps({'pairs': [dict(left=a, right=b, reason='conflict')]})
                self.organizer.model_review.side_effect = change
                report = await self.organizer.run(self.scope)
                self.assertEqual(report['status'], 'changed')
                self.assertEqual(self.store.reviews(self.scope), [])

    async def test_persistent_daily_budget_global_cooldown_and_guild_isolation(self):
        self.configure(semantic_daily_limit=1)
        p = self.store.policies.get('1')
        self.assertTrue(self.organizer.reserve_model(self.scope, p))
        self.assertFalse(self.organizer.reserve_model(Scope('2', '10'), self.store.policies.get('2')))
        with self.store.connect() as db:
            db.execute('UPDATE memory_review_calls SET started=?', (time.time()-1000,))
        self.assertFalse(self.organizer.reserve_model(self.scope, p))
        again = MemoryStore(self.store.path)
        other = MemoryOrganizer(NS(bot=self.bot, service=NS(store=again)))
        self.assertFalse(other.reserve_model(self.scope, again.policies.get('1')))
        self.assertTrue(other.reserve_model(Scope('2', '10'), again.policies.get('2')))

    async def test_cancel_preserves_data_and_budget_without_pending_worker(self):
        self.pair()
        started = asyncio.Event()
        async def wait(chat, rows):
            started.set()
            await asyncio.Event().wait()
        self.organizer.model_review.side_effect = wait
        task = asyncio.create_task(self.organizer.run(self.scope))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.organizer.lock.locked())
        self.assertEqual(self.store.status(self.scope)['count'], 2)
        self.assertEqual(self.store.reviews(self.scope), [])
        self.assertFalse(self.organizer.reserve_model(self.scope, self.store.policies.get('1')))

    async def test_disabled_busy_or_inaccessible_scope_never_calls_model(self):
        self.pair()
        self.configure(organize_enabled=False)
        self.assertEqual((await self.organizer.run(self.scope))['status'], 'disabled')
        self.configure(organize_enabled=True)
        self.channel.permissions_for = lambda user: NS(view_channel=False, read_message_history=False)
        self.assertEqual((await self.organizer.run(self.scope))['status'], 'disabled')
        self.chat._message_queue = NS(active=lambda channel_id: True)
        self.assertEqual((await self.organizer.run(self.scope))['status'], 'busy')
        self.assertEqual((await self.organizer.run(Scope('2', '10')))['status'], 'unavailable')
        self.organizer.model_review.assert_not_awaited()

    async def test_stale_impression_labels_do_not_delete_it(self):
        old = time.time()-121*86400
        self.save(kind='impression', sources=[dict(message='100', author='55', evidence='我喜欢清淡的菜', observed=old, support=True)])
        report = await self.organizer.run(self.scope)
        self.assertEqual(report['stale'], 1)
        self.assertTrue(self.store.rows(self.scope)[0]['stale'])

    async def test_model_client_has_no_retry_tools_and_does_not_modify_live_config(self):
        rows = []
        original = self.chat.client.config
        self.chat.client.config.enable_web_search = True
        fake = NS(create_chat_completion=AsyncMock(return_value='{"pairs":[]}'))
        with patch('chat.memory_maintenance.OpenAICompatibleClient', return_value=fake) as client:
            await MemoryOrganizer.model_review(self.organizer, self.chat, rows)
        cfg = client.call_args.args[0]
        self.assertEqual((cfg.retry_count, cfg.timeout_seconds, cfg.max_tokens), (0, 45, 1500))
        self.assertFalse(cfg.enable_web_search)
        self.assertTrue(original.enable_web_search)
        self.chat._wait_for_agent_api_slot.assert_awaited_once()

    def test_parser_rejects_cross_author_kind_and_unknown_instructions(self):
        rows = [dict(id='a', author='55', kind='fact'), dict(id='b', author='56', kind='fact')]
        with self.assertRaises(MemoryError):
            parse_review('{"pairs":[{"left":"a","right":"b","reason":"conflict"}]}', rows)
        with self.assertRaises(MemoryError):
            parse_review('{"pairs":[],"delete_all":true}', rows)
        self.assertEqual(parse_review('```json\n{"pairs":[]}\n```', rows), [])


class PanelTests(StoreFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.member = NS(id=55, bot=False, guild_permissions=NS(administrator=True))
        self.guild = NS(id=1, name='test', owner_id=11, fetch_member=AsyncMock(return_value=self.member),
                        get_channel_or_thread=lambda identity: None)
        self.chat = NS(owner_user_id=99, _is_chat_guild_whitelisted=lambda identity: True)
        self.bot = NS(get_cog=lambda name: self.chat, fetch_channel=AsyncMock())
        self.cog = NS(service=NS(store=self.store), bot=self.bot)
        self.interaction = NS(guild=self.guild, user=NS(id=55),
                              response=NS(defer=AsyncMock(), is_done=lambda: True, send_message=AsyncMock()),
                              followup=NS(send=AsyncMock()))

    async def test_live_admin_owner_developer_and_cross_guild(self):
        await require_manager(self.bot, self.interaction, 1)
        self.member.guild_permissions.administrator = False
        with self.assertRaises(PolicyError):
            await require_manager(self.bot, self.interaction, 1)
        self.guild.owner_id = 55
        await require_manager(self.bot, self.interaction, 1)
        self.interaction.user.id = 99
        await require_manager(self.bot, self.interaction, 1)
        with self.assertRaises(PolicyError):
            await require_manager(self.bot, self.interaction, 2)

    async def test_panel_bound_to_opener_and_revocation_rechecked(self):
        view = ServerSettingsView(self.cog, self.interaction)
        await view.save(self.interaction, {'web_search_enabled': False})
        self.member.guild_permissions.administrator = False
        with self.assertRaises(PolicyError):
            await view.save(self.interaction, {'web_search_enabled': True})
        self.interaction.user.id = 11
        with self.assertRaises(PolicyError):
            await view.authorize(self.interaction)
        self.assertFalse(self.store.policies.get('1')['web_search_enabled'])
        self.assertTrue(self.store.policies.get('2')['web_search_enabled'])
        view.stop()

    async def test_channel_modal_rejects_foreign_guild_and_stale_revision(self):
        view = ServerSettingsView(self.cog, self.interaction)
        modal = PolicyModal(view, channels=True)
        modal.ids._value = '<#200>'
        self.bot.fetch_channel.return_value = NS(guild=NS(id=2), send=AsyncMock())
        await modal.on_submit(self.interaction)
        self.assertEqual(self.store.policies.get('1')['revision'], 0)
        self.bot.fetch_channel.return_value = NS(guild=self.guild, send=AsyncMock())
        self.configure(memory_enabled=False)
        await modal.on_submit(self.interaction)
        self.assertEqual(self.store.policies.get('1')['channel_mode'], 'all')
        fresh = PolicyModal(view, channels=True)
        fresh.revision = 1
        fresh.ids._value = '<#200> 201'
        await fresh.on_submit(self.interaction)
        self.assertEqual(self.store.policies.get('1')['channel_ids'], ['200', '201'])
        view.stop()

    async def test_capacity_modal_is_guild_local_and_bound_to_live_manager(self):
        view = ServerSettingsView(self.cog, self.interaction)
        modal = PolicyModal(view, capacity=True)
        self.assertEqual(len(modal.fields), 4)
        for key, field in modal.fields.items():
            field._value = str(self.store.policies.get('1')[key])
        modal.fields['memory_user_active']._value = '400'
        await modal.on_submit(self.interaction)
        self.assertEqual(self.store.policies.get('1')['memory_user_active'], 400)
        self.assertEqual(self.store.policies.get('2')['memory_user_active'], 300)
        self.member.guild_permissions.administrator = False
        modal = PolicyModal(view, capacity=True)
        modal.fields['memory_user_active']._value = '500'
        await modal.on_submit(self.interaction)
        self.assertEqual(self.store.policies.get('1')['memory_user_active'], 400)
        view.stop()


if __name__ == '__main__':
    unittest.main()
