# Discord management target safety

Queries return the exact string IDs plus opaque `userRef`, `roleRef`, and
`channelRef` values. Prefer these values in `user_ref`, `role_ref`, and
`channel_id`. A reference is typed and valid only within the requesting turn;
another turn or guild must query again. These references identify objects,
not permission grants. Existing guild and role-hierarchy checks still apply.

Names must match exactly and uniquely. Partial matches are discovery candidates,
not mutation targets. With an incomplete Gateway member cache, member/member-list
queries use the Discord REST member search endpoint (bounded to 100 candidates and
12 seconds); no privileged Gateway intent or channel membership is required.
Full usernames, global display names and nicknames can resolve directly when the
live results contain one exact match and did not hit the API cap. A name prefix
is for discovery; zero partial-name results do not prove someone left the guild.
Empty queries list cached members only and explicitly disclose incomplete coverage.
Repeated identical lookups, including failures, are deduplicated within the turn.
Fresh member objects stay in that turn's host cache so returned references work
without relying on the Gateway cache. Duplicate names require clarification. Supplying a name
and ID that resolve differently fails instead of silently choosing one.

Raw snowflakes must be exact quoted ASCII decimal strings. Unsafe JSON integers,
floats, booleans, scientific notation, and malformed IDs are rejected. Never try
to reconstruct an already-rounded ID. Names and references are resolved to fixed
IDs before destructive confirmation, so renaming while waiting cannot redirect
the approved operation. Range deletion verifies that boundary messages actually
exist in the target channel before confirmation or deletion.

Audit logs use a separate permission check. On each tool call the host fetches
the requester in the current guild and requires guild ownership, Administrator,
or View Audit Log. Bot-developer status alone does not bypass this check. The bot
also needs its own View Audit Log permission. Failed permission lookup refuses
access; the model is instructed not to replay another user's remembered audit
details when access is denied. This check protects new tool reads; previously
posted messages are still governed by Discord channel visibility.

`discord_query(action="audit_log")` also checks permission again before returning
fetched data. It supports `audit_action`, actor `user_ref` OR exact quoted `actor_id`,
exact quoted `target_id`, literal `query`, `since`/`until`, and `limit` (1–50 matches).
Actor and target are distinct: the actor performed the action; the target was
changed. IDs are never accepted as JSON numbers, and departed/deleted objects can
still be searched by their exact historical IDs. Name lookup uses the existing
unique-member resolver; ambiguous names are not guessed.

Time filters are ISO dates/timestamps, inclusive start/exclusive end. Missing
timezone means Asia/Shanghai. Discord only retains audit logs for 45 days;
older requested ranges are explicitly clipped. This feature does not create an
audit database or promise recovery of expired entries or deleted message text.

Each invocation fetches at most one 100-entry Discord page, with a 15-second
deadline and no application-level automatic retries. It returns bounded,
valid JSON with before/after changes, affected IDs/names, reason, extra metadata,
`scanComplete`, `scannedEntries` and `next_cursor`. Names/reasons are untrusted
reference material; sensitive-looking fields are redacted. Large changes have
bounded detail and omitted-field counts rather than broken/truncated JSON.

Continue in the same turn with `action="audit_log"` plus `cursor` only. Cursors
are random, turn/guild/requester-bound, single-use and expire after 10 minutes;
they retain only filters and the last scanned ID, not audit entries. Empty results
with a cursor mean the scan is unfinished, not that no records exist. A later
conversation turn must start a fresh search using dates/targets, and permissions
are checked on every page even when a valid cursor is supplied.

Regression tests: `python -m unittest tests.test_discord_target_safety
tests.test_discord_tools tests.test_discord_agent_semantics` (one command).
