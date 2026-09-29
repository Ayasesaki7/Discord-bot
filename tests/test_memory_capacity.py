from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chat.memory import MemoryStore, MemoryError, Scope, terms, pack
from chat import memory_capacity as capacity, memory_index
from chat.memory_maintenance import parse_review, plan_local
from chat.guild_settings import PolicyError


class CapacityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = MemoryStore(Path(folder.name)/'memory.sqlite3')
        self.scope = Scope('1', '10')

    def configure(self, **changes):
        policies = self.store.policies
        return policies.update('1', '55', changes, expected_revision=policies.get('1')['revision'])

    def save(self, index=0, **changes):
        args = dict(author='55', message=str(100+index), topic=f'话题 {index}', kind='episode',
                    content=f'一起听音乐的经历 {index}', evidence=f'一起听音乐的经历 {index}',
                    vector=[1., 0.], model='test')
        args.update(changes)
        return self.store.remember(self.scope, self.store.status(self.scope)['generation'], **args)

    def seed(self, count, *, vector=None):
        # Bulk fixtures bypass capacity policy, but use the production index migration.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for i in range(count):
                db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (f'{i:016x}', '1', '10', str(200+i), f'topic{i}', 'fact', f'ordinary entry {i}',
                            f'actual source {i}', str(1000+i), time.time()+i, 253402300799., pack(vector), 'test'))
            db.execute('PRAGMA user_version=2')
            db.commit()
        self.store = MemoryStore(self.store.path)

    def test_defaults_and_old_policy_merge(self):
        state = self.store.status(self.scope)
        self.assertEqual((state['active_target'], state['user_target'], state['budget_mb']), (3000, 300, 256))
        with self.store.connect() as db:
            db.execute('INSERT INTO guild_settings VALUES(?,?,?,?,?)', ('2', '{"recall_limit":2}', 1, time.time(), '55'))
        again = MemoryStore(self.store.path)
        self.assertEqual(again.policies.get('2')['memory_user_active'], 300)
        self.assertEqual(again.policies.get('2')['recall_limit'], 2)

    def test_pressure_archives_instead_of_rejecting_and_sources_remain(self):
        self.configure(memory_user_active=10, memory_channel_active=100)
        ids = [self.save(i) for i in range(15)]
        state = self.store.status(self.scope)
        self.assertEqual(state['count'], 15)
        self.assertLessEqual(state['active'], 10)
        self.assertGreater(state['archived'], 0)
        archived = self.store.rows(self.scope, tier='archive')
        self.assertTrue(archived[0]['sources'])
        self.assertIn(archived[0]['id'], ids)

    def test_disabled_organizer_does_not_archive_on_write(self):
        self.configure(memory_user_active=10, memory_channel_active=100, organize_enabled=False)
        for i in range(12):
            self.save(i)
        self.assertEqual(self.store.status(self.scope)['active'], 12)

    def test_protected_and_pinned_exceed_soft_target_without_loss(self):
        self.configure(memory_user_active=10, memory_channel_active=100)
        identity = self.save()
        self.store.manage(self.scope, action='pin', author='55', memory_id=identity)
        for i in range(1, 16):
            self.save(i, kind='preference')
        self.assertEqual(self.store.status(self.scope)['active'], 16)
        self.assertEqual(self.store.status(self.scope)['archived'], 0)

    def test_cold_social_and_completed_tasks_archive_not_open_tasks(self):
        a, b = self.save(), self.save(1, kind='todo')
        with self.store.connect() as db:
            db.execute('UPDATE memories SET updated=updated-100*86400')
            capacity.rebalance(db, self.scope, self.store.policies.get('1'))
        rows = {r['id']: r for r in self.store.rows(self.scope)}
        self.assertTrue(rows[a]['archived'])
        self.assertFalse(rows[b]['archived'])
        self.store.manage(self.scope, action='complete', memory_id=b, author='55')
        with self.store.connect() as db:
            capacity.rebalance(db, self.scope, self.store.policies.get('1'))
        self.assertEqual(self.store.status(self.scope)['archived'], 2)
        self.store.manage(self.scope, action='reopen', memory_id=b, author='55')
        self.assertEqual(self.store.status(self.scope)['active'], 1)

    def test_archive_search_and_scope_isolation(self):
        identity = self.save(content='那次海边烟花晚会', topic='烟花', evidence='那次海边烟花晚会')
        self.store.manage(self.scope, action='archive', memory_id=identity)
        self.assertEqual(self.store.search(self.scope, '海边烟花', automatic=True)[0]['id'], identity)
        for scope in (Scope('1','11'), Scope('2','10')):
            self.assertEqual(self.store.search(scope, '海边烟花', vector=[1.,0.], model='test'), [])

    def test_lexical_old_archive_retrieval_beyond_1000(self):
        self.seed(1100)
        identity = self.save(content='约好一起寻找海底水晶', topic='珍贵约定', evidence='约好一起寻找海底水晶')
        with self.store.connect() as db:
            db.execute('UPDATE memories SET updated=1 WHERE id=?', (identity,))
        self.store.manage(self.scope, action='archive', memory_id=identity)
        self.assertEqual(self.store.search(self.scope, '海底水晶')[0]['id'], identity)
        self.assertEqual(self.store.status(self.scope)['count'], 1101)

    def test_semantic_old_archive_retrieval_without_keyword_overlap(self):
        self.seed(1100, vector=[0.,1.])
        identity = self.save(content='钟爱薄荷饮品', topic='口味', evidence='钟爱薄荷饮品')
        with self.store.connect() as db:
            db.execute('UPDATE memories SET updated=1 WHERE id=?', (identity,))
        self.store.manage(self.scope, action='archive', memory_id=identity)
        self.assertEqual(self.store.search(self.scope, '喜欢喝什么', vector=[1.,0.], model='test')[0]['id'], identity)
        self.assertEqual(self.store.search(self.scope, '喜欢喝什么', vector=[1.,0.], model='other'), [])

    def test_candidates_bounded_and_indexes_used(self):
        self.seed(900, vector=[1.,0.])
        with self.store.connect() as db:
            result = memory_index.candidates(db, self.scope, terms('ordinary'), pack([1.,0.]), 'test', '55')
            self.assertLessEqual(len(result), 660)
            plan = db.execute('EXPLAIN QUERY PLAN SELECT memory_id FROM memory_terms WHERE guild=? AND channel=? AND term=?',
                              ('1','10','ordinary')).fetchall()
            self.assertIn('memory_term_lookup', str([tuple(r) for r in plan]))

    def test_versions_preserved_and_never_recall_old_facts(self):
        identity = self.save(kind='preference', content='喜欢香菜')
        self.save(1, topic='话题 0', kind='preference', content='现在不吃香菜', evidence='现在不吃香菜')
        version = json.loads(self.store.versions(self.scope, identity)[0]['snapshot'])
        self.assertEqual(version['memory']['content'], '喜欢香菜')
        self.assertEqual(self.store.search(self.scope, '香菜')[0]['content'], '现在不吃香菜')
        self.assertEqual(self.store.versions(Scope('2','10'), identity), [])
        # Deleted old evidence must not survive in historical snapshots.
        self.store.invalidate_source(self.scope, '100')
        self.assertEqual(self.store.versions(self.scope, identity), [])

    def test_repeated_joke_accumulates_sources_not_rows_or_versions(self):
        identity = self.save(kind='in_joke')
        self.save(1, topic='话题 0', content='一起听音乐的经历 0', kind='in_joke')
        row = self.store.rows(self.scope)[0]
        self.assertEqual(self.store.status(self.scope)['count'], 1)
        self.assertEqual(row['support_count'], 2)
        self.assertEqual(self.store.versions(self.scope, identity), [])

    def test_budget_failure_rolls_back_and_warns_before_full(self):
        identity = self.save()
        with self.store.connect() as db:
            db.execute('UPDATE memory_state SET bytes=? WHERE memory_id=?', (256*1024*1024-100, identity))
        self.assertTrue(self.store.status(self.scope)['capacity_warning'])
        with self.assertRaisesRegex(MemoryError, '预算'):
            self.save(1)
        self.assertEqual(self.store.status(self.scope)['count'], 1)
        self.assertEqual(len(self.store.rows(self.scope)[0]['sources']), 1)

    def test_management_cannot_mutate_other_author_or_scope(self):
        identity = self.save()
        for action in ('pin', 'archive', 'restore', 'complete', 'reopen', 'unpin'):
            self.assertEqual(self.store.manage(self.scope, action=action, author='56', memory_id=identity), 0)
            self.assertEqual(self.store.manage(Scope('2','10'), action=action, memory_id=identity), 0)
        self.store.manage(self.scope, action='pin', memory_id=identity, author='55')
        with self.assertRaisesRegex(MemoryError, '固定'):
            self.store.manage(self.scope, action='archive', memory_id=identity)

    def test_archive_management_invalidates_inflight_request(self):
        identity = self.save()
        generation = self.store.status(self.scope)['generation']
        self.store.manage(self.scope, action='archive', memory_id=identity)
        with self.assertRaises(MemoryError):
            self.store.remember(self.scope, generation, author='55', message='999', topic='x', kind='fact',
                                content='不允许旧写入', evidence='不允许旧写入')

    def test_migration_keeps_originals_sources_vectors_and_is_idempotent(self):
        identity = self.save()
        original = self.store.rows(self.scope)[0]
        with self.store.connect() as db:
            for table in ('memory_terms', 'memory_buckets', 'memory_state'):
                db.execute('DELETE FROM '+table)
            db.execute('PRAGMA user_version=2')
        again = MemoryStore(self.store.path)
        row = again.rows(self.scope)[0]
        for key in ('id','content','evidence','vector','sources'):
            self.assertEqual(row[key], original[key])
        self.assertEqual(again.search(self.scope, '音乐')[0]['id'], identity)
        self.assertEqual(MemoryStore(self.store.path).status(self.scope)['count'], 1)

    def test_delete_cascades_archives_versions_and_indexes(self):
        identity = self.save()
        self.save(1, topic='话题 0')
        self.store.manage(self.scope, action='clear')
        with self.store.connect() as db:
            for table in ('memories','memory_sources','memory_terms','memory_buckets','memory_versions','memory_version_sources','memory_state'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0, table)

    def test_invalid_policy_is_rejected_without_modification(self):
        for changes in ({'memory_user_active':True}, {'memory_channel_active':30001}, {'memory_budget_mb':0},
                        {'memory_archive_days':731}, {'memory_user_active':200,'memory_channel_active':100}):
            with self.subTest(changes=changes), self.assertRaises(PolicyError):
                self.configure(**changes)


if __name__ == '__main__':
    unittest.main()
