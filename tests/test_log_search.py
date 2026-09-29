import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat.agent.runtime_tools import RuntimeDiagnosticHost, RuntimeToolError
from chat.agent import log_reader


class LogSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.host = RuntimeDiagnosticHost(self.root)
        self.addCleanup(lambda: [job.close() for job in self.host._log_reader.cursors.values()])

    def log(self, name, text):
        if name.endswith('.gz'):
            with gzip.open(self.root / name, 'wt', encoding='utf-8') as handle:
                handle.write(text)
        else:
            (self.root / name).write_text(text, encoding='utf-8')

    def read(self, **args):
        result = self.host.read_log(args)
        self.assertLessEqual(len(result['content'].encode()), log_reader.MAX_OUTPUT_BYTES)
        return json.loads(result['content'])

    def all_pages(self, **args):
        page, hits = self.read(**args), []
        for _ in range(100):
            hits.extend(page['results'])
            if not page['next_cursor']:
                return hits, page
            page = self.read(cursor=page['next_cursor'])
        self.fail('search did not terminate')

    def test_history_includes_numbered_gzip_and_timestamp_archives(self):
        for name in ('bot.log.2.gz', 'bot.log.1', 'bot.log.20260920_102030', 'bot.log', 'bot.err.log.1'):
            self.log(name, f'2026-09-20 12:00:00+0200 [ERROR] PDF failed in {name}\n')
        self.log('bot.log.env', 'PDF secret\n')
        self.log('unrelated.log', 'PDF secret\n')
        hits, end = self.all_pages(query='PDF', limit=1, context_lines=0)
        self.assertEqual(len(hits), 5)
        self.assertEqual(len({hit['file'] for hit in hits}), 5)
        self.assertTrue(end['coverageComplete'])
        hits, _ = self.all_pages(query='PDF', history=False, context_lines=0)
        self.assertEqual([h['file'] for h in hits], ['bot.log'])
        files = self.read(mode='list')['files']
        self.assertEqual(len(files), 5)
        self.assertTrue(next(f for f in files if f['file'].endswith('.gz'))['compressed'])

    def test_search_reaches_before_old_512k_tail(self):
        self.log('bot.log', '2026-09-20 10:00:00+0800 [ERROR] ancient needle\n' + ('filler\n' * 100000))
        hits, _ = self.all_pages(query='ancient needle', context_lines=0)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['matchLine'], 1)

    def test_literal_and_filters_exclusion_case_and_level(self):
        self.log('bot.log', '2026-09-27 10:00:00+0800 [WARN] PDF error [a.*]\n'
                 '2026-09-27 10:01:00+0800 [ERROR] PDF error [a.*] noisy\n'
                 '2026-09-27 10:02:00+0800 [ERROR] pdf error [a.*]\n'
                 '2026-09-27 10:03:00+0800 [ERROR] music error\n')
        hits, _ = self.all_pages(terms=['pdf', '[a.*]'], exclude='noisy', level='ERROR', context_lines=0)
        self.assertEqual([h['matchLine'] for h in hits], [3])
        hits, _ = self.all_pages(query='PDF', case_sensitive=True, context_lines=0)
        self.assertEqual([h['matchLine'] for h in hits], [1, 2])

    def test_timezones_and_exclusive_end_do_not_invent_legacy_dates(self):
        self.log('bot.log', 'old undated ERROR\n2026-09-27 01:59:59Z [ERROR] before\n'
                 '2026-09-27 02:00:00Z [ERROR] start\n2026-09-27 05:00:00+0200 [ERROR] end\n')
        hits, end = self.all_pages(since='2026-09-27T10:00:00', until='2026-09-27T11:00:00+08:00', context_lines=0)
        self.assertEqual([h['matchLine'] for h in hits], [3])
        self.assertEqual(end['undatedLines'], 1)

    def test_context_traceback_and_file_boundary(self):
        self.log('bot.err.log.1', 'old before\nold needle\nold after\n')
        self.log('bot.err.log', '2026-09-27 10:00:00+0800 [ERROR] needle\n  stack A\n  stack B\nnew plain\n')
        hits, _ = self.all_pages(query='needle', context_lines=2, limit=1)
        self.assertEqual(len(hits), 2)
        for hit in hits:
            self.assertTrue(all(line['file'] == hit['file'] for line in hit['lines']))
        newest = hits[-1]
        self.assertEqual([x['line'] for x in newest['lines']], [1, 2, 3])
        self.assertEqual(newest['lines'][0]['timestamp'], newest['lines'][2]['timestamp'])

    def test_no_match_page_is_continuable_and_gzip_work_is_bounded(self):
        self.log('bot.log.1.gz', 'filler\n' * 1000 + 'late needle\n')
        with patch.object(log_reader, 'MAX_SCAN_BYTES', 256):
            page = self.read(query='needle', context_lines=0)
            self.assertFalse(page['scanComplete'])
            self.assertFalse(page['results'])
            self.assertTrue(page['next_cursor'])
            self.assertLess(page['scannedBytesThisPage'], 512)
            hits, end = self.all_pages(cursor=page['next_cursor'])
        self.assertEqual(len(hits), 1)
        self.assertTrue(end['coverageComplete'])

    def test_append_is_snapshot_bounded_and_rotation_invalidates_cursor(self):
        self.log('bot.log', 'needle 1\nneedle 2\n')
        page = self.read(query='needle', limit=1, context_lines=0)
        with (self.root / 'bot.log').open('a', encoding='utf8') as h:
            h.write('needle 3\n')
        hits, _ = self.all_pages(cursor=page['next_cursor'])
        self.assertEqual([x['matchLine'] for x in hits], [2])
        page = self.read(query='needle', limit=1, context_lines=0)
        self.log('bot.log', 'rotated\n')
        with self.assertRaisesRegex(RuntimeToolError, 'rotated/changed'):
            self.read(cursor=page['next_cursor'])

    def test_cursor_single_use_expiry_and_fixed_parameters(self):
        self.log('bot.log', 'needle\n' * 5)
        page = self.read(query='needle', limit=1, context_lines=0)
        token = page['next_cursor']
        with self.assertRaises(RuntimeToolError):
            self.read(cursor=token, query='other')
        second = self.read(cursor=token)
        with self.assertRaisesRegex(RuntimeToolError, 'expired, consumed or unknown'):
            self.read(cursor=token)
        self.host._log_reader.cursors[second['next_cursor']].touched -= 601
        with self.assertRaises(RuntimeToolError):
            self.read(cursor=second['next_cursor'])

    def test_secrets_redacted_before_matching_and_output(self):
        secret = 'sk-fish-never-expose-this-secret'
        self.log('bot.log.1.gz', f'API_KEY={secret}\nCookie: a=private; b=private-too\n'
                 'FISH_API_KEY=another-secret-value\n{"api_key":"json-secret-value"}\n'
                 '-----BEGIN PRIVATE KEY-----\nprivatebody\n-----END PRIVATE KEY-----\n')
        hits, _ = self.all_pages(mode='search', context_lines=0)
        output = json.dumps(hits)
        for word in (secret, 'private-too', 'another-secret-value', 'json-secret-value', 'privatebody'):
            self.assertNotIn(word, output)
        self.assertIn('redacted', output)
        hits, _ = self.all_pages(query=secret, context_lines=0)
        self.assertFalse(hits)

    def test_output_and_oversized_line_bounds(self):
        self.log('bot.log', '太长' * 40000 + '\n' + ('关键词' + '话' * 2000 + '\n') * 150)
        page = self.read(query='关键词', limit=100, context_lines=3)
        self.assertGreater(page['oversizedLinesSkipped'], 0)
        self.assertTrue(page['next_cursor'])
        self.assertLess(len(page['results']), 100)
        self.assertTrue(page['results'][0]['lines'][0]['textTruncated'])

    def test_corrupt_gzip_is_not_reported_as_success(self):
        (self.root / 'bot.log.1.gz').write_bytes(b'not gzip')
        with self.assertRaisesRegex(RuntimeToolError, 'corrupt'):
            self.read(mode='search')

    def test_bad_parameters_never_silently_disable_filters(self):
        for args in ({'file_path': '.env'}, {'regex': '(a+)+$'}, {'mode': 'arbitrary'}, {'limit': True},
                     {'terms': 'oops'}, {'context_lines': 100}, {'query': 'x'*201}, {'since': 'yesterday'},
                     {'since': '2026-10-01', 'until': '2026-09-01'}, {'cursor': ''}, {'level': 'not-a-level'},
                     {'history': 'yes'}, {'mode': 'tail', 'query': 'oops'}, {'mode': 'list', 'query': 'ignored'},
                     {'tail_lines': 20, 'since': '2026-09-01'}):
            with self.subTest(args=args), self.assertRaises(RuntimeToolError):
                self.host.read_log(args)

    def test_log_alias_cannot_read_arbitrary_files(self):
        private = self.root / 'private.txt'
        private.write_text('NEVER_SHOW', encoding='utf8')
        try:
            (self.root / 'bot.log').symlink_to(private)
        except OSError:
            os.link(private, self.root / 'bot.log')
        result = self.read(mode='search')
        self.assertFalse(result['coverageComplete'])
        self.assertFalse(result['results'])
        self.assertTrue(result['warnings'])
        self.assertNotIn('NEVER_SHOW', self.host.read_log({})['content'])


if __name__ == '__main__':
    unittest.main()
