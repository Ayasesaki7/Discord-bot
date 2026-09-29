from __future__ import annotations

import ssl
import unittest
from unittest.mock import patch

import aiohttp

from chat.client import OpenAICompatibleClient, OpenAICompatibleConfig


class _CertificateFailingRequest:
    async def __aenter__(self):
        try:
            raise ssl.SSLCertVerificationError(1, "certificate has expired")
        except ssl.SSLCertVerificationError as exc:
            raise aiohttp.ClientConnectionError("TLS handshake failed") from exc

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _FakeClientSession:
    calls = 0

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def post(self, *_args, **_kwargs) -> _CertificateFailingRequest:
        type(self).calls += 1
        return _CertificateFailingRequest()


class ChatClientTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_certificate_validation_failure_is_not_retried_or_echoed(self) -> None:
        client = OpenAICompatibleClient(
            OpenAICompatibleConfig(
                base_url="https://private.example/v1",
                api_key="test-key",
                model="test-model",
                retry_count=5,
            )
        )
        debug: dict[str, object] = {}
        _FakeClientSession.calls = 0

        with patch("chat.client.aiohttp.ClientSession", _FakeClientSession):
            with self.assertRaises(RuntimeError) as raised:
                _ = [
                    chunk
                    async for chunk in client.stream_chat_completion(
                        [{"role": "user", "content": "hello"}],
                        debug=debug,
                    )
                ]

        self.assertEqual(_FakeClientSession.calls, 1)
        self.assertIn("certificate validation failed", str(raised.exception))
        self.assertNotIn("certificate has expired", str(debug))
        self.assertNotIn("TLS handshake failed", str(debug))
        attempts = debug.get("attempts")
        self.assertIsInstance(attempts, list)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["transport_failure"], "tls_certificate_validation_failed")


if __name__ == "__main__":
    unittest.main()
