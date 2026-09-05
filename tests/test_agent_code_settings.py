from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chat.agent.code_settings import (
    AgentCodeSettings,
    AgentCodeSettingsError,
    AgentCodeSettingsStore,
    WebSearchSettingsStore,
)
from chat.cog import AtriChat
from chat.code_settings_panel import (
    AgentCodeModelSelect,
    AgentCodeSettingsModal,
    AgentCodeSettingsView,
    _extract_model_ids,
    _models_url,
)
from chat.admin_panel import ChatConfigModal, _reload_agent_chat_runtime_if_needed


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

    def test_web_search_settings_use_an_independent_protected_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = WebSearchSettingsStore(root)
            settings = AgentCodeSettings(
                True,
                "https://search.example/v1",
                "fake-search-key",
                "search-model",
                8192,
            )

            store.save(settings)

            self.assertEqual(store.load(), settings)
            self.assertEqual(store.path.name, "web_search_api.json")


class FakePool:
    def __init__(self) -> None:
        self.closed = False
        self.recycled = False
        self.runtime_env: dict[str, str] = {}
        self.template = SimpleNamespace(model="test-model")

    def configure_runtime_env(self, values: dict[str, str]) -> None:
        self.runtime_env.update(values)

    async def close(self) -> None:
        self.closed = True

    async def recycle_runtimes(self) -> int:
        self.recycled = True
        return 2


class AgentChatHotReloadTests(unittest.IsolatedAsyncioTestCase):
    def test_chat_config_modal_allows_manual_model_id(self) -> None:
        config = SimpleNamespace(
            base_url="https://api.example/v1",
            model="hidden-upstream-model",
        )
        panel = SimpleNamespace(
            cog=SimpleNamespace(
                client=SimpleNamespace(config=config),
                history_limit=300,
                recent_visual_context_window=3,
            )
        )

        modal = ChatConfigModal(panel)

        self.assertEqual(modal.model.default, "hidden-upstream-model")
        self.assertTrue(modal.model.required)
        self.assertEqual(len(modal.children), 5)

    async def test_main_api_reload_swaps_pool_and_preserves_tool_bridge(self) -> None:
        cog = object.__new__(AtriChat)
        old_pool = FakePool()
        new_pool = FakePool()
        new_pool.template.model = "new-chat-model"
        cog.agent_v2_enabled = True
        cog.agent_privacy = object()
        cog.dsh_runtime_pool = old_pool
        cog.agent_tool_server = SimpleNamespace(
            endpoint="http://127.0.0.1:1234",
            token="fake-token",
        )
        cog._agent_tool_bridge_configured = True
        cog._chat_runtime_reload_lock = asyncio.Lock()
        cog._create_normal_runtime_pool = lambda: new_pool

        recycled = await cog.reload_agent_chat_runtime_from_env()

        self.assertIs(cog.dsh_runtime_pool, new_pool)
        self.assertTrue(old_pool.recycled)
        self.assertEqual(recycled, 2)
        self.assertEqual(new_pool.runtime_env["ATRI_AGENT_CODE_MODE"], "false")

    async def test_admin_api_keys_trigger_reload_but_history_only_does_not(self) -> None:
        cog = SimpleNamespace(reload_agent_chat_runtime_from_env=AsyncMock())

        changed = await _reload_agent_chat_runtime_if_needed(
            cog,
            {"OPENAI_MODEL": "new-chat-model"},
        )
        unchanged = await _reload_agent_chat_runtime_if_needed(
            cog,
            {"CHAT_HISTORY_LIMIT": "300"},
        )

        self.assertTrue(changed)
        self.assertFalse(unchanged)
        cog.reload_agent_chat_runtime_from_env.assert_awaited_once()


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

    async def test_web_search_panel_uses_separate_settings_and_pull_only_modal(self) -> None:
        secret = "fake-private-search-key"
        cog = type(
            "Cog",
            (),
            {
                "owner_user_id": 123,
                "agent_code_settings": AgentCodeSettings(False, "", "", ""),
                "agent_code_enabled": False,
                "dsh_code_runtime_pool": None,
                "web_search_settings": AgentCodeSettings(
                    True,
                    "https://search.example/v1",
                    secret,
                    "search-model",
                    8192,
                ),
            },
        )()
        panel = AgentCodeSettingsView(cog, profile="web_search")
        modal = AgentCodeSettingsModal(panel)

        rendered = str(panel.build_embed().to_dict())
        self.assertIn("联网搜索 Agent", rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(modal.model, modal.children)


if __name__ == "__main__":
    unittest.main()
