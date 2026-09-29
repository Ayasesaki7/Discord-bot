from __future__ import annotations

import io
import re
import unittest

from bot import TimestampedStream


STAMP = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4} '


class TimestampedStreamTests(unittest.TestCase):
    def test_each_line_is_stamped_once_across_partial_writes(self):
        target = io.StringIO()
        stream = TimestampedStream(target)
        # print() writes the text and the newline separately.
        stream.write('[INFO] first')
        stream.write('\n')
        stream.write('Traceback line 1\n  line 2\n')
        stream.write('')
        lines = target.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        for line, text in zip(lines, ['[INFO] first', 'Traceback line 1', '  line 2']):
            self.assertRegex(line, '^' + STAMP + re.escape(text) + '$')

    def test_stream_attributes_are_forwarded(self):
        target = io.StringIO()
        stream = TimestampedStream(target)
        self.assertEqual(stream.write('abc'), 3)
        stream.flush()
        self.assertEqual(stream.getvalue(), target.getvalue())
        self.assertIs(stream.closed, target.closed)


if __name__ == '__main__':
    unittest.main()
