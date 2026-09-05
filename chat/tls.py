from __future__ import annotations

import ssl

import aiohttp
import certifi


def build_verified_ssl_context() -> ssl.SSLContext:
    """Build ATRI's shared outbound TLS context from certifi's CA bundle."""

    return ssl.create_default_context(cafile=certifi.where())


def build_verified_connector() -> aiohttp.TCPConnector:
    """Create an aiohttp connector that never falls back to the stale OS chain."""

    return aiohttp.TCPConnector(ssl=build_verified_ssl_context())
