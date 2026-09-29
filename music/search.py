"""Shared QQ full-song search for the picker, batch requests and Agent.

This is search-input cleanup, not a chat intent router. Only called after the
user/Agent has requested music. Preserve the original query before fallbacks.
"""
from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable


SEARCH_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
MAX_SEARCH_REQUESTS = 3
SEARCH_TIMEOUT = 10
_REQUEST_PREFIX = re.compile(
    r"^(?:(?:请|麻烦你?|帮我|给我)\s*)*"
    r"(?:播放(?:一下|一首)?|搜索(?:一下)?|搜一下|点一首|来一首|"
    r"放(?:一下|一首)|(?:我)?想听)\s*|"
    r"^(?:please\s+)?(?:play|search\s+for|find)\s+", re.I
)
_THANKS = re.compile(r"[\s,，。!！]*(?:谢谢(?:你)?|好吗|可以吗|吧)[\s,，。!！]*$")


def query_variants(query: str) -> list[str]:
    # Bound user input, but never silently truncate a song into a different one.
    if not isinstance(query, str) or len(query) > 1000:
        return []
    original = unicodedata.normalize("NFKC", html.unescape(query))
    original = "".join(" " if unicodedata.category(c).startswith("C") else c for c in original)
    original = " ".join(original.split())
    if not original:
        return []
    cleaned = _clean_query(original)
    # Keep apostrophes, +, /, &, and version brackets in the first two queries.
    # A punctuation-separated fallback is only used if those fail to match well.
    softened = " ".join("".join(c if c.isalnum() else " " for c in cleaned).split())
    return list(dict.fromkeys(s for s in (original, cleaned, softened) if s))[:MAX_SEARCH_REQUESTS]


def _clean_query(original: str) -> str:
    cleaned, count = _REQUEST_PREFIX.subn("", original, count=1)
    if count:
        cleaned = _THANKS.sub("", cleaned).strip()
    # Only remove possessives next to an explicit quoted title, not inside songs.
    cleaned = re.sub(r"(?:演唱的|唱的|的)\s*(?=[《「『])", " ", cleaned)
    cleaned = re.sub(r"[《》「」『』]", " ", cleaned)
    cleaned = re.sub(r"\s+[-–—|]+\s+", " ", cleaned)
    return " ".join(cleaned.split())


def _key(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", html.unescape(value)).casefold() if c.isalnum())


def _contains(query: str, phrase: str) -> bool:
    key = _key(phrase)
    if not key:
        return False
    if re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", phrase):
        return key in _key(query)
    # Don't rank "It" above a requested "Without You" just for a substring.
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", html.unescape(phrase)).casefold())
    return bool(words and re.search(r"(?<!\w)" + r"[\W_]*".join(map(re.escape, words)) + r"(?!\w)", query.casefold()))


def relevance(song: dict, query: str) -> int:
    name = str(song.get("name") or song.get("title") or "")
    title = str(song.get("title") or name)
    artists = [str(s.get("name") or "") for s in song.get("singer", []) if isinstance(s, dict)]
    query_key = _key(query)
    if html.unescape(title).casefold() == query.casefold():
        return 140
    if _key(title) and _key(title) == query_key:
        return 120
    title_match = _contains(query, title)
    name_match = _contains(query, name)
    if not (title_match or name_match):
        return 0
    artist_match = any(_contains(query, artist) for artist in artists)
    # Full title preserves Live/remix/Taylor's Version, while name may omit it.
    score = (60 if title_match else 40) + (40 if artist_match else 0)
    if title_match and _key(title) != _key(name):
        score += 20
    if _key(name) == query_key:
        score += 30
    return score


def _read_songs(result: dict) -> list[dict]:
    if not isinstance(result, dict) or result.get("code") != 0:
        raise RuntimeError("QQ full search failed at response level")
    block = result.get("req_1")
    if not isinstance(block, dict) or block.get("code") != 0:
        raise RuntimeError("QQ full search failed at method level")
    data = block.get("data")
    if not isinstance(data, dict) or data.get("code", 0) != 0:
        raise RuntimeError("QQ full search failed at data level")
    body = data.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("song"), dict):
        raise RuntimeError("QQ full search returned an invalid song body")
    items = body["song"].get("list")
    if not isinstance(items, list):
        raise RuntimeError("QQ full search returned an invalid song list")
    # Limit processing too; responses can contain thousands of grouped editions.
    return [s for s in items[:30] if isinstance(s, dict) and isinstance(s.get("mid"), str)
            and s["mid"] and isinstance(s.get("singer", []), list)
            and (s.get("name") or s.get("title"))]


def search_songs(request_json: Callable, query: str, *, limit: int = 8) -> list[dict]:
    variants = query_variants(query)
    if not variants or limit <= 0:
        return []
    limit = min(limit, 25)
    results: dict[str, dict] = {}
    # The cleaned text is the best ranking target; the original is still sent first.
    ranking_query = _clean_query(variants[0]) or variants[0]
    for keyword in variants:
        payload = {
            "comm": {"ct": 24, "cv": 0, "format": "json"},
            "req_1": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {"query": keyword, "num_per_page": max(20, limit), "page_num": 1, "search_type": 0},
            },
        }
        try:
            items = _read_songs(request_json(SEARCH_URL, method="POST", data=payload, timeout=SEARCH_TIMEOUT))
        except Exception:
            # Timeouts, risk-control and schema errors aren't "no matches". No
            # retry storm against another endpoint; keep any earlier candidates.
            if results:
                break
            raise
        for item in items:
            results.setdefault(item["mid"], item)
        if any(relevance(item, ranking_query) >= 80 for item in items):
            break
    # Stable sort keeps QQ's order when equally relevant, and dedupes by MID,
    # never by name (different editions can intentionally share a song title).
    return sorted(results.values(), key=lambda item: relevance(item, ranking_query), reverse=True)[:limit]
