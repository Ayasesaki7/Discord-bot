from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from chat.whitelist_panel import (
    WHITELIST_ENV_KEY,
    persist_whitelist_guild_ids,
)


class ChatWhitelistHotUpdateTests(unittest.TestCase):
    def test_persist_updates_live_cog_before_restart(self) -> None:
        cog = SimpleNamespace(whitelisted_guild_ids={100})
        previous = os.environ.get(WHITELIST_ENV_KEY)
        try:
            with patch("chat.whitelist_panel._write_env_updates") as write_env:
                persist_whitelist_guild_ids(cog, {300, 200})

            self.assertEqual(cog.whitelisted_guild_ids, {200, 300})
            self.assertEqual(os.environ[WHITELIST_ENV_KEY], "200,300")
            write_env.assert_called_once_with({WHITELIST_ENV_KEY: "200,300"})
        finally:
            if previous is None:
                os.environ.pop(WHITELIST_ENV_KEY, None)
            else:
                os.environ[WHITELIST_ENV_KEY] = previous


if __name__ == "__main__":
    unittest.main()
