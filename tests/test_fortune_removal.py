from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FortuneRemovalTests(unittest.TestCase):
    def test_no_module_sources_or_compatibility_loaders_remain(self):
        for directory in ('fortune', 'tools/fortune'):
            self.assertEqual(list((PROJECT_ROOT / directory).glob('*.py')), [])

    def test_bot_does_not_load_fortune_extension(self):
        source = (PROJECT_ROOT / 'bot.py').read_text(encoding='utf-8-sig')
        ast.parse(source)
        self.assertNotIn("load_extension('fortune')", source)
        for extension in ('chat', 'music', 'roles', 'bilibili', 'douyin'):
            self.assertIn(f"load_extension('{extension}')", source)

    def test_chat_and_maintenance_no_longer_advertise_fortune(self):
        for relative in (
            'chat/cog.py', 'chat/admin_panel.py', 'chat/whitelist_panel.py',
            'chat/agent/tool_server.py', 'chat/agent/project_tools.py',
            'agent_runtime/dsh/plugins/atri-tools/index.js',
            'agent_runtime/dsh/cordis-code.yml', '.env.example',
        ):
            source = (PROJECT_ROOT / relative).read_text(encoding='utf-8')
            for removed in ('daily_fortune', 'DailyFortuneCog', 'tools/fortune', 'FORTUNE_', '每日运势'):
                self.assertNotIn(removed, source, relative)


if __name__ == '__main__':
    unittest.main()
