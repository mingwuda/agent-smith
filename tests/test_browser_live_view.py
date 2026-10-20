"""浏览器实时视图 / takeover 路由测试

方案 A：/api/browser/live 返回 agent 当前浏览器页面快照（实时视图）。
方案 B：/api/browser/takeover-url 返回用户真浏览器接管入口。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_core"))

from tools import browser_tools as bt
from api.routes.browser import (
    router,
    LiveViewResponse,
    TakeoverResponse,
)


class LiveViewEmptyStateTest(unittest.TestCase):
    """浏览器未初始化时 get_live_view 返回空壳（不崩溃）。"""

    def test_empty_when_browser_never_initialized(self):
        # 备份全局状态，测试后还原
        old_browser, old_pages = bt._browser, dict(bt._pages)
        bt._browser = None
        bt._pages.clear()
        try:
            view = bt.get_live_view("default")
            self.assertIsInstance(view, dict)
            self.assertFalse(view.get("available"))
            # 空壳不含截图（不产生无意义大 payload）
            self.assertEqual(view.get("screenshot"), "")
        finally:
            bt._browser = old_browser
            bt._pages = old_pages

    def test_unavailable_when_session_has_no_page(self):
        old_browser, old_pages = bt._browser, dict(bt._pages)
        # 模拟浏览器已初始化（用一个非空对象）但该会话没有页面
        bt._browser = object()
        bt._pages = {"other_thread": object()}
        try:
            view = bt.get_live_view("default")
            self.assertIn("available", view)
            # other_thread 存在，default 不存在 → default 不可用，不崩溃
            self.assertFalse(view.get("available"))
        finally:
            bt._browser = old_browser
            bt._pages = old_pages


class BrowserRoutesRegisteredTest(unittest.TestCase):
    """路由注册正确：两个接手端点挂在 server 上。"""

    def test_router_has_both_endpoints(self):
        routes = [r.path for r in router.routes]
        self.assertIn("/api/browser/live", routes)
        self.assertIn("/api/browser/takeover-url", routes)

    def test_live_view_response_model_fields(self):
        # response_model 字段符合前端契约
        fields = list(LiveViewResponse.model_fields.keys())
        for f in ("available", "url", "title", "screenshot", "dom", "note"):
            self.assertIn(f, fields)
        self.assertIn("backend", fields)

    def test_takeover_response_model_fields(self):
        fields = list(TakeoverResponse.model_fields.keys())
        for f in ("available", "url", "cdp_url", "note"):
            self.assertIn(f, fields)


if __name__ == "__main__":
    unittest.main()