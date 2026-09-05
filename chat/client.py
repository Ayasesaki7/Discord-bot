from __future__ import annotations

import asyncio
import json
import os
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp

from .tls import build_verified_ssl_context


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, '').strip().lower()
    if not raw:
        return default
    return raw not in {'0', 'false', 'no', 'off'}


def _read_choice(name: str, allowed: set[str]) -> str | None:
    raw = os.getenv(name, '').strip().lower()
    if not raw:
        return None
    if raw in allowed:
        return raw
    return None


def _read_text(name: str) -> str | None:
    raw = os.getenv(name, '').strip()
    return raw or None


def _read_json_object(name: str) -> dict[str, object] | None:
    raw = os.getenv(name, '').strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return data
    return None


def _read_headers(name: str) -> dict[str, str]:
    raw = os.getenv(name, '').strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    headers: dict[str, str] = {}
    for key, value in data.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            headers[key_text] = value_text
    return headers


@dataclass(slots=True)
class ChatCompletionUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(slots=True)
class OpenAICompatibleConfig:
    base_url: str
    api_key: str
    model: str
    max_tokens: int | None = None
    temperature: float = 0.9
    timeout_seconds: int = 60
    include_stream_usage: bool = True
    enable_web_search: bool = False
    web_search_context_size: str | None = None
    web_search_country: str | None = None
    web_search_city: str | None = None
    web_search_region: str | None = None
    web_search_timezone: str | None = None
    web_search_options_override: dict[str, object] | None = None
    retry_count: int = 2
    retry_backoff_seconds: float = 1.5
    user_agent: str | None = None
    referer: str | None = None
    title: str | None = None
    extra_headers: dict[str, str] | None = None

    @classmethod
    def from_env(cls) -> 'OpenAICompatibleConfig':
        return cls(
            base_url=os.getenv('OPENAI_BASE_URL', '').strip(),
            api_key=os.getenv('OPENAI_API_KEY', '').strip(),
            model=os.getenv('OPENAI_MODEL', '').strip(),
            max_tokens=(
                max(_read_int('OPENAI_MAX_TOKENS', 0), 1)
                if _read_int('OPENAI_MAX_TOKENS', 0) > 0
                else None
            ),
            temperature=_read_float('OPENAI_TEMPERATURE', 0.9),
            timeout_seconds=_read_int('OPENAI_TIMEOUT', 60),
            include_stream_usage=_read_bool('OPENAI_STREAM_INCLUDE_USAGE', True),
            enable_web_search=_read_bool('OPENAI_ENABLE_WEB_SEARCH', False),
            web_search_context_size=_read_choice('OPENAI_WEB_SEARCH_CONTEXT_SIZE', {'low', 'medium', 'high'}),
            web_search_country=_read_text('OPENAI_WEB_SEARCH_COUNTRY'),
            web_search_city=_read_text('OPENAI_WEB_SEARCH_CITY'),
            web_search_region=_read_text('OPENAI_WEB_SEARCH_REGION'),
            web_search_timezone=_read_text('OPENAI_WEB_SEARCH_TIMEZONE'),
            web_search_options_override=_read_json_object('OPENAI_WEB_SEARCH_OPTIONS_JSON'),
            retry_count=max(_read_int('OPENAI_RETRY_COUNT', 2), 0),
            retry_backoff_seconds=max(_read_float('OPENAI_RETRY_BACKOFF', 1.5), 0.0),
            user_agent=_read_text('OPENAI_USER_AGENT') or 'ATRI-DiscordBot/1.0',
            referer=_read_text('OPENAI_HTTP_REFERER'),
            title=_read_text('OPENAI_X_TITLE'),
            extra_headers=_read_headers('OPENAI_EXTRA_HEADERS_JSON') or None,
        )

    def build_web_search_options(self) -> dict[str, object] | None:
        if not self.enable_web_search:
            return None

        if self.web_search_options_override is not None:
            return dict(self.web_search_options_override)

        options: dict[str, object] = {}
        if self.web_search_context_size:
            options['search_context_size'] = self.web_search_context_size

        approximate: dict[str, str] = {}
        if self.web_search_country:
            approximate['country'] = self.web_search_country
        if self.web_search_city:
            approximate['city'] = self.web_search_city
        if self.web_search_region:
            approximate['region'] = self.web_search_region
        if self.web_search_timezone:
            approximate['timezone'] = self.web_search_timezone
        if approximate:
            options['user_location'] = {
                'type': 'approximate',
                'approximate': approximate,
            }

        return options

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip('/')
        if base.endswith('/chat/completions'):
            return base
        return f'{base}/chat/completions'

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


class OpenAICompatibleClient:
    def __init__(self, config: OpenAICompatibleConfig):
        self.config = config

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        return build_verified_ssl_context()

    def is_configured(self) -> bool:
        return self.config.is_configured()

    async def create_chat_completion(
        self,
        messages: list[dict[str, object]],
        *,
        temperature: float | None = None,
        usage: ChatCompletionUsage | None = None,
        debug: dict[str, object] | None = None,
    ) -> str:
        chunks = []
        async for chunk in self.stream_chat_completion(
            messages,
            temperature=temperature,
            usage=usage,
            debug=debug,
        ):
            chunks.append(chunk)

        content = ''.join(chunks).strip()
        if not content:
            raise RuntimeError('AI API returned an empty message.')
        return content

    async def stream_chat_completion(
        self,
        messages: list[dict[str, object]],
        *,
        temperature: float | None = None,
        usage: ChatCompletionUsage | None = None,
        debug: dict[str, object] | None = None,
    ) -> AsyncIterator[str]:
        if not self.is_configured():
            raise RuntimeError('Missing OPENAI_BASE_URL, OPENAI_API_KEY, or OPENAI_MODEL.')

        headers = {
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream, application/json',
        }
        if self.config.api_key:
            headers['Authorization'] = f'Bearer {self.config.api_key}'
        if self.config.user_agent:
            headers['User-Agent'] = self.config.user_agent
        if self.config.referer:
            headers['HTTP-Referer'] = self.config.referer
        if self.config.title:
            headers['X-Title'] = self.config.title
        if self.config.extra_headers:
            headers.update(self.config.extra_headers)

        base_payload = {
            'model': self.config.model,
            'messages': messages,
            'temperature': self.config.temperature if temperature is None else temperature,
            'stream': True,
        }
        if self.config.max_tokens is not None:
            base_payload['max_tokens'] = self.config.max_tokens
        web_search_options = self.config.build_web_search_options()
        if web_search_options is not None:
            base_payload['web_search_options'] = web_search_options
        payloads = [dict(base_payload)]
        if self.config.include_stream_usage:
            payloads.insert(
                0,
                {
                    **base_payload,
                    'stream_options': {'include_usage': True},
                },
            )

        if debug is not None:
            debug.clear()
            debug['request_url'] = self.config.chat_completions_url
            debug['model'] = self.config.model
            debug['temperature'] = base_payload['temperature']
            debug['message_count'] = len(messages)
            debug['retry_count'] = self.config.retry_count
            debug['attempts'] = []

        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        connector = aiohttp.TCPConnector(ssl=self._build_ssl_context())
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
        ) as session:
            for attempt_index, payload in enumerate(payloads):
                max_retries = self.config.retry_count
                for retry_index in range(max_retries + 1):
                    attempt_debug: dict[str, object] = {
                        'attempt': attempt_index + 1,
                        'include_stream_usage': 'stream_options' in payload,
                        'retry_attempt': retry_index + 1,
                    }
                    if debug is not None:
                        attempts = debug.get('attempts')
                        if isinstance(attempts, list):
                            attempts.append(attempt_debug)
                        else:
                            debug['attempts'] = [attempt_debug]

                    try:
                        async with session.post(
                            self.config.chat_completions_url,
                            headers=headers,
                            json=payload,
                        ) as response:
                            attempt_debug['response_status'] = response.status
                            content_type = response.headers.get('Content-Type')
                            if content_type:
                                attempt_debug['content_type'] = content_type

                            if response.status >= 400:
                                body = await self._read_json_text(response)
                                attempt_debug['error_body'] = self._compact_text(body, 4000)
                                if (
                                    attempt_index == 0
                                    and retry_index == 0
                                    and len(payloads) > 1
                                    and self._should_retry_without_usage(response.status, body)
                                ):
                                    attempt_debug['retry_without_usage'] = True
                                    break
                                if retry_index < max_retries and self._should_retry_request(response.status):
                                    delay = self._retry_delay_seconds(response, retry_index)
                                    attempt_debug['retry_scheduled_in_seconds'] = delay
                                    await asyncio.sleep(delay)
                                    continue
                                raise RuntimeError(self._format_http_error(response.status, body))

                            async for chunk in self._iter_response_chunks(response, usage, debug=attempt_debug):
                                if chunk:
                                    yield chunk
                            return
                    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
                        attempt_debug['transport_error_type'] = exc.__class__.__name__
                        if self._is_tls_certificate_error(exc):
                            attempt_debug['transport_failure'] = 'tls_certificate_validation_failed'
                            attempt_debug['retry_suppressed'] = 'non_retryable_tls_certificate'
                            raise RuntimeError(
                                'AI API TLS certificate validation failed; check the upstream certificate'
                            ) from exc
                        if retry_index < max_retries:
                            delay = self._retry_delay_seconds(None, retry_index)
                            attempt_debug['retry_scheduled_in_seconds'] = delay
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(
                            'AI API request failed before a valid response was received '
                            f'({exc.__class__.__name__})'
                        ) from exc

    def _is_tls_certificate_error(self, exc: BaseException) -> bool:
        current: BaseException | None = exc
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, (aiohttp.ClientConnectorCertificateError, ssl.SSLCertVerificationError)):
                return True
            os_error = getattr(current, 'os_error', None)
            if isinstance(os_error, ssl.SSLCertVerificationError):
                return True
            current = current.__cause__ or current.__context__
        return False

    async def _iter_response_chunks(
        self,
        response: aiohttp.ClientResponse,
        usage: ChatCompletionUsage | None,
        *,
        debug: dict[str, object] | None = None,
    ) -> AsyncIterator[str]:
        raw_lines: list[bytes] = []
        event_lines: list[str] = []
        saw_sse = False

        if debug is not None:
            debug['saw_sse'] = False
            debug['sse_event_count'] = 0
            debug['content_chunk_count'] = 0
            debug['done_received'] = False

        async for raw_line in response.content:
            raw_lines.append(raw_line)
            line = self._decode_bytes(raw_line).strip()

            if not saw_sse and not (
                line.startswith('data:') or line.startswith(':') or not line
            ):
                continue

            if line.startswith('data:') or line.startswith(':') or saw_sse:
                saw_sse = True
                if debug is not None:
                    debug['saw_sse'] = True
                    debug['response_mode'] = 'sse'

            if not saw_sse:
                continue

            if not line:
                chunk = self._extract_sse_chunk(event_lines, usage, debug=debug)
                event_lines.clear()
                if chunk is None:
                    return
                if chunk:
                    if debug is not None:
                        debug['content_chunk_count'] = int(debug.get('content_chunk_count', 0)) + 1
                    yield chunk
                continue

            if line.startswith(':'):
                continue

            if line.startswith('data:'):
                event_lines.append(line[5:].strip())

        if saw_sse:
            chunk = self._extract_sse_chunk(event_lines, usage, debug=debug)
            if chunk is None:
                return
            if chunk:
                if debug is not None:
                    debug['content_chunk_count'] = int(debug.get('content_chunk_count', 0)) + 1
                yield chunk
            return

        body = self._decode_bytes(b''.join(raw_lines))
        if debug is not None:
            debug['response_mode'] = 'json'
            debug['raw_body_excerpt'] = self._compact_text(body, 4000)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            if debug is not None:
                debug['response_parse_error'] = 'invalid_json'
            raise RuntimeError('AI API returned invalid JSON.') from exc

        self._update_usage(usage, data)
        self._update_debug_from_payload(debug, data)
        content = self._extract_content(data)
        if content:
            if debug is not None:
                debug['content_chunk_count'] = int(debug.get('content_chunk_count', 0)) + 1
            yield content

    def _extract_sse_chunk(
        self,
        event_lines: list[str],
        usage: ChatCompletionUsage | None,
        *,
        debug: dict[str, object] | None = None,
    ) -> str | None:
        if not event_lines:
            return ''

        payload = '\n'.join(event_lines).strip()
        if not payload:
            return ''
        if payload == '[DONE]':
            if debug is not None:
                debug['done_received'] = True
            return None

        if debug is not None:
            debug['response_mode'] = 'sse'
            debug['sse_event_count'] = int(debug.get('sse_event_count', 0)) + 1
            debug['last_event_excerpt'] = self._compact_text(payload, 4000)

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            if debug is not None:
                debug['invalid_sse_payload'] = self._compact_text(payload, 4000)
            return ''

        self._update_usage(usage, data)
        self._update_debug_from_payload(debug, data)
        return self._extract_stream_delta(data)

    async def _read_json_text(self, response: aiohttp.ClientResponse) -> str:
        body = await response.read()
        return self._decode_bytes(body)

    def _decode_bytes(self, body: bytes) -> str:
        for encoding in ('utf-8-sig', 'utf-8', 'gb18030'):
            try:
                return body.decode(encoding)
            except UnicodeDecodeError:
                continue
        return body.decode('utf-8', errors='replace')

    def _extract_content(self, data: dict) -> str:
        choices = data.get('choices') or []
        if not choices:
            return ''

        message = choices[0].get('message') or {}
        content = message.get('content')
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return self._extract_text_parts(content).strip()
        return ''

    def _extract_stream_delta(self, data: dict) -> str:
        choices = data.get('choices') or []
        if not choices:
            return ''

        choice = choices[0]
        delta = choice.get('delta') or choice.get('message') or {}

        content = delta.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return self._extract_text_parts(content)

        if isinstance(choice.get('text'), str):
            return choice['text']
        return ''

    def _extract_text_parts(self, items: list) -> str:
        text_parts = []
        for item in items:
            if not isinstance(item, dict):
                continue

            if isinstance(item.get('text'), str):
                text_parts.append(item['text'])
                continue

            if isinstance(item.get('content'), str):
                text_parts.append(item['content'])

        return ''.join(text_parts)

    def _first_int(self, data: dict, *keys: str) -> int | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.isdigit():
                    return int(stripped)
        return None

    def _update_usage(self, usage: ChatCompletionUsage | None, data: dict) -> None:
        if usage is None or not isinstance(data, dict):
            return

        usage_payload = data.get('usage')
        if not isinstance(usage_payload, dict):
            usage_payload = data.get('usage_metadata')
        if not isinstance(usage_payload, dict):
            usage_payload = data.get('usageMetadata')
        if not isinstance(usage_payload, dict):
            return

        input_tokens = self._first_int(
            usage_payload,
            'prompt_tokens',
            'input_tokens',
            'promptTokenCount',
            'inputTokenCount',
        )
        output_tokens = self._first_int(
            usage_payload,
            'completion_tokens',
            'output_tokens',
            'completionTokenCount',
            'candidatesTokenCount',
            'outputTokenCount',
        )
        total_tokens = self._first_int(
            usage_payload,
            'total_tokens',
            'totalTokenCount',
        )

        if input_tokens is not None:
            usage.input_tokens = input_tokens
        if output_tokens is not None:
            usage.output_tokens = output_tokens
        if total_tokens is not None:
            usage.total_tokens = total_tokens
        elif usage.input_tokens is not None and usage.output_tokens is not None:
            usage.total_tokens = usage.input_tokens + usage.output_tokens

    def _update_debug_from_payload(
        self,
        debug: dict[str, object] | None,
        data: dict,
    ) -> None:
        if debug is None or not isinstance(data, dict):
            return

        if isinstance(data.get('id'), str):
            debug['response_id'] = data['id']
        if isinstance(data.get('object'), str):
            debug['response_object'] = data['object']
        if isinstance(data.get('model'), str):
            debug['response_model'] = data['model']
        if isinstance(data.get('system_fingerprint'), str):
            debug['system_fingerprint'] = data['system_fingerprint']

        choices = data.get('choices')
        if isinstance(choices, list):
            debug['choices_count'] = len(choices)
            if choices and isinstance(choices[0], dict):
                finish_reason = choices[0].get('finish_reason')
                if isinstance(finish_reason, str):
                    debug['finish_reason'] = finish_reason

        error_payload = data.get('error')
        if isinstance(error_payload, dict):
            debug['error_payload'] = self._compact_text(
                json.dumps(error_payload, ensure_ascii=False),
                4000,
            )
        self._update_tool_debug_from_payload(debug, data)
        sources = self._extract_web_sources(data)
        if sources:
            existing_sources = debug.get('web_sources')
            if not isinstance(existing_sources, list):
                existing_sources = []
                debug['web_sources'] = existing_sources
            existing_urls = {
                str(item.get('url'))
                for item in existing_sources
                if isinstance(item, dict) and item.get('url')
            }
            for source in sources:
                if source['url'] in existing_urls:
                    continue
                existing_sources.append(source)
                existing_urls.add(source['url'])
                if len(existing_sources) >= 20:
                    break

    def _update_tool_debug_from_payload(
        self,
        debug: dict[str, object] | None,
        data: dict,
    ) -> None:
        if debug is None or not isinstance(data, dict):
            return

        labels = self._detect_tool_labels(data)
        if not labels:
            return

        debug['tool_call_detected'] = True
        existing = debug.get('tools_used')
        if not isinstance(existing, list):
            existing = []
            debug['tools_used'] = existing
        for label in labels:
            if label not in existing:
                existing.append(label)

    def _detect_tool_labels(self, data: object) -> set[str]:
        labels: set[str] = set()
        network_search_key_tokens = (
            'grounding',
            'web_search',
            'websearch',
            'googlesearch',
            'google_search',
            'searchentrypoint',
            'websearchqueries',
            'citation',
            'citations',
            'url_citation',
            'urlcitation',
            'annotation',
            'annotations',
            'source_attribution',
            'sourceattribution',
            'source_attributions',
            'sourceattributions',
            'retrieval',
            'search_metadata',
            'searchmetadata',
            'search_result',
            'searchresult',
        )
        network_search_value_tokens = (
            'google_search',
            'web_search',
            'web search',
            'url_citation',
            'url citation',
        )
        tool_key_names = {
            'tool_calls',
            'tool_call',
            'toolcalls',
            'toolcall',
            'function_call',
            'functioncall',
        }

        def walk(value: object, parent_key: str = '') -> None:
            if isinstance(value, dict):
                for raw_key, item in value.items():
                    key = str(raw_key)
                    key_folded = key.casefold()
                    if any(token in key_folded for token in network_search_key_tokens):
                        labels.add('network_search')
                    if key_folded in tool_key_names:
                        labels.add(self._tool_label_from_payload(item) or 'tool')
                    walk(item, key)
                return

            if isinstance(value, list):
                for item in value:
                    walk(item, parent_key)
                return

            if isinstance(value, str):
                lowered = value.casefold()
                parent_folded = parent_key.casefold()
                if any(token in parent_folded for token in network_search_key_tokens):
                    labels.add('network_search')
                if any(token in lowered for token in network_search_value_tokens):
                    labels.add('network_search')

        walk(data)
        if 'tool' in labels and 'network_search' in labels:
            labels.discard('tool')
        return labels

    def _extract_web_sources(self, data: object) -> list[dict[str, str]]:
        """Collect citation URLs emitted by common OpenAI/Gemini gateways."""

        results: list[dict[str, str]] = []
        seen: set[str] = set()
        citation_tokens = (
            'citation', 'grounding', 'source', 'search', 'annotation',
            'attribution', 'web',
        )

        def add(url: object, title: object = '') -> None:
            if not isinstance(url, str):
                return
            normalized = url.strip()
            if not normalized.casefold().startswith(('https://', 'http://')):
                return
            if normalized in seen or len(results) >= 20:
                return
            seen.add(normalized)
            results.append(
                {
                    'url': normalized[:2000],
                    'title': str(title or '').strip()[:300],
                }
            )

        def walk(value: object, parent_key: str = '') -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item, parent_key)
                return
            if not isinstance(value, dict):
                return

            folded_context = ' '.join(
                [
                    parent_key,
                    str(value.get('type') or ''),
                    *[str(key) for key in value.keys()],
                ]
            ).casefold()
            citation_like = any(token in folded_context for token in citation_tokens)
            if citation_like:
                title = (
                    value.get('title')
                    or value.get('name')
                    or value.get('display_name')
                    or value.get('displayName')
                    or ''
                )
                for key in ('url', 'uri', 'link'):
                    add(value.get(key), title)
                nested_citation = value.get('url_citation') or value.get('urlCitation')
                if isinstance(nested_citation, dict):
                    add(
                        nested_citation.get('url') or nested_citation.get('uri'),
                        nested_citation.get('title') or title,
                    )
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    walk(item, str(key))

        walk(data)
        return results

    def _tool_label_from_payload(self, value: object) -> str:
        if isinstance(value, list):
            labels = [
                self._tool_label_from_payload(item)
                for item in value
            ]
            return next((label for label in labels if label), '')

        if not isinstance(value, dict):
            return ''

        candidate = value.get('name')
        if not isinstance(candidate, str):
            function_payload = value.get('function')
            if isinstance(function_payload, dict):
                candidate = function_payload.get('name')
        if not isinstance(candidate, str):
            return ''

        lowered = candidate.casefold()
        if any(token in lowered for token in ('search', 'google', 'web')):
            return 'network_search'
        return 'tool'

    def _should_retry_without_usage(self, status: int, body: str) -> bool:
        if status not in {400, 404, 415, 422}:
            return False

        lowered = body.lower()
        return any(
            token in lowered
            for token in (
                'stream_options',
                'include_usage',
                'usage',
            )
        )

    def _should_retry_request(self, status: int) -> bool:
        return status in {429, 500, 502, 503, 504, 520, 522, 524}

    def _retry_delay_seconds(
        self,
        response: aiohttp.ClientResponse | None,
        retry_index: int,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get('Retry-After', '').strip()
            if retry_after.isdigit():
                return max(float(retry_after), 0.0)
        base = max(self.config.retry_backoff_seconds, 0.0)
        return base * (retry_index + 1)

    def _format_http_error(self, status: int, body: str) -> str:
        compact = ' '.join(body.split())
        if len(compact) > 240:
            compact = compact[:237] + '...'
        return f'AI API request failed ({status}): {compact}'

    def _compact_text(self, text: str, limit: int = 400) -> str:
        compact = ' '.join(text.split())
        if len(compact) > limit:
            compact = compact[: limit - 3] + '...'
        return compact
