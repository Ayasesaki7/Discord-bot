from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import aiohttp


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass(slots=True)
class WebSearchConfig:
    enabled: bool = True
    max_results: int = 5
    timeout_seconds: int = 15
    provider: str = "auto"
    user_agent: str = "ATRI-DiscordBot/1.0"
    brave_api_key: str = ""
    serper_api_key: str = ""

    @classmethod
    def from_env(cls) -> "WebSearchConfig":
        return cls(
            enabled=_read_bool("ATRI_DRAW_ENABLE_WEB_SEARCH", True),
            max_results=max(1, min(_read_int("ATRI_DRAW_WEB_SEARCH_MAX_RESULTS", 5), 10)),
            timeout_seconds=max(5, _read_int("ATRI_DRAW_WEB_SEARCH_TIMEOUT", 15)),
            provider=os.getenv("ATRI_DRAW_WEB_SEARCH_PROVIDER", "auto").strip().lower() or "auto",
            user_agent=os.getenv("OPENAI_USER_AGENT", "").strip() or "ATRI-DiscordBot/1.0",
            brave_api_key=os.getenv("BRAVE_SEARCH_API_KEY", "").strip(),
            serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
        )


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class CharacterSearchClient:
    def __init__(self, config: WebSearchConfig | None = None) -> None:
        self.config = config or WebSearchConfig.from_env()

    def is_configured(self) -> bool:
        return self.config.enabled

    async def search_character(
        self,
        character_name: str,
        work_name: str = "",
    ) -> list[SearchResult]:
        if not self.config.enabled:
            return []

        query_parts = [character_name.strip()]
        if work_name.strip():
            query_parts.append(work_name.strip())
        query_parts.append("appearance outfit character")
        query = " ".join(part for part in query_parts if part)

        providers = self._candidate_providers()
        last_results: list[SearchResult] = []
        for provider in providers:
            try:
                if provider == "brave":
                    last_results = await self._search_brave(query)
                elif provider == "serper":
                    last_results = await self._search_serper(query)
                else:
                    last_results = await self._search_duckduckgo(query)
            except Exception:
                last_results = []
            if last_results:
                return last_results[: self.config.max_results]
        return last_results[: self.config.max_results]

    def _candidate_providers(self) -> list[str]:
        provider = self.config.provider
        if provider in {"brave", "serper", "duckduckgo"}:
            return [provider]
        providers: list[str] = []
        if self.config.brave_api_key:
            providers.append("brave")
        if self.config.serper_api_key:
            providers.append("serper")
        providers.append("duckduckgo")
        return providers

    async def _search_brave(self, query: str) -> list[SearchResult]:
        if not self.config.brave_api_key:
            return []
        headers = {
            "Accept": "application/json",
            "User-Agent": self.config.user_agent,
            "X-Subscription-Token": self.config.brave_api_key,
        }
        params = {"q": query, "count": str(self.config.max_results)}
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get("https://api.search.brave.com/res/v1/web/search", params=params) as response:
                if response.status >= 400:
                    return []
                data = await response.json(content_type=None)
        items = (((data or {}).get("web") or {}).get("results") or [])
        results: list[SearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    snippet=_strip_html(str(item.get("description") or "")),
                )
            )
        return [result for result in results if result.url]

    async def _search_serper(self, query: str) -> list[SearchResult]:
        if not self.config.serper_api_key:
            return []
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.config.serper_api_key,
            "User-Agent": self.config.user_agent,
        }
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": self.config.max_results},
            ) as response:
                if response.status >= 400:
                    return []
                data = await response.json(content_type=None)
        items = (data or {}).get("organic") or []
        results: list[SearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=str(item.get("link") or ""),
                    snippet=_strip_html(str(item.get("snippet") or "")),
                )
            )
        return [result for result in results if result.url]

    async def _search_duckduckgo(self, query: str) -> list[SearchResult]:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": self.config.user_agent,
        }
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as response:
                if response.status >= 400:
                    return []
                text = await response.text(errors="replace")
        return self._parse_duckduckgo_html(text)

    def _parse_duckduckgo_html(self, text: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        matches = list(pattern.finditer(text))
        for index, match in enumerate(matches):
            raw_url = html.unescape(match.group("href"))
            title = _strip_html(match.group("title"))
            snippet = ""
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else start + 2500
            block = text[start:end]
            snippet_match = re.search(
                r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
                block,
                re.IGNORECASE | re.DOTALL,
            )
            if snippet_match:
                snippet = _strip_html(snippet_match.group("snippet"))
            results.append(SearchResult(title=title, url=_unwrap_duckduckgo_url(raw_url), snippet=snippet))
            if len(results) >= self.config.max_results:
                break
        return [result for result in results if result.url]


def _strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return " ".join(value.split())


def _unwrap_duckduckgo_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    query = parse_qs(parsed.query)
    uddg = query.get("uddg")
    if uddg:
        return unquote(uddg[0])
    return raw_url
