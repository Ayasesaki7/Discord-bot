"""Channel-scoped, bounded lexical + approximate vector candidate retrieval.

No external service. The final ranking still uses exact cosine similarity.
Vector LSH is approximate; lexical and recent candidates complement it.
"""
from __future__ import annotations

from functools import lru_cache
import random
import struct


@lru_cache(maxsize=8)
def _planes(dimensions):
    rng = random.Random(0x41545249 + dimensions)
    return [[rng.choice((-1, 1)) for _ in range(dimensions)] for _ in range(48)]


def buckets(encoded):
    if not encoded:
        return []
    values = struct.unpack(f'<{len(encoded)//4}f', encoded)
    bits = [int(sum(a*b for a, b in zip(values, plane)) >= 0) for plane in _planes(len(values))]
    return [(band, sum(bits[band*6+i] << i for i in range(6))) for band in range(8)]


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS memory_terms (
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            guild TEXT NOT NULL, channel TEXT NOT NULL, term TEXT NOT NULL,
            PRIMARY KEY(memory_id,term));
        CREATE INDEX IF NOT EXISTS memory_term_lookup ON memory_terms(guild,channel,term,memory_id);
        CREATE TABLE IF NOT EXISTS memory_buckets (
            memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            guild TEXT NOT NULL, channel TEXT NOT NULL, model TEXT NOT NULL, dimensions INTEGER NOT NULL,
            band INTEGER NOT NULL, bucket INTEGER NOT NULL, PRIMARY KEY(memory_id,band));
        CREATE INDEX IF NOT EXISTS memory_bucket_lookup
            ON memory_buckets(guild,channel,model,dimensions,band,bucket,memory_id);
    ''')


def update(db, row, terms):
    identity, guild, channel = row['id'], row['guild'], row['channel']
    db.execute('DELETE FROM memory_terms WHERE memory_id=?', (identity,))
    db.executemany('INSERT INTO memory_terms VALUES(?,?,?,?)',
                   [(identity, guild, channel, word) for word in sorted(terms(row['topic']+' '+row['content']))])
    db.execute('DELETE FROM memory_buckets WHERE memory_id=?', (identity,))
    db.executemany('INSERT INTO memory_buckets VALUES(?,?,?,?,?,?,?)',
                   [(identity, guild, channel, row['model'], len(row['vector'])//4, band, bucket)
                    for band, bucket in buckets(row['vector'])])


def candidates(db, scope, words, encoded, model, author):
    identities = set()
    # Bounded UNION of indexed postings, never the latest 1000 memories only.
    words = sorted(words)[:80]
    if words:
        marks = ','.join('?' for _ in words)
        identities.update(r[0] for r in db.execute(
            f'SELECT memory_id FROM memory_terms WHERE guild=? AND channel=? AND term IN ({marks}) '
            'GROUP BY memory_id ORDER BY count(*) DESC,memory_id LIMIT 240',
            (scope.guild, scope.channel, *words)))
    vector_votes = {}
    for band, bucket in buckets(encoded):
        probes = [bucket] + [bucket ^ (1 << bit) for bit in range(6)]
        for row in db.execute('SELECT memory_id FROM memory_buckets WHERE guild=? AND channel=? '
                              'AND model=? AND dimensions=? AND band=? AND bucket IN (?,?,?,?,?,?,?) LIMIT 240',
                              (scope.guild, scope.channel, model, len(encoded)//4, band, *probes)):
            vector_votes[row[0]] = vector_votes.get(row[0], 0) + 1
    identities.update(k for k, _ in sorted(vector_votes.items(), key=lambda pair: (-pair[1], pair[0]))[:240])
    # Recent/familiarity candidates remain bounded and do not replace archive search.
    identities.update(r[0] for r in db.execute(
        'SELECT id FROM memories WHERE guild=? AND channel=? ORDER BY updated DESC,id LIMIT 120',
        (scope.guild, scope.channel)))
    if author is not None:
        identities.update(r[0] for r in db.execute(
            "SELECT id FROM memories WHERE guild=? AND channel=? AND author=? AND kind='impression' "
            'ORDER BY updated DESC,id LIMIT 60', (scope.guild, scope.channel, str(author))))
    return sorted(identities)
