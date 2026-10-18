import asyncio
import unittest

import agent_core.tools.browser_tools as bt
from agent_core.tools.browser_tools import (
    TOOLS,
    _DOM_EXTRACT_JS,
    _DOM_MAX_OUTPUT_CHARS,
    _DOM_STABLE_JS,
    _format_dom_node,
    _format_dom_tree,
    _wait_dom_stable,
    browser_get_dom,
    browser_wait_for_dom_stable,
)


class DomNodeFormatTest(unittest.TestCase):
    """_format_dom_node 纯函数：行格式、脱敏标记、缩进。"""

    def test_interactive_node_full_line(self):
        line = _format_dom_node({
            "tag": "a", "id": "login-link", "depth": 2, "interactive": True,
            "href": "/login", "text": "登录", "rect": [8, 11, 32, 18], "sel": "#login-link",
        })
        self.assertEqual(
            line,
            '    ▸ <a#login-link> href=/login "登录" (8,11 32×18)  sel=#login-link',
        )

    def test_structure_node_marker_and_indent(self):
        line = _format_dom_node({"tag": "div", "depth": 1, "rect": [8, 30, 1264, 25],
                                 "sel": "body>div"})
        # 非交互节点用空格占位 marker，缩进 2 空格 + marker + 分隔空格
        self.assertEqual(line, '    <div> (8,30 1264×25)  sel=body>div')

    def test_password_field_redacted_at_format(self):
        line = _format_dom_node({
            "tag": "input", "type": "password", "depth": 1, "interactive": True,
            "text": "[redacted]", "label": "密码", "sel": "#p",
        })
        self.assertIn("[password]", line)
        self.assertIn("[redacted]", line)
        self.assertIn("label=密码", line)

    def test_input_without_rect_or_text(self):
        line = _format_dom_node({"tag": "input", "id": "u", "type": "text",
                                 "depth": 1, "interactive": True, "label": "用户名", "sel": "#u"})
        self.assertEqual(line, '  ▸ <input#u[text]> label=用户名  sel=#u')

    def test_missing_fields_do_not_crash(self):
        # 空 dict / 缺字段不应抛异常
        self.assertEqual(_format_dom_node({}), "  <?>")


class DomTreeFormatTest(unittest.TestCase):
    """_format_dom_tree：缩进树 + 超限截断。"""

    def test_joins_nodes_with_indent(self):
        nodes = [
            {"tag": "body", "depth": 0, "sel": "body"},
            {"tag": "button", "depth": 1, "interactive": True, "text": "Go", "sel": "body>button"},
        ]
        text, truncated = _format_dom_tree(nodes)
        self.assertFalse(truncated)
        self.assertIn("  ▸ <button>", text)

    def test_truncates_on_char_budget(self):
        original = bt._DOM_MAX_OUTPUT_CHARS
        try:
            bt._DOM_MAX_OUTPUT_CHARS = 120  # 只放得下约 2-3 行
            nodes = [{"tag": "div", "depth": 0, "sel": f"#d{i}", "text": "x" * 40}
                     for i in range(50)]
            text, truncated = _format_dom_tree(nodes)
            self.assertTrue(truncated)
            self.assertIn("DOM 过大", text)
            self.assertLess(len(text), 400)
        finally:
            bt._DOM_MAX_OUTPUT_CHARS = original


class DomJsGuardsTest(unittest.TestCase):
    """两段内联 JS 的关键安全/行为守卫（防误删）。"""

    def test_extract_js_has_core_guards(self):
        self.assertIn("[redacted]", _DOM_EXTRACT_JS)          # 密码脱敏
        self.assertIn("type === 'password'", _DOM_EXTRACT_JS)
        self.assertIn("maxDepth", _DOM_EXTRACT_JS)             # 深度截断
        self.assertIn("aria-hidden", _DOM_EXTRACT_JS)          # 不可见剪枝
        self.assertIn("display === 'none'", _DOM_EXTRACT_JS)
        self.assertNotIn(r"\.value", _DOM_EXTRACT_JS)       # 一律不读 input value

    def test_stable_js_uses_mutation_observer(self):
        self.assertIn("MutationObserver", _DOM_STABLE_JS)
        self.assertIn("quietCount", _DOM_STABLE_JS)


class ToolRegistrationTest(unittest.TestCase):
    """新工具已注册且带 docstring（LangGraph schema 需要）。"""

    def test_tools_in_registry(self):
        self.assertIn(browser_get_dom, TOOLS)
        self.assertIn(browser_wait_for_dom_stable, TOOLS)

    def test_tools_have_descriptions(self):
        for t in (browser_get_dom, browser_wait_for_dom_stable):
            self.assertTrue(t.description and len(t.description) > 20, t.name)
            self.assertIn("max_depth" if t is browser_get_dom else "quiet_ms",
                          t.description)


class NavigateDomStableFallbackTest(unittest.TestCase):
    """browser_navigate 的 networkidle 兜底路径：_wait_dom_stable 辅助函数。

    用假 page 对象测辅助函数本身（不启动浏览器）；端到端接线由真实浏览器
    验证脚本覆盖（见提交说明）。
    """

    class _FakePage:
        def __init__(self, result=None, raises=None):
            self._result = result
            self._raises = raises
            self.eval_arg = None

        async def evaluate(self, js, arg):
            self.eval_arg = arg
            if self._raises:
                raise self._raises
            return self._result

    def test_nav_params_are_bounded(self):
        # 兜底等待必须在 navigate 的 90s 总预算内，且静默期合理
        p = bt._NAV_DOM_STABLE
        self.assertEqual(p["timeout"], 5000)
        self.assertEqual(p["quiet_ms"], 500)
        self.assertEqual(p["quiet_intervals"], 3)

    def test_returns_true_when_stable(self):
        page = self._FakePage({"stable": True, "elapsedMs": 1500, "mutations": 7})
        self.assertTrue(asyncio.run(_wait_dom_stable(page)))
        # 必须用导航兜底参数调用，而不是工具默认参数
        self.assertEqual(page.eval_arg, bt._NAV_DOM_STABLE)

    def test_returns_false_when_timeout(self):
        page = self._FakePage({"stable": False, "elapsedMs": 5000, "mutations": 50})
        self.assertFalse(asyncio.run(_wait_dom_stable(page)))

    def test_swallows_evaluate_error(self):
        # 导航中上下文被销毁等异常不应阻断导航
        page = self._FakePage(raises=RuntimeError("Execution context was destroyed"))
        self.assertFalse(asyncio.run(_wait_dom_stable(page)))

    def test_empty_result_is_unstable(self):
        page = self._FakePage(None)
        self.assertFalse(asyncio.run(_wait_dom_stable(page)))


if __name__ == "__main__":
    unittest.main()
