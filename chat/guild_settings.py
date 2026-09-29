"""Guild-local, non-secret policy. Never grants global developer privileges."""
from __future__ import annotations

import json
import time
import threading


DEFAULTS = dict(chat_enabled=True, web_search_enabled=True, tts_enabled=True, memory_enabled=True,
                memory_style='social', recall_limit=3, organize_enabled=True,
                organize_interval_hours=6, semantic_review_enabled=True, semantic_daily_limit=2,
                memory_user_active=300, memory_channel_active=3000, memory_archive_days=90,
                memory_budget_mb=256,
                channel_mode='all', channel_ids=[])


class PolicyError(ValueError):
    pass


def channel_allowed(policy, channel_id):
    return policy['chat_enabled'] and (policy['channel_mode'] == 'all' or str(channel_id) in policy['channel_ids'])


def policy_for(bot, guild_id):
    if getattr(bot, '_guild_policy_failed', False) is True:
        return dict(DEFAULTS, channel_ids=[], revision=0, chat_enabled=False,
                    web_search_enabled=False, memory_enabled=False)
    getter = getattr(bot, 'get_cog', None)
    memory = getter('ChannelMemory') if callable(getter) else None
    store = getattr(getattr(memory, 'service', None), 'store', None)
    policies = getattr(store, 'policies', None)
    if guild_id is not None and isinstance(policies, GuildPolicyStore):
        return policies.get(guild_id)
    return dict(DEFAULTS, channel_ids=[], revision=0)


def validate_policy(value):
    if set(value) != set(DEFAULTS):
        raise PolicyError('配置字段不合法。')
    for key, default in DEFAULTS.items():
        if isinstance(default, bool) and type(value[key]) is not bool:
            raise PolicyError('开关必须是布尔值。')
    for key, low, high in [('recall_limit', 1, 6), ('organize_interval_hours', 1, 48), ('semantic_daily_limit', 0, 6),
                           ('memory_user_active', 10, 3000), ('memory_channel_active', 100, 30000),
                           ('memory_archive_days', 7, 730), ('memory_budget_mb', 16, 1024)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise PolicyError(f'{key} 必须介于 {low}～{high}。')
    if value['memory_user_active'] > value['memory_channel_active']:
        raise PolicyError('每人活跃目标不能大于频道活跃目标。')
    if value['memory_style'] not in {'facts', 'social'} or value['channel_mode'] not in {'all', 'allowlist'}:
        raise PolicyError('记忆模式或频道范围不合法。')
    ids = value['channel_ids']
    if not isinstance(ids, list) or len(ids) > 25:
        raise PolicyError('最多设置 25 个不重复的当前服务器频道 ID。')
    if any(not isinstance(v, str) or not v.isascii() or not v.isdigit() or int(v) <= 0 for v in ids):
        raise PolicyError('最多设置 25 个不重复的当前服务器频道 ID。')
    if len(set(ids)) != len(ids):
        raise PolicyError('最多设置 25 个不重复的当前服务器频道 ID。')


class GuildPolicyStore:
    def __init__(self, store):
        self.store = store
        self.cache = {}
        self._cache_lock = threading.Lock()
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild TEXT PRIMARY KEY, config TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL, actor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS guild_settings_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL, actor TEXT NOT NULL,
                    updated REAL NOT NULL, changed TEXT NOT NULL);
            ''')
            for row in db.execute('SELECT * FROM guild_settings'):
                value = dict(DEFAULTS) | json.loads(row['config'])
                validate_policy(value)
                self.cache[row['guild']] = value | {'revision': row['revision'], 'channel_ids': list(value['channel_ids'])}

    @staticmethod
    def _guild(guild):
        value = str(guild)
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            raise PolicyError('无效服务器。')
        return value

    def get(self, guild, db=None):
        guild = self._guild(guild)
        if db is not None:
            row = db.execute('SELECT config,revision FROM guild_settings WHERE guild=?', (guild,)).fetchone()
            return (dict(DEFAULTS) | json.loads(row['config']) | {'revision': row['revision']}) if row else dict(DEFAULTS, revision=0)
        with self._cache_lock:
            value = self.cache.get(guild, dict(DEFAULTS, revision=0))
            return dict(value, channel_ids=list(value['channel_ids']))

    def update(self, guild, actor, changes, *, expected_revision):
        guild = self._guild(guild)
        if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
            raise PolicyError('不允许修改全局配置、凭据或其他服务器。')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self.get(guild, db)
            if old['revision'] != expected_revision:
                raise PolicyError('此面板已过期：其他管理员刚修改了配置，请刷新后再试。')
            revision = old.pop('revision') + 1
            value = old | changes
            validate_policy(value)
            db.execute('INSERT INTO guild_settings VALUES(?,?,?,?,?) ON CONFLICT(guild) DO UPDATE SET '
                       'config=excluded.config,revision=excluded.revision,updated=excluded.updated,actor=excluded.actor',
                       (guild, json.dumps(value), revision, time.time(), str(actor)))
            db.execute('INSERT INTO guild_settings_audit(guild,actor,updated,changed) VALUES(?,?,?,?)',
                       (guild, str(actor), time.time(), json.dumps(changes)))
            db.execute('DELETE FROM guild_settings_audit WHERE guild=? AND id NOT IN '
                       '(SELECT id FROM guild_settings_audit WHERE guild=? ORDER BY id DESC LIMIT 100)', (guild, guild))
            # Invalidate all in-flight memory writes/maintenance in this guild.
            db.execute('UPDATE channels SET generation=generation+1 WHERE guild=?', (guild,))
            db.commit()
            with self._cache_lock:
                if revision > self.cache.get(guild, {}).get('revision', -1):
                    self.cache[guild] = value | {'revision': revision, 'channel_ids': list(value['channel_ids'])}
        return self.get(guild)

    def audit(self, guild):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT actor,updated,changed FROM guild_settings_audit '
                                                    'WHERE guild=? ORDER BY id DESC LIMIT 5', (self._guild(guild),))]
