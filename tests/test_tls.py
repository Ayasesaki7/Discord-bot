from __future__ import annotations

import unittest

from chat.tls import build_verified_ssl_context


class SharedTlsContextTests(unittest.TestCase):
    def test_certifi_context_has_ca_certificates_and_hostname_checks(self) -> None:
        context = build_verified_ssl_context()
        stats = context.cert_store_stats()

        self.assertTrue(context.check_hostname)
        self.assertGreater(stats.get("x509_ca", 0), 0)


if __name__ == "__main__":
    unittest.main()
