from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from chat.agent.code_settings import (
    AgentCodeSettings,
    AgentCodeSettingsError,
    AgentCodeSettingsStore,
)
from chat.cog import AtriChat
from chat.code_settings_panel import (
    AgentCodeModelSelect,
    AgentCodeSettingsModal,
    AgentCodeSettingsView,
    _extract_model_ids,
    _models_url,
)


class AgentCodeSettingsStoreTests(unittest.TestCase):
    def test_round_trip_and_fingerprint_without_exposing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AgentCodeSettingsStore(Path(temp_dir))
            before = store.fingerprint()
            settings = AgentCodeSettings(
                enabled=True,
                base_url="https://code.example/v1",
                api_key="fake-private-key",
                model="code-model",
                max_tokens=4096,
            )

            store.save(settings)

            self.assertEqual(store.load(), settings)
            self.assertNotEqual(store.fingerprint(), before)
            self.assertNotIn("fake-private-key", store.fingerprint())

    def test_enabled_settings_require_complete_http_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = AgentCodeSettingsStore(Path(temp_dir))

            with self.assertRaises(AgentCodeSettingsError):
                store.save(AgentCodeSettings(True, "", "key", "model"))
            with self.assertRaises(AgentCodeSettingsError):
                store.save(
                    AgentCodeSettings(
                        True,
                        "file:///private/api",
                        "key",
                        "model",
                    )
                )


class FakePool:
    def __init__(self) -> None:
        self.closed = False
        self.runtime_env: dict[str, str] = {}

    def configure_runtime_env(self, values: dict[str, str]) -> None:
        self.runtime_env.update(values)

    async def close(self) -> None:
        self.closed = True


class AgentCodeHotReloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_disable_swaps_and_closes_old_runtime_without_normal_restart(self) -> None:
        cog = object.__new__(AtriChat)
        old_pool = FakePool()
        cog.agent_privacy = object()
        cog.dsh_runtime_pool = object()
        cog.dsh_code_runtime_pool = old_pool
        cog.agent_tool_server = None
        cog._agent_tool_bridge_configured = False
        cog._agent_session_locks = {}
        cog._code_runtime_reload_lock = asyncio.Lock()
        cog.agent_code_settings = AgentCodeSettings(
            True,
            "https://code.example/v1",
            "key",
            "model",
        )
        cog.agent_code_enabled = True
        cog.capability_prompt = "old"

        await cog._hot_reload_code_runtime(
            AgentCodeSettings(False, "", "", "")
        )

        self.assertIsNone(cog.dsh_code_runtime_pool)
        self.assertFalse(cog.agent_code_enabled)
        self.assertTrue(old_pool.closed)

    async def test_enable_configures_new_pool_when_tool_bridge_is_live(self) -> None:
        cog = object.__new__(AtriChat)
        new_pool = FakePool()
        cog.agent_privacy = object()
        cog.dsh_runtime_pool = object()
        cog.dsh_code_runtime_pool = None
        cog.agent_tool_server = type(
            "ToolServer",
            (),
            {"endpoint": "http://127.0.0.1:1234", "token": "fake-token"},
        )()
        cog._agent_tool_bridge_configured = True
        cog._agent_session_locks = {}
        cog._code_runtime_reload_lock = asyncio.Lock()
        cog.agent_code_settings = AgentCodeSettings(False, "", "", "")
        cog.agent_code_enabled = False
        cog.capability_prompt = "old"
        cog._create_code_runtime_pool = lambda _settings: new_pool
        settings = AgentCodeSettings(
            True,
            "https://code.example/v1",
            "fake-key",
            "code-model",
        )

        await cog._hot_reload_code_runtime(settings)

        self.assertIs(cog.dsh_code_runtime_pool, new_pool)
        self.assertTrue(cog.agent_code_enabled)
        self.assertEqual(new_pool.runtime_env["ATRI_AGENT_CODE_MODE"], "true")


class DiscordAgentCodePanelTests(unittest.IsolatedAsyncioTestCase):
    def test_model_endpoint_and_response_shapes(self) -> None:
        self.assertEqual(
            _models_url("https://api.example/v1"),
            "https://api.example/v1/models",
        )
        self.assertEqual(
            _extract_model_ids(
                {"data": [{"id": "model-b"}, {"name": "model-a"}, "model-c"]}
            ),
            ["model-a", "model-b", "model-c"],
        )

    async def test_panel_never_renders_api_key_value(self) -> None:
        secret = "fake-private-discord-panel-key"
        cog = type(
            "Cog",
            (),
            {
                "owner_user_id": 123,
                "agent_code_settings": AgentCodeSettings(
                    True,
                    "https://code.example/v1",
                    secret,
                    "code-model",
                    4096,
                ),
                "agent_code_enabled": True,
                "dsh_code_runtime_pool": object(),
            },
        )()
        panel = AgentCodeSettingsView(cog)

        embed = panel.build_embed()
        modal = AgentCodeSettingsModal(panel)
        rendered = str(embed.to_dict())

        self.assertNotIn(secret, rendered)
        self.assertIn("已设置", rendered)
        self.assertEqual(modal.api_key.default, "")

    async def test_pulled_models_are_presented_as_paginated_select(self) -> None:
        cog = type(
            "Cog",
            (),
            {
                "owner_user_id": 123,
                "agent_code_settings": AgentCodeSettings(
                    True,
                    "https://code.example/v1",
                    "fake-key",
                    "model-00",
                ),
                "agent_code_enabled": False,
                "dsh_code_runtime_pool": None,
            },
        )()
        panel = AgentCodeSettingsView(cog)
        panel.available_models = [f"model-{index:02d}" for index in range(30)]
        panel.refresh_model_controls()

        select = next(
            child for child in panel.children if isinstance(child, AgentCodeModelSelect)
        )

        self.assertEqual(len(select.options), 25)
        self.assertFalse(select.disabled)
        self.assertEqual(panel.total_model_pages, 2)


if __name__ == "__main__":
    unittest.main()
