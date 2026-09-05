from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.fortune.service import DailyFortuneService


class FortuneApiSyncTests(unittest.TestCase):
    def test_fortune_always_uses_main_chat_api_and_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                "os.environ",
                {
                    "OPENAI_BASE_URL": "https://main-one.example/v1",
                    "OPENAI_API_KEY": "main-key-one",
                    "OPENAI_MODEL": "main-model-one",
                    "FORTUNE_OPENAI_BASE_URL": "https://stale-fortune.example/v1",
                    "FORTUNE_OPENAI_API_KEY": "stale-key",
                    "FORTUNE_OPENAI_MODEL": "stale-model",
                },
                clear=False,
            ):
                service = DailyFortuneService(Path(temp_dir) / "fortune.json")
                self.assertEqual(service.client.config.model, "main-model-one")
                self.assertEqual(
                    service.client.config.base_url,
                    "https://main-one.example/v1",
                )

                with patch.dict(
                    "os.environ",
                    {
                        "OPENAI_BASE_URL": "https://main-two.example/v1",
                        "OPENAI_API_KEY": "main-key-two",
                        "OPENAI_MODEL": "main-model-two",
                    },
                    clear=False,
                ):
                    service.reload_client_from_env()

                self.assertEqual(service.client.config.model, "main-model-two")
                self.assertEqual(
                    service.client.config.base_url,
                    "https://main-two.example/v1",
                )


if __name__ == "__main__":
    unittest.main()
