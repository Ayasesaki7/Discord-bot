import asyncio
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from chat.agent.audit_log import OUTPUT_LIMIT
from chat.agent.discord_tools import DiscordToolError
from tests import test_discord_target_safety as fixtures


class AuditSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = fixtures.AuditLogAuthorizationTests().make_host(view=True)
        self.now = discord.utils.utcnow()
        self.entries = []

        async def entries(**kwargs):
            found = [e for e in self.entries if e.id < kwargs['before'].id and e.id > kwargs['after'].id]
            if 'user' in kwargs:
                found = [e for e in found if e.user_id == kwargs['user'].id]
            if 'action' in kwargs:
                found = [e for e in found if e.action == kwargs['action']]
            for item in sorted(found, key=lambda e: e.id, reverse=True)[:kwargs['limit']]:
                yield item
        self.host.message.guild.audit_logs = MagicMock(side_effect=entries)

    def entry(self, *, seconds=60, actor=20, target=300, reason='reason', action=discord.AuditLogAction.role_update,
              before=None, after=None):
        date = self.now - timedelta(seconds=seconds)
        b, a = discord.AuditLogDiff(), discord.AuditLogDiff()
        for k, v in (before or {}).items():
            setattr(b, k, v)
        for k, v in (after or {}).items():
            setattr(a, k, v)
        item = NS(id=discord.utils.time_snowflake(date), user_id=actor, user=NS(id=actor),
                  target=NS(id=target, name='deleted role'), action=action, reason=reason,
                  created_at=date, before=b, after=a, extra=None)
        self.entries.append(item)
        return item

    async def read(self, **args):
        result = await self.host.execute('audit_log', args)
        self.assertLessEqual(len(result['content']), OUTPUT_LIMIT)
        return json.loads(result['content'])

    async def all_pages(self, **args):
        page = await self.read(**args)
        items = []
        for _ in range(30):
            items.extend(page['entries'])
            if page['next_cursor'] is None:
                return items, page
            page = await self.read(cursor=page['next_cursor'])
        self.fail('pagination did not terminate')

    async def test_action_actor_target_and_keywords_with_real_changes(self):
        target, actor = 333333333333333333, 222222222222222222
        self.entry(actor=actor, target=target, before={'name': 'old name', 'permissions': discord.Permissions.none()},
                   after={'name': 'new name', 'permissions': discord.Permissions(view_audit_log=True)})
        self.entry(seconds=120, actor=22, target=target)
        self.entry(seconds=180, actor=actor, target=40)
        self.entry(seconds=240, actor=actor, target=target, action=discord.AuditLogAction.role_delete)
        page = await self.read(audit_action='role_update', actor_id=str(actor), target_id=str(target), query='NEW NAME')
        self.assertEqual(len(page['entries']), 1)
        item = page['entries'][0]
        self.assertEqual(item['actorId'], str(actor))
        self.assertEqual(item['targetId'], str(target))
        self.assertEqual(item['changes'][0]['before'], 'old name')
        self.assertEqual(item['changes'][0]['after'], 'new name')
        kwargs = self.host.message.guild.audit_logs.call_args.kwargs
        self.assertEqual(kwargs['user'].id, actor)
        self.assertEqual(kwargs['action'], discord.AuditLogAction.role_update)
        self.assertEqual(kwargs['limit'], 100)

    async def test_limit_paging_has_no_missing_or_duplicated_entries(self):
        for n in range(13):
            self.entry(seconds=n+1)
        items, final = await self.all_pages(limit=3)
        self.assertEqual([e['id'] for e in items], [str(e.id) for e in self.entries])
        self.assertTrue(final['scanComplete'])

    async def test_empty_filtered_page_does_not_mean_no_match(self):
        for n in range(110):
            self.entry(seconds=n+1, reason='needle' if n == 105 else 'no')
        first = await self.read(query='needle')
        self.assertFalse(first['entries'])
        self.assertEqual(first['scannedEntries'], 100)
        self.assertFalse(first['scanComplete'])
        self.host.message.guild.audit_logs.assert_called_once()
        more = await self.read(cursor=first['next_cursor'])
        self.assertEqual(len(more['entries']), 1)
        self.assertTrue(more['scanComplete'])

    async def test_dates_use_beijing_when_naive_and_until_is_exclusive(self):
        for delta in (100, 200, 300):
            self.entry(seconds=delta)
        start = self.entries[1].created_at.replace(microsecond=0)
        end = self.entries[0].created_at.replace(microsecond=0)
        from datetime import timezone
        start_beijing = start.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat()
        page = await self.read(since=start_beijing, until=end.isoformat())
        self.assertEqual([e['id'] for e in page['entries']], [str(self.entries[1].id)])

    async def test_retention_clips_and_outside_window_makes_no_audit_request(self):
        page = await self.read(since=(self.now-timedelta(days=100)).isoformat(), until=(self.now-timedelta(days=46)).isoformat())
        self.assertTrue(page['retentionClipped'])
        self.assertTrue(page['scanComplete'])
        self.assertEqual(page['retentionDays'], 45)
        self.assertFalse(page['entries'])
        self.host.message.guild.audit_logs.assert_not_called()
        self.assertEqual(self.host.message.guild.fetch_member.await_count, 2)

    async def test_departed_actor_id_and_deleted_target_need_no_member_fetch_for_target(self):
        item = self.entry(actor=42)
        item.user = None
        self.host._member_from_arguments = AsyncMock(side_effect=AssertionError('must not resolve departed ID'))
        page = await self.read(user_ref='42', target_id='300')
        self.assertEqual(page['entries'][0]['actorId'], '42')
        self.assertIsNone(page['entries'][0]['actor'])
        self.host._member_from_arguments.assert_not_awaited()

    async def test_actor_name_uses_existing_exact_unique_resolver(self):
        self.entry(actor=42)
        self.host._member_from_arguments = AsyncMock(return_value=NS(id=42))
        page = await self.read(user_ref='unique')
        self.assertEqual(len(page['entries']), 1)
        self.host._member_from_arguments.assert_awaited_once_with({'user_ref': 'unique'})

    async def test_cursor_rejects_other_turn_guild_requester_and_changed_filters(self):
        for n in range(3):
            self.entry(seconds=n+1)
        page = await self.read(limit=1)
        token = page['next_cursor']
        for who in ({'view': True}, {'admin': True}, {'guild_owner': True}):
            other = fixtures.AuditLogAuthorizationTests().make_host(**who)
            with self.assertRaisesRegex(DiscordToolError, '游标'):
                await other.execute('audit_log', {'cursor': token})
            other.message.guild.audit_logs.assert_not_called()
        with self.assertRaisesRegex(DiscordToolError, '游标'):
            await self.read(cursor=token, target_id='300')
        await self.read(cursor=token)
        with self.assertRaisesRegex(DiscordToolError, '游标'):
            await self.read(cursor=token)

    async def test_valid_cursor_cannot_bypass_permission_revocation(self):
        self.entry(seconds=1)
        self.entry(seconds=2)
        page = await self.read(limit=1)
        self.host.message.guild.fetch_member.return_value.guild_permissions = discord.Permissions.none()
        with self.assertRaisesRegex(DiscordToolError, '拒绝读取审核日志'):
            await self.read(cursor=page['next_cursor'])
        self.host.message.guild.audit_logs.assert_called_once()

    async def test_revocation_during_request_discards_already_fetched_data(self):
        self.entry()
        allowed = self.host.message.guild.fetch_member.return_value
        denied = NS(id=allowed.id, guild=allowed.guild, guild_permissions=discord.Permissions.none())
        self.host.message.guild.fetch_member.side_effect = [allowed, denied]
        with self.assertRaisesRegex(DiscordToolError, '拒绝读取审核日志'):
            await self.read()
        self.host.message.guild.audit_logs.assert_called_once()
        self.assertFalse(self.host._audit_log_search.cursors)

    async def test_failure_after_partial_result_does_not_return_success_or_retry(self):
        item = self.entry()
        async def failing(**kwargs):
            yield item
            raise TimeoutError()
        self.host.message.guild.audit_logs.side_effect = failing
        with self.assertRaisesRegex(DiscordToolError, '失败或超时'):
            await self.read()
        self.host.message.guild.audit_logs.assert_called_once()

    async def test_large_results_stay_valid_json_and_resume_before_unreturned_hit(self):
        for n in range(15):
            self.entry(seconds=n+1, before={f'field_{k}': 'x'*500 for k in range(30)},
                       after={f'field_{k}': 'y'*500 for k in range(30)})
        first = await self.read(limit=50)
        self.assertLess(len(first['entries']), 15)
        self.assertGreater(first['entries'][0]['changesOmitted'], 0)
        items, _ = await self.all_pages(cursor=first['next_cursor'])
        ids = [e['id'] for e in first['entries']+items]
        self.assertEqual(len(ids), 15)
        self.assertEqual(len(set(ids)), 15)

    async def test_sensitive_fields_and_reason_are_redacted(self):
        self.entry(reason='Authorization: Bearer no-exposure-ever-123456',
                   before={'token': 'old-secret'}, after={'token': 'new-secret'})
        page = await self.read()
        data = json.dumps(page)
        for text in ('no-exposure-ever', 'old-secret', 'new-secret'):
            self.assertNotIn(text, data)
        self.assertIn('redacted', data)

    async def test_unknown_filter_and_unsafe_snowflakes_rejected_before_api(self):
        for args in ({'actor_id': 222222222222222222}, {'target_id': 300}, {'target_id': '1e18'}, {'user_ref': 42},
                     {'target_id': '18446744073709551616'}, {'user_ref': 'name', 'actor_id': '20'},
                     {'audit_action': 'made_up'}, {'limit': True}, {'query': 'x'*201}, {'channel_id': '300'},
                     {'since': 'yesterday'}, {'since': '2026-10-01', 'until': '2026-09-01'}, {'cursor': ''}):
            with self.subTest(args=args), self.assertRaises(DiscordToolError):
                await self.read(**args)
        self.host.message.guild.audit_logs.assert_not_called()

    async def test_cursor_expires(self):
        self.entry(seconds=1)
        self.entry(seconds=2)
        page = await self.read(limit=1)
        token = page['next_cursor']
        state, stamp = self.host._audit_log_search.cursors[token]
        self.host._audit_log_search.cursors[token] = (state, stamp-601)
        with self.assertRaisesRegex(DiscordToolError, '游标'):
            await self.read(cursor=token)

    async def test_real_discord_role_change_decoding_without_network(self):
        guild = self.host.message.guild
        guild._state = NS()
        guild.get_role = lambda _: None
        item = discord.AuditLogEntry(guild=guild, users={}, integrations={}, app_commands={}, automod_rules={}, webhooks={},
                                    data={'id': str(discord.utils.time_snowflake(self.now-timedelta(seconds=1))),
                                          'action_type': 25, 'user_id': '20', 'target_id': '300',
                                          'changes': [{'key': '$add', 'new_value': [{'id': '333333333333333333', 'name': 'NewRole'}]}]})
        self.entries.append(item)
        page = await self.read(audit_action='member_role_update')
        role = page['entries'][0]['changes'][0]['after'][0]
        self.assertEqual(role['id'], '333333333333333333')
        self.assertEqual(role['name'], 'NewRole')


if __name__ == '__main__':
    unittest.main()
