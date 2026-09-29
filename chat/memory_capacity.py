"""Lossless active/archive lifecycle. All writes run in the caller's transaction."""
from __future__ import annotations

import json
import time


PROTECTED = {'preference', 'relationship', 'agreement', 'project', 'todo', 'fact'}


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS memory_state (
            memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
            archived INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0,
            hits INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS memory_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            created REAL NOT NULL, snapshot TEXT NOT NULL, vector BLOB, bytes INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS memory_version_lookup ON memory_versions(memory_id,id);
        CREATE TABLE IF NOT EXISTS memory_version_sources (
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            message TEXT NOT NULL, PRIMARY KEY(memory_id,message));
        CREATE INDEX IF NOT EXISTS memory_author_lookup ON memories(guild,channel,author,kind,updated);
    ''')


def refresh_bytes(db, identity):
    row = dict(db.execute('SELECT * FROM memories WHERE id=?', (identity,)).fetchone())
    vector = row.pop('vector')
    sources = [dict(r) for r in db.execute('SELECT * FROM memory_sources WHERE memory_id=?', (identity,))]
    size = len(json.dumps([row, sources], ensure_ascii=False).encode()) + len(vector or b'')
    # Account for index/SQLite overhead conservatively; panel calls this a logical budget.
    size += 1024 + 96 * db.execute('SELECT count(*) FROM memory_terms WHERE memory_id=?', (identity,)).fetchone()[0]
    size += db.execute('SELECT coalesce(sum(bytes),0) FROM memory_versions WHERE memory_id=?', (identity,)).fetchone()[0]
    db.execute('INSERT INTO memory_state(memory_id,bytes) VALUES(?,?) ON CONFLICT(memory_id) DO UPDATE SET bytes=excluded.bytes',
               (identity, size))


def save_version(db, identity):
    row = dict(db.execute('SELECT * FROM memories WHERE id=?', (identity,)).fetchone())
    vector = row.pop('vector')
    sources = [dict(r) for r in db.execute('SELECT * FROM memory_sources WHERE memory_id=?', (identity,))]
    snapshot = json.dumps(dict(memory=row, sources=sources), ensure_ascii=False)
    size = len(snapshot.encode()) + len(vector or b'') + 128
    db.execute('INSERT INTO memory_versions(memory_id,created,snapshot,vector,bytes) VALUES(?,?,?,?,?)',
               (identity, time.time(), snapshot, vector, size))
    db.executemany('INSERT OR IGNORE INTO memory_version_sources VALUES(?,?)',
                   [(identity, source['message']) for source in sources])


def usage(db, scope):
    row = db.execute('SELECT count(*) AS count,coalesce(sum(s.archived),0) AS archived,'
                     'coalesce(sum(s.pinned),0) AS pinned,coalesce(sum(s.bytes),0) AS bytes '
                     'FROM memories m JOIN memory_state s ON s.memory_id=m.id WHERE m.guild=? AND m.channel=?',
                     (scope.guild, scope.channel)).fetchone()
    value = dict(row)
    value['active'] = value['count'] - value['archived']
    return value


def rebalance(db, scope, policy):
    """Archive cold social records near the soft target; never evict protected facts."""
    if not policy['organize_enabled']:
        return 0
    now = time.time()
    rows = db.execute('SELECT m.id,m.author,m.kind,m.updated,s.* FROM memories m '
                      'JOIN memory_state s ON s.memory_id=m.id WHERE m.guild=? AND m.channel=? AND s.archived=0',
                      (scope.guild, scope.channel)).fetchall()
    counts = {}
    for row in rows:
        counts[row['author']] = counts.get(row['author'], 0) + 1
    total = len(rows)
    user_limit, channel_limit = policy['memory_user_active'], policy['memory_channel_active']
    user_target, channel_target = max(1, int(user_limit*.8)), max(1, int(channel_limit*.8))
    pressured = {author for author, count in counts.items() if count >= max(1, int(user_limit*.9))}
    channel_pressure = total >= max(1, int(channel_limit*.9))
    eligible = [r for r in rows if not r['pinned'] and (r['kind'] not in PROTECTED or r['completed'])]
    eligible.sort(key=lambda r: (max(r['updated'], r['last_used']) + min(r['hits'], 30)*86400, r['id']))
    archived = 0
    for row in eligible:
        cold = now-max(row['updated'], row['last_used']) >= policy['memory_archive_days']*86400
        pressure = ((row['author'] in pressured and counts[row['author']] > user_target)
                    or (channel_pressure and total > channel_target))
        if cold or pressure:
            db.execute('UPDATE memory_state SET archived=1 WHERE memory_id=?', (row['id'],))
            total -= 1
            counts[row['author']] -= 1
            archived += 1
    return archived
