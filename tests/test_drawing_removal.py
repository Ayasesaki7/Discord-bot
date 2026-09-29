from types import SimpleNamespace
import unittest

from chat.admin_panel import ChatAdminView


class DrawingRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_panel_does_not_require_or_offer_drawing_agent(self) -> None:
        cog = SimpleNamespace(
            owner_user_id=123,
            client=SimpleNamespace(config=SimpleNamespace(model="test-model")),
        )
        panel = ChatAdminView(cog)
        labels = {getattr(item, "label", None) for item in panel.children}
        self.assertNotIn("编辑绘图路由", labels)
        self.assertIn("编辑连接/上下文", labels)
        self.assertIn("刷新模型列表", labels)
        self.assertFalse(hasattr(panel, "open_draw_router_modal"))
        panel.stop()


if __name__ == "__main__":
    unittest.main()
