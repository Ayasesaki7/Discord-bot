"""Bounded LRC parsing and playback-position lookup, without network or Discord."""
from __future__ import annotations

import base64
import bisect
import html
import re


_STAMP = re.compile(r'\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]')


def parse_lrc(value: object) -> list[tuple[float, str]]:
    if not isinstance(value, str) or not value or len(value) > 512_000:
        return []
    text = html.unescape(value)
    if not _STAMP.search(text):
        try:
            text = html.unescape(base64.b64decode(text, validate=True).decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            return []
    offset_match = re.search(r'\[offset:([+-]?\d+)\]', text, re.I)
    offset = max(-300, min(300, int(offset_match[1]) / 1000)) if offset_match else 0
    result = {}
    for line in text.splitlines()[:8000]:
        stamps = list(_STAMP.finditer(line))
        if not stamps:
            continue
        lyric = ' '.join(_STAMP.sub('', line).split())[:220]
        if not lyric:
            continue
        for stamp in stamps[:20]:
            fraction = int(stamp[3]) / 10 ** len(stamp[3]) if stamp[3] else 0
            timestamp = max(0, int(stamp[1]) * 60 + int(stamp[2]) + fraction - offset)
            result.setdefault(timestamp, lyric)
        if len(result) >= 2000:
            break
    return sorted(result.items())[:2000]


def lyric_window(lines: list[tuple[float, str]], seconds: float) -> tuple[str, str, str]:
    if not lines:
        return '', '', ''
    index = bisect.bisect_right([item[0] for item in lines], max(0, seconds)) - 1
    if index < 0:
        return '', '♪ 前奏', lines[0][1]
    return (lines[index - 1][1] if index else '', lines[index][1],
            lines[index + 1][1] if index + 1 < len(lines) else '')


def next_refresh_delay(lines: list[tuple[float, str]], seconds: float) -> float:
    """Refresh on the next lyric boundary; coalesce dense lyrics to <= 1 Hz."""
    index = bisect.bisect_right([item[0] for item in lines], max(0, seconds))
    if index >= len(lines):
        return 10.0  # Progress-only heartbeat during long instrumental sections/end.
    return max(1.0, min(10.0, lines[index][0] - max(0, seconds) + 0.04))
