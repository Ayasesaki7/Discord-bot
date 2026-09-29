"""Source-backed key memories. Every storage query requires the exact channel scope."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import math
import os
import re
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .guild_settings import GuildPolicyStore, channel_allowed
from . import memory_capacity as capacity, memory_index

FACT_KINDS = {'preference', 'relationship', 'agreement', 'project', 'todo', 'fact'}
SOCIAL_KINDS = {'episode', 'in_joke', 'impression'}
KINDS = FACT_KINDS | SOCIAL_KINDS
# Durable facts do not silently expire merely because nobody mentioned them.
# Updated state replaces an older topic; users retain explicit deletion control.
NO_EXPIRY = 253402300799.0
SECRET = re.compile(r'(?i)(?:api[_ -]?key|token|password|密码|私钥|cookie)\s*[:=：]|'
                    r'-----BEGIN .*PRIVATE KEY|\b(?:sk|ghp|gho)-?[A-Za-z0-9_]{20,}')


class MemoryError(ValueError):
    pass


@dataclass(frozen=True)
class Scope:
    guild: str
    channel: str

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.isascii() or not v.isdigit()
               or int(v) <= 0 for v in (self.guild, self.channel)):
            raise MemoryError('记忆只能绑定有效的当前服务器和频道。')


def terms(text):
    """Chinese bigrams plus exact Latin words; avoids FTS unicode61's Chinese gap."""
    result = set(re.findall(r'[a-z0-9_]+', text.lower()))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        result.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return result


def pack(vector):
    if vector is None:
        return None
    if not isinstance(vector, list) or not 1 <= len(vector) <= 2048:
        raise MemoryError('Invalid memory embedding')
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or norm <= 0:
        raise MemoryError('Invalid memory embedding')
    return struct.pack(f'<{len(values)}f', *(v / norm for v in values))


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS channels (
                    guild TEXT NOT NULL, channel TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, generation INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(guild,channel));
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, guild TEXT NOT NULL, channel TEXT NOT NULL,
                    author TEXT NOT NULL, topic TEXT NOT NULL, kind TEXT NOT NULL,
                    content TEXT NOT NULL, evidence TEXT NOT NULL, message TEXT NOT NULL,
                    updated REAL NOT NULL, expires REAL NOT NULL, vector BLOB, model TEXT NOT NULL,
                    UNIQUE(guild,channel,author,topic));
                CREATE INDEX IF NOT EXISTS memory_scope ON memories(guild,channel,updated);
                CREATE TABLE IF NOT EXISTS memory_sources (
                    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    guild TEXT NOT NULL, channel TEXT NOT NULL, message TEXT NOT NULL,
                    author TEXT NOT NULL, evidence TEXT NOT NULL, observed REAL NOT NULL,
                    support INTEGER NOT NULL,
                    PRIMARY KEY(memory_id,message));
                CREATE INDEX IF NOT EXISTS memory_source_scope ON memory_sources(guild,channel,message);
                CREATE TABLE IF NOT EXISTS memory_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL, channel TEXT NOT NULL,
                    left_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    right_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE, reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(guild,channel,left_id,right_id,reason));
                CREATE INDEX IF NOT EXISTS memory_review_scope ON memory_reviews(guild,channel,status);
                CREATE TABLE IF NOT EXISTS memory_labels (
                    memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                    duplicate_of TEXT REFERENCES memories(id) ON DELETE SET NULL, stale INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS memory_maintenance (
                    guild TEXT NOT NULL, channel TEXT NOT NULL, last_run REAL NOT NULL DEFAULT 0,
                    fingerprint TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(guild,channel));
                CREATE TABLE IF NOT EXISTS memory_review_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL, channel TEXT NOT NULL,
                    started REAL NOT NULL);
            ''')
            capacity.initialize(db)
            memory_index.initialize(db)
            # Additive migration: retain every v1 memory and its original evidence.
            db.execute('BEGIN IMMEDIATE')
            if db.execute('PRAGMA user_version').fetchone()[0] < 2:
                db.execute('INSERT OR IGNORE INTO memory_sources '
                           'SELECT id,guild,channel,message,author,evidence,updated,1 FROM memories')
                db.execute('PRAGMA user_version=2')
            if db.execute('PRAGMA user_version').fetchone()[0] < 3:
                for row in db.execute('SELECT * FROM memories').fetchall():
                    memory_index.update(db, row, terms)
                    capacity.refresh_bytes(db, row['id'])
                db.execute('PRAGMA user_version=3')
            db.commit()
        if os.name != 'nt':
            path.chmod(0o600)
        self.policies = GuildPolicyStore(self)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA secure_delete=ON')
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _settings(db, scope):
        db.execute('INSERT OR IGNORE INTO channels(guild,channel) VALUES(?,?)', (scope.guild, scope.channel))
        return db.execute('SELECT enabled,generation FROM channels WHERE guild=? AND channel=?',
                          (scope.guild, scope.channel)).fetchone()

    def status(self, scope):
        with self.connect() as db:
            row = self._settings(db, scope)
            used = capacity.usage(db, scope)
            policy = self.policies.get(scope.guild, db)
            enabled = bool(row['enabled']) and policy['memory_enabled'] and channel_allowed(policy, scope.channel)
            return {'enabled': enabled, 'channel_enabled': bool(row['enabled']),
                    'generation': row['generation'], **used,
                    'budget_mb': policy['memory_budget_mb'],
                    'capacity_warning': used['bytes'] >= policy['memory_budget_mb']*1024*1024*.8,
                    'active_target': policy['memory_channel_active'], 'user_target': policy['memory_user_active']}

    def scopes(self, *, limit=1000):
        with self.connect() as db:
            rows = db.execute('SELECT c.guild,c.channel FROM channels c WHERE EXISTS '
                              '(SELECT 1 FROM memories m WHERE m.guild=c.guild AND m.channel=c.channel) '
                              'ORDER BY c.guild,c.channel LIMIT ?',
                              (min(max(limit, 1), 10000),)).fetchall()
            return [Scope(row['guild'], row['channel']) for row in rows]

    def reviews(self, scope, *, limit=20, status='open'):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM memory_reviews WHERE guild=? AND channel=? AND status=? ORDER BY updated DESC LIMIT ?',
                (scope.guild, scope.channel, status, min(max(limit, 1), 100))).fetchall()]

    def remember(self, scope, generation, *, author, message, topic, kind, content, evidence,
                 vector=None, model='', sources=None, mode='replace'):
        if kind not in KINDS or not 1 <= len(topic) <= 80 or not 1 <= len(content) <= 500:
            raise MemoryError('记忆类型、主题或长度不合法。')
        if not 3 <= len(evidence) <= 600 or SECRET.search(topic + '\n' + content + '\n' + evidence):
            raise MemoryError('缺少来源原文，或内容可能含凭据，未保存。')
        if not str(author).isdigit() or not str(message).isdigit():
            raise MemoryError('Invalid source identity')
        encoded = pack(vector)
        now = time.time()
        if mode not in {'replace', 'reinforce'} or (mode == 'reinforce' and kind != 'impression'):
            raise MemoryError('只有印象支持 reinforce；纠正或改变说法请使用 replace。')
        sources = sources or [dict(message=str(message), author=str(author), evidence=evidence,
                                   observed=now, support=True)]
        if len(sources) > 4 or not any(s['support'] and s['author'] == str(author) for s in sources):
            raise MemoryError('需要当前发言人自己的互动依据，不能只用他人评价或 BOT 回答建人设。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._settings(db, scope)
            policy = self.policies.get(scope.guild, db)
            if (not state['enabled'] or not policy['memory_enabled'] or not channel_allowed(policy, scope.channel)
                    or state['generation'] != generation):
                raise MemoryError('频道记忆已关闭或管理状态已变更，本次旧请求不再写入。')
            if policy['memory_style'] == 'facts' and kind in SOCIAL_KINDS:
                raise MemoryError('本服务器只记录事实与约定，不写入经历、梗和印象。')
            db.execute('DELETE FROM memories WHERE guild=? AND channel=? AND expires<=?',
                       (scope.guild, scope.channel, now))
            old = db.execute('SELECT * FROM memories WHERE guild=? AND channel=? AND author=? '
                             'AND (topic=? OR (content=? AND kind=?)) ORDER BY (topic=?) DESC LIMIT 1',
                             (scope.guild, scope.channel, author, topic, content, kind, topic)).fetchone()
            if old and int(old['message']) > int(message):
                raise MemoryError('较旧发言不能覆盖较新的记忆。')
            written = db.execute('SELECT count(*) FROM memories WHERE guild=? AND channel=? AND message=?',
                                 (scope.guild, scope.channel, message)).fetchone()[0]
            if written >= 3 and (not old or old['message'] != message):
                raise MemoryError('每条发言最多保留 3 条关键记忆。')
            if not old and capacity.usage(db, scope)['count'] >= 50000:
                raise MemoryError('频道总存储达到 50000 条安全上限；请管理归档或提高容量方案，不要反复重试。旧记忆未删除。')
            identity = old['id'] if old else uuid.uuid4().hex[:16]
            previous = db.execute('SELECT * FROM memory_sources WHERE memory_id=?', (identity,)).fetchall()
            if mode == 'reinforce' and old and (old['kind'] != kind or old['content'] != content):
                raise MemoryError('加强印象需要沿用原来的准确表述；内容变化应 replace，重新积累依据。')
            if (old and old['content'] == content and old['kind'] == kind
                    and {s['message'] for s in sources} <= {s['message'] for s in previous}):
                # A tool retry cannot strengthen or refresh an impression.
                return identity
            if old:
                if old['content'] != content or old['kind'] != kind:
                    capacity.save_version(db, identity)
                db.execute('DELETE FROM memory_labels WHERE memory_id=? OR duplicate_of=?', (identity, identity))
                db.execute('DELETE FROM memory_reviews WHERE left_id=? OR right_id=?', (identity, identity))
            db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'kind=excluded.kind,content=excluded.content,evidence=excluded.evidence,message=excluded.message,'
                       'updated=excluded.updated,expires=excluded.expires,vector=excluded.vector,model=excluded.model',
                       (identity, scope.guild, scope.channel, author, old['topic'] if old else topic, kind,
                        content, evidence, message, now, NO_EXPIRY, encoded, model))
            # A repeated fact/joke adds provenance; a changed statement starts a new version.
            if mode != 'reinforce' and not (old and old['content'] == content and old['kind'] == kind):
                db.execute('DELETE FROM memory_sources WHERE memory_id=?', (identity,))
            for source in sources:
                db.execute('INSERT OR IGNORE INTO memory_sources VALUES(?,?,?,?,?,?,?,?)',
                           (identity, scope.guild, scope.channel, source['message'], source['author'],
                            source['evidence'], source['observed'], int(source['support'])))
            # Bounded evidence history; retain the latest 16 distinct observations.
            db.execute('DELETE FROM memory_sources WHERE memory_id=? AND message NOT IN '
                       '(SELECT message FROM memory_sources WHERE memory_id=? ORDER BY observed DESC LIMIT 16)',
                       (identity, identity))
            memory_index.update(db, db.execute('SELECT * FROM memories WHERE id=?', (identity,)).fetchone(), terms)
            capacity.refresh_bytes(db, identity)
            if old and (old['content'] != content or old['kind'] != kind):
                db.execute('UPDATE memory_state SET archived=0,completed=0 WHERE memory_id=?', (identity,))
            used = capacity.usage(db, scope)
            if used['bytes'] > policy['memory_budget_mb']*1024*1024:
                raise MemoryError('本频道记忆存储预算已满；旧记忆与归档未删除，请在服务器设置提高预算或手动整理，不要反复重试。')
            capacity.rebalance(db, scope, policy)
            db.commit()
            return identity

    def rows(self, scope, *, offset=0, limit=20, include_vectors=False, ids=None, tier='all'):
        with self.connect() as db:
            channel = self._settings(db, scope)
            policy = self.policies.get(scope.guild, db)
            if include_vectors and (not channel['enabled'] or not policy['memory_enabled'] or not channel_allowed(policy, scope.channel)):
                return []
            # Scope filtering happens in SQL BEFORE any lexical or vector scoring.
            if ids is not None and not ids:
                return []
            extra, params = '', [scope.guild, scope.channel, time.time()]
            if tier in {'active', 'archive'}:
                extra += ' AND s.archived=?'
                params.append(int(tier == 'archive'))
            if ids is not None:
                extra += ' AND m.id IN (' + ','.join('?' for _ in ids) + ')'
                params.extend(ids)
            rows = db.execute('SELECT m.*,s.archived,s.pinned,s.completed,s.last_used,s.hits FROM memories m '
                              'JOIN memory_state s ON s.memory_id=m.id WHERE m.guild=? AND m.channel=? AND m.expires>? '
                              + extra + ' ORDER BY m.updated DESC,m.id LIMIT ? OFFSET ?',
                              (*params, min(limit, 50000), max(0, offset))).fetchall()
            selected = [r['id'] for r in rows]
            sources = []
            for start in range(0, len(selected), 400):
                chunk = selected[start:start+400]
                sources.extend(db.execute('SELECT * FROM memory_sources WHERE guild=? AND channel=? AND memory_id IN ('
                                           + ','.join('?' for _ in chunk) + ') ORDER BY observed DESC',
                                           (scope.guild, scope.channel, *chunk)).fetchall())
            labels, reviews = {}, []
            for start in range(0, len(selected), 400):
                chunk = selected[start:start+400]
                marks = ','.join('?' for _ in chunk)
                labels.update({r['memory_id']: dict(r) for r in db.execute(
                    'SELECT l.* FROM memory_labels l JOIN memories m ON m.id=l.memory_id '
                    f'WHERE m.guild=? AND m.channel=? AND m.id IN ({marks})', (scope.guild, scope.channel, *chunk))})
                reviews.extend(db.execute('SELECT * FROM memory_reviews WHERE guild=? AND channel=? AND status=? '
                                           f'AND (left_id IN ({marks}) OR right_id IN ({marks}))',
                                           (scope.guild, scope.channel, 'open', *chunk, *chunk)).fetchall())
            by_id = {}
            for source in sources:
                by_id.setdefault(source['memory_id'], []).append(dict(source))
            result = []
            for raw in rows:
                row = dict(raw)
                row['sources'] = by_id.get(row['id'], [])
                row['duplicate_of'] = labels.get(row['id'], {}).get('duplicate_of')
                row['stale'] = bool(labels.get(row['id'], {}).get('stale'))
                row['review_flags'] = sorted({r['reason'] for r in reviews if row['id'] in (r['left_id'], r['right_id'])})
                own = [s['observed'] for s in row['sources'] if s['support']]
                row['support_count'] = len(own)
                row['support_span'] = max(own) - min(own) if own else 0
                row['last_observed'] = max(own) if own else row['updated']
                row['confidence'] = ('supported' if len(own) >= 2 and row['support_span'] >= 6 * 3600
                                     else 'tentative') if row['kind'] == 'impression' else 'attributed'
                result.append(row)
            return result

    def search(self, scope, query, *, vector=None, model='', limit=6, author=None, recent='', automatic=False):
        q = terms(query)
        context_terms = terms(recent)
        encoded = pack(vector)
        with self.connect() as db:
            state, policy = self._settings(db, scope), self.policies.get(scope.guild, db)
            if not state['enabled'] or not policy['memory_enabled'] or not channel_allowed(policy, scope.channel):
                return []
            ids = memory_index.candidates(db, scope, q | context_terms, encoded, model, author)
        rows = self.rows(scope, ids=ids, limit=800, include_vectors=True)
        # A folded archive may match words not present in the representative.
        # Resolve its representative inside this same scope and rank that record
        # using the alias terms; do not lose old wording at the candidate boundary.
        present = {row['id'] for row in rows}
        missing = sorted({row['duplicate_of'] for row in rows if row['duplicate_of']} - present)
        if missing:
            rows += self.rows(scope, ids=missing, limit=800, include_vectors=True)
        aliases = {}
        by_id = {row['id']: row for row in rows}
        for row in rows:
            target = by_id.get(row['duplicate_of'])
            if target and (target['author'], target['kind']) == (row['author'], row['kind']):
                aliases.setdefault(target['id'], set()).update(terms(row['topic']+' '+row['content']))
        query_vector = struct.unpack(f'<{len(encoded)//4}f', encoded) if encoded else None
        scored = []
        policy = self.policies.get(scope.guild)
        for row in rows:
            if row['duplicate_of']:
                continue
            if policy['memory_style'] == 'facts' and row['kind'] in SOCIAL_KINDS:
                continue
            lex = terms(row['topic'] + ' ' + row['content']) | aliases.get(row['id'], set())
            lexical = len(q & lex) / max(1, len(q))
            context_overlap = len(context_terms & lex) / max(1, len(context_terms))
            similarity = 0.0
            if (query_vector and row['vector'] and row['model'] == model
                    and len(row['vector']) == len(encoded)):
                candidate = struct.unpack(f'<{len(query_vector)}f', row['vector'])
                similarity = sum(a * b for a, b in zip(candidate, query_vector))
            age_days = max(0, (time.time() - row['last_observed']) / 86400)
            own = row['author'] == str(author)
            if automatic and row['kind'] == 'impression' and (row['confidence'] != 'supported' or age_days > 120):
                continue
            relevant = lexical > 0 or context_overlap > 0 or similarity >= 0.35
            familiarity = automatic and own and row['kind'] == 'impression' and age_days <= 30 and not row['archived']
            if not relevant and not familiarity:
                continue
            score = (0.4 * lexical + 0.1 * context_overlap + 0.5 * max(0, similarity)) if relevant else 0.01
            if relevant:
                score += 0.08 * own + 0.04 * math.exp(-age_days / 90)
            if row['kind'] == 'impression':
                score *= (1.0 if row['confidence'] == 'supported' else 0.35) * 2 ** (-age_days / 30)
            scored.append((score, row))
        scored.sort(key=lambda pair: (pair[0], pair[1]['updated']), reverse=True)
        selected, social_count, impression_count, used_chars = [], 0, 0, 0
        for _, row in scored:
            social = row['kind'] in SOCIAL_KINDS
            impression = row['kind'] == 'impression'
            if automatic and ((social and social_count >= 2) or (impression and impression_count >= 1)):
                continue
            public = self.public(row)
            size = len(json.dumps(public, ensure_ascii=False))
            if used_chars + size > 6000:
                continue
            selected.append(public)
            used_chars += size
            social_count += social
            impression_count += impression
            if len(selected) >= max(1, min(limit, 10)):
                break
        if selected:
            with self.connect() as db:
                db.executemany('UPDATE memory_state SET last_used=?,hits=min(hits+1,100000) WHERE memory_id=?',
                               [(time.time(), row['id']) for row in selected])
        return selected

    @staticmethod
    def public(row):
        sources = [{k: s[k] for k in ('message', 'author', 'evidence', 'observed', 'support')}
                   | {'url': f'https://discord.com/channels/{row["guild"]}/{row["channel"]}/{s["message"]}'}
                   for s in row['sources'][:4]]
        return {key: row[key] for key in ('id', 'author', 'topic', 'kind', 'content', 'evidence',
                                          'message', 'updated', 'expires', 'confidence', 'support_count', 'last_observed',
                                          'duplicate_of', 'stale', 'review_flags', 'archived', 'pinned', 'completed')} | {
            'sources': sources,
            'source_url': next((s['url'] for s in sources if s['support']),
                               f'https://discord.com/channels/{row["guild"]}/{row["channel"]}/{row["message"]}')}

    def manage(self, scope, *, action, author=None, memory_id=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._settings(db, scope)
            if action in {'enable', 'disable'}:
                db.execute('UPDATE channels SET enabled=?,generation=generation+1 WHERE guild=? AND channel=?',
                           (int(action == 'enable'), scope.guild, scope.channel))
                count = 0
            elif action in {'pin', 'unpin', 'archive', 'restore', 'complete', 'reopen'}:
                row = db.execute('SELECT m.*,s.pinned FROM memories m JOIN memory_state s ON s.memory_id=m.id '
                                 'WHERE m.guild=? AND m.channel=? AND m.id=?',
                                 (scope.guild, scope.channel, memory_id)).fetchone()
                if not row or (author is not None and row['author'] != str(author)):
                    return 0
                if action == 'archive' and row['pinned']:
                    raise MemoryError('固定记忆不能归档，请先取消固定。')
                if action in {'complete', 'reopen'} and row['kind'] != 'todo':
                    raise MemoryError('只有待办记忆可以标记完成/重新打开。')
                updates = {'pin': 'pinned=1,archived=0', 'unpin': 'pinned=0', 'archive': 'archived=1',
                           'restore': 'archived=0,last_used='+str(time.time()), 'complete': 'completed=1', 'reopen': 'completed=0,archived=0'}
                db.execute('UPDATE memory_state SET '+updates[action]+' WHERE memory_id=?', (memory_id,))
                if action in {'restore', 'pin'}:
                    db.execute('DELETE FROM memory_labels WHERE memory_id=?', (memory_id,))
                db.execute('UPDATE channels SET generation=generation+1 WHERE guild=? AND channel=?', (scope.guild, scope.channel))
                count = 1
            elif action in {'delete', 'clear'}:
                sql = 'DELETE FROM memories WHERE guild=? AND channel=?'
                params = [scope.guild, scope.channel]
                if action == 'delete':
                    sql += ' AND id=?'
                    params.append(memory_id)
                if author is not None:
                    sql += ' AND author=?'
                    params.append(author)
                count = db.execute(sql, params).rowcount
                db.execute('UPDATE channels SET generation=generation+1 WHERE guild=? AND channel=?',
                           (scope.guild, scope.channel))
            else:
                raise MemoryError('Unknown memory operation')
            db.commit()
            return count

    def versions(self, scope, memory_id, *, offset=0):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT v.id,v.created,v.snapshot FROM memory_versions v JOIN memories m ON m.id=v.memory_id '
                'WHERE m.guild=? AND m.channel=? AND m.id=? ORDER BY v.id DESC LIMIT 5 OFFSET ?',
                (scope.guild, scope.channel, memory_id, max(0, offset)))]

    def invalidate_source(self, scope, message, *, invalidate_pending=True):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('DELETE FROM memories WHERE guild=? AND channel=? AND (message=? OR id IN '
                              '(SELECT memory_id FROM memory_sources WHERE guild=? AND channel=? AND message=?) '
                              'OR id IN (SELECT memory_id FROM memory_version_sources WHERE message=?))',
                               (scope.guild, scope.channel, str(message), scope.guild, scope.channel, str(message), str(message))).rowcount
            # Also invalidate in-flight writes sourced from an edited/deleted message.
            if count or invalidate_pending:
                db.execute('UPDATE channels SET generation=generation+1 WHERE guild=? AND channel=?',
                           (scope.guild, scope.channel))
            db.commit()
            return count


class LocalEmbedder:
    """One isolated CPU embedding process. No HTTP service and no cloud credentials."""
    def __init__(self):
        self.python = os.getenv('ATRI_MEMORY_EMBED_PYTHON', '')
        self.model = os.getenv('ATRI_MEMORY_EMBED_MODEL', 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
        self.cache = os.getenv('ATRI_MEMORY_EMBED_CACHE', '')
        self.process = None
        self.lock = asyncio.Lock()
        self.cooldown = 0.0

    async def _close_process(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def close(self):
        async with self.lock:
            await self._close_process()

    async def embed(self, text):
        if not self.python or time.monotonic() < self.cooldown:
            return None
        try:
            async with asyncio.timeout(12):
                async with self.lock:
                    try:
                        if self.process is None or self.process.returncode is not None:
                            # Deliberately do not inherit API keys, Discord tokens or cookies.
                            env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TMP', 'TEMP')
                                   if key in os.environ}
                            env.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_PROGRESS_BARS='1', TOKENIZERS_PARALLELISM='false')
                            self.process = await asyncio.create_subprocess_exec(
                                self.python, '-u', str(Path(__file__).with_name('memory_embed_worker.py')),
                                self.model, self.cache, env=env, stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=128*1024)
                        self.process.stdin.write((json.dumps({'text': text[:2000]}) + '\n').encode())
                        await self.process.stdin.drain()
                        line = await self.process.stdout.readline()
                        vector = json.loads(line)['vector']
                        pack(vector)
                        return vector
                    except BaseException:
                        await self._close_process()
                        raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.cooldown = time.monotonic() + 60
            print(f'[WARN] Memory embedding unavailable; lexical fallback: {type(exc).__name__}')
            return None


class MemoryService:
    def __init__(self, path, embedder=None):
        self.store = MemoryStore(path)
        self.embedder = embedder or LocalEmbedder()

    async def retrieve(self, scope, query, *, author=None, recent='', automatic=False):
        state = await asyncio.to_thread(self.store.status, scope)
        if not state['enabled'] or not state['count']:
            return [], state
        embedding_query = query[:1200] + ('\n近期话题：' + recent[:600] if recent else '')
        vector = await self.embedder.embed(embedding_query)
        rows = await asyncio.to_thread(self.store.search, scope, query, vector=vector, model=self.embedder.model,
                                       author=author, recent=recent, automatic=automatic,
                                       limit=self.store.policies.get(scope.guild)['recall_limit'] if automatic else 6)
        # Management can finish while embedding is running. Do not return stale memories.
        latest = await asyncio.to_thread(self.store.status, scope)
        return (rows if latest['enabled'] and latest['generation'] == state['generation'] else []), latest

    async def remember(self, scope, generation, *, author, message, source, args, sources=None):
        allowed = {'topic', 'kind', 'content', 'evidence'}
        if set(args) - (allowed | {'mode'}) or not all(isinstance(args.get(key), str) for key in allowed):
            raise MemoryError('只接受主题、类型、内容与当前发言原文，不接受任何目标频道/用户 ID。')
        evidence = args['evidence'].strip()
        if evidence not in source or not 3 <= len(evidence) <= 600:
            raise MemoryError('记忆依据必须逐字来自本轮真人消息，不能来自引用、附件或 BOT 回答。')
        if SECRET.search(args['topic'] + '\n' + args['content'] + '\n' + evidence):
            raise MemoryError('不保存疑似凭据。')
        mode = args.get('mode', 'reinforce' if args['kind'] == 'impression' else 'replace')
        if sources:
            for item in sources:
                if (set(item) != {'message', 'author', 'evidence', 'observed', 'support'}
                        or not str(item['message']).isascii() or not str(item['message']).isdigit()
                        or not 3 <= len(item['evidence']) <= 600 or SECRET.search(item['evidence'])
                        or not math.isfinite(item['observed'])
                        or (item['support'] and item['author'] != str(author))
                        or (not item['support'] and args['kind'] not in {'episode', 'in_joke'})):
                    raise MemoryError('不合法的记忆依据；不能用他人或 BOT 回答给当前发言人建立事实/印象。')
        vector = await self.embedder.embed(args['topic'] + ' ' + args['content'])
        identity = await asyncio.to_thread(self.store.remember, scope, generation, author=str(author),
                                          message=str(message), topic=args['topic'].strip(), kind=args['kind'],
                                          content=args['content'].strip(), evidence=evidence, vector=vector,
                                          model=self.embedder.model if vector else '', sources=sources, mode=mode)
        return {'summary': '已保存本频道记忆。印象仍是可修正的观察，不是定论；同一来源重试不增加支持度。',
                'content': json.dumps({'id': identity, 'channel_isolated': True}), 'truncated': False}

    async def close(self):
        await self.embedder.close()
