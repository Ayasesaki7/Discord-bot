from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DshCapacityConfigTests(unittest.TestCase):
    def test_normal_chat_separates_provider_capacity_from_memory_target(self) -> None:
        config = (PROJECT_ROOT / 'agent_runtime' / 'dsh' / 'cordis.yml').read_text(
            encoding='utf-8'
        )

        self.assertIn('ATRI_DSH_PROVIDER_CONTEXT_WINDOW ?? 1048576', config)
        self.assertIn('ATRI_DSH_CONTEXT_WINDOW ?? 140000) * 0.78', config)
        self.assertIn(
            'retainTokens: !!js Math.floor(Number(process.env.ATRI_DSH_CONTEXT_WINDOW ?? 140000) * 0.2)',
            config,
        )
        self.assertNotIn('retainRatio:', config)
        self.assertNotIn(
            'contextWindow: !!js Number(process.env.ATRI_DSH_CONTEXT_WINDOW',
            config,
        )
        self.assertIn('maxRetries: 0', config)
        self.assertNotIn('maxRetries: 2', config)

    def test_maintenance_runtime_can_override_provider_capacity(self) -> None:
        config = (
            PROJECT_ROOT / 'agent_runtime' / 'dsh' / 'cordis-code.yml'
        ).read_text(encoding='utf-8')

        self.assertIn('ATRI_AGENT_CODE_PROVIDER_CONTEXT_WINDOW', config)
        self.assertIn('ATRI_DSH_PROVIDER_CONTEXT_WINDOW ?? 1048576', config)
        self.assertIn('ATRI_DSH_CONTEXT_WINDOW ?? 140000) * 0.72', config)
        self.assertIn(
            'retainTokens: !!js Math.floor(Number(process.env.ATRI_DSH_CONTEXT_WINDOW ?? 140000) * 0.18)',
            config,
        )
        self.assertNotIn('retainRatio:', config)
        self.assertIn('maxRetries: 0', config)
        self.assertNotIn('maxRetries: 2', config)

    def test_jsonrpc_bridge_exposes_native_manual_compaction(self) -> None:
        bridge = (
            PROJECT_ROOT
            / 'agent_runtime'
            / 'dsh'
            / 'plugins'
            / 'atri-sdk-jsonrpc-server'
            / 'index.js'
        ).read_text(encoding='utf-8')

        self.assertIn("method === 'session/compact'", bridge)
        self.assertIn("compaction.compactNow(rec.handle.agent", bridge)
        self.assertIn('shadowedTokens: result.shadowedTokenCount', bridge)

    def test_discord_snowflake_schema_never_accepts_json_integers(self) -> None:
        plugin = (
            PROJECT_ROOT
            / 'agent_runtime'
            / 'dsh'
            / 'plugins'
            / 'atri-tools'
            / 'index.js'
        ).read_text(encoding='utf-8')

        helper_start = plugin.index('function discordSnowflakeDefinition')
        helper_end = plugin.index('\n}\n', helper_start) + 3
        helper = plugin[helper_start:helper_end]
        self.assertIn("type: 'string'", helper)
        self.assertNotIn("type: 'integer'", helper)
        self.assertIn('Never emit this ID as a JSON integer', helper)

        query_start = plugin.index("name: 'discord_query'")
        query_end = plugin.index("name: 'discord_visual_inspect'", query_start)
        query_schema = plugin[query_start:query_end]
        self.assertIn('user_ref:', query_schema)
        self.assertNotIn('user_id:', query_schema)

        manage_start = plugin.index("name: 'discord_manage'")
        manage_end = plugin.index('function registerWebSearchTool', manage_start)
        manage_schema = plugin[manage_start:manage_end]
        self.assertIn('user_ref:', manage_schema)
        self.assertIn('role_ref:', manage_schema)
        self.assertNotIn('user_id:', manage_schema)
        self.assertNotIn('role_id:', manage_schema)


if __name__ == '__main__':
    unittest.main()
