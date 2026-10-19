"""双浏览器后端选择与回退逻辑测试（Obscura 优先 + Chromium 回退）。

这些测试不启动真实浏览器，全部用 mock stub 验证 _init_browser_backend 的
决策逻辑：Obscura 成功/失败回退/禁用回退抛错/未配置 Obscura、能力探测与
URL 解析。真实浏览器链路由 POC 端到端脚本覆盖（见提交说明）。
"""
import asyncio
import unittest
from unittest import mock

import agent_core.tools.browser_tools as bt


def _reset():
    """清空模块级后端状态与全局 playwright stub"""
    bt._BROWSER_BACKEND = None
    bt._BACKEND_CAPS = {"file": False, "iframe": False, "contenteditable": False, "cjk": False}
    bt._browser = None
    bt._playwright = None
    bt._TOOL_ORIG_DESC = {}
    # 默认允许回退
    bt._BACKEND_TRY_CHROMIUM = True


class FakeChromium:
    """stub：chromium.launch / connect_over_cdp 返回可 await 的结果"""
    def __init__(self, launch=None, cdp=None):
        self._launch = launch or (lambda **k: FakeBrowser())
        self._cdp = cdp or (lambda url: FakeBrowser())
        self.launch_calls = 0
        self.cdp_calls = 0
    async def launch(self, **kwargs):
        self.launch_calls += 1
        return self._launch(**kwargs)
    async def connect_over_cdp(self, url):
        self.cdp_calls += 1
        return self._cdp(url)


class FakePlaywright:
    """stub：chromium.launch / connect_over_cdp 覆盖两类调用"""
    def __init__(self, launch=None, cdp=None):
        self.chromium = FakeChromium(
            launch=(lambda **k: FakeBrowser()) if launch is None else launch,
            cdp=(lambda url: (_ for _ in ()).throw(ConnectionError("no obscura"))) if cdp is None else cdp,
        )


class FakeBrowser:
    """stub：可被 _probe_backend_caps 使用的假浏览器"""
    def __init__(self, caps=None):
        self._caps = caps if caps is not None else {
            "file": True, "iframe": True, "contenteditable": True, "cjk": True}
        self._closed = False

    async def new_context(self, viewport=None):
        return FakeCtx(self._caps, viewport)

    async def close(self):
        self._closed = True


class FakeCtx:
    def __init__(self, caps, viewport=None):
        self._caps = caps
    async def new_page(self):
        return FakePage(self._caps)
    async def close(self):
        pass


class FakePage:
    def __init__(self, caps):
        self._caps = caps
    async def goto(self, *a, **k):
        return None
    async def set_content(self, *a, **k):
        return None
    async def wait_for_timeout(self, *a, **k):
        return None
    async def screenshot(self, *a, **k):
        return b"fake-png-bytes"
    async def evaluate(self, js):
        # 模拟 _PROBE_CAPS_JS 探测返回
        return {
            "file": self._caps["file"],
            "iframe": self._caps["iframe"],
            "contenteditable": self._caps["contenteditable"],
        }


class FakePersistentCtx:
    """stub：launch_persistent_context 的返回值（一个 BrowserContext）"""
    def __init__(self, caps=None):
        self._browser = FakeBrowser(caps)
        self._pages = []
    @property
    def browser(self):
        return self._browser
    async def new_page(self):
        p = FakePage(self._browser._caps)
        self._pages.append(p)
        return p
    async def close(self):
        pass


class PersistableBackendTest(unittest.TestCase):
    """MOSS_PERSISTABLE_BROWSER=1 → 优先 Chromium persistent context"""

    def setUp(self):
        _reset()

    def test_persistable_uses_launch_persistent_context(self):
        """开启持久化 → 走 launch_persistent_context，不用 connect_cdp/new_context 隔离"""
        launch_calls = []

        async def run():
            fake_pw = FakePlaywright(launch=lambda **k: FakeBrowser())
            # 覆盖 chromium.launch_persistent_context 为假持久上下文
            fake_pc = FakePersistentCtx()
            fake_pw.chromium.launch_persistent_context = (
                lambda user_dir, **k: _await_later(fake_pc)
            )
            with mock.patch.object(bt, "persistable", True):
                await _call_init(fake_pw, "http://127.0.0.1:9222")
            self.assertIsNotNone(bt._persistent_ctx)
            self.assertIs(bt._persistent_ctx, fake_pc)
            self.assertEqual(bt._BROWSER_BACKEND, "chromium")
            # 持久化模式即使 Obscura 可用也不用 Obscura（可靠性优先）
            self.assertEqual(fake_pw.chromium.cdp_calls, 0)
        asyncio.run(run())

    def test_persistable_falls_back_to_obscura_on_launch_failure(self):
        """持久上下文启动失败 → 回退常规后端（Obscura/Chromium），不抛错"""
        async def run():
            async def boom(user_dir, **k):
                raise RuntimeError("profile locked")
            fake_pw = FakePlaywright(
                cdp=lambda url: FakeBrowser(caps={
                    "file": False, "iframe": False, "contenteditable": False}),
                launch=lambda **k: FakeBrowser())
            fake_pw.chromium.launch_persistent_context = boom
            with mock.patch.object(bt, "persistable", True):
                await _call_init(fake_pw, "http://127.0.0.1:9222")
            self.assertIsNone(bt._persistent_ctx)
            self.assertEqual(bt._BROWSER_BACKEND, "obscura")
        asyncio.run(run())

    def test_non_persistable_ignores_persistent_path(self):
        """关闭持久化 → 不调用 launch_persistent_context"""
        launch_calls = []
        async def run():
            fake_pw = FakePlaywright(launch=lambda **k: FakeBrowser())
            fake_pw.chromium.launch_persistent_context = (
                lambda *a, **k: launch_calls.append(1) or _await_later(FakePersistentCtx())
            )
            with mock.patch.object(bt, "persistable", False):
                await _call_init(fake_pw, "http://127.0.0.1:9222")
            self.assertEqual(launch_calls, [])
            self.assertIsNone(bt._persistent_ctx)
        asyncio.run(run())

    def test_ensure_browser_reuses_persistent_ctx(self):
        """持久化模式 → _ensure_browser 在共享持久上下文开页，不 new_context"""
        async def run():
            fake_pw = FakePlaywright(launch=lambda **k: FakeBrowser())
            fake_pc = FakePersistentCtx()
            fake_pw.chromium.launch_persistent_context = (
                lambda user_dir, **k: _await_later(fake_pc))
            with mock.patch.object(bt, "persistable", True):
                await _call_init(fake_pw, "http://127.0.0.1:9222")
                page = await bt._ensure_browser("sess-1")
                self.assertIsNotNone(page)
                # 持久上下文只创建了一个页面，且 _contexts 指向持久 ctx
                self.assertEqual(len(fake_pc._pages), 1)
                self.assertIs(bt._contexts["sess-1"], fake_pc)
            await bt._close_browser()
            self.assertIsNone(bt._persistent_ctx)
        asyncio.run(run())


async def _await_later(v):
    """把同步值包装成可 await 的协程（模拟 async 返回）"""
    return v


class ObscuraUrlTest(unittest.TestCase):
    @mock.patch.dict("os.environ", {}, clear=False)
    def setUp(self):
        pass

    def test_unset_returns_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            bt._obscura_cdp_url.__wrapped__ if hasattr(bt._obscura_cdp_url, "__wrapped__") else None
            self.assertIsNone(bt._obscura_cdp_url())

    def test_set_returns_url(self):
        with mock.patch.dict("os.environ", {"OBSCURA_CDP_URL": " http://x:9222  "}, clear=True):
            self.assertEqual(bt._obscura_cdp_url(), "http://x:9222")


class BackendSelectionTest(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_obscura_preferred_when_available(self):
        """配置了 Obscura 且可连 → 用 Obscura，能力探测执行"""
        async def run():
            fake_pw = FakePlaywright(cdp=lambda url: FakeBrowser(caps={
                "file": False, "iframe": False, "contenteditable": False}))
            await _call_init(fake_pw, "http://127.0.0.1:9222")
            self.assertEqual(bt._BROWSER_BACKEND, "obscura")
            self.assertEqual(fake_pw.chromium.cdp_calls, 1)
            self.assertEqual(fake_pw.chromium.launch_calls, 0)
            # Obscura 的缺能力被记录
            self.assertFalse(bt._BACKEND_CAPS["file"])
        asyncio.run(run())

    def test_falls_back_to_chromium_when_obscura_unreachable(self):
        """Obscura 连不上 → 自动回退 Chromium，连接错误被吞、不抛"""
        async def run():
            fake_pw = FakePlaywright(
                cdp=lambda url: (_ for _ in ()).throw(ConnectionError("refused")),
                launch=lambda **k: FakeBrowser())
            await _call_init(fake_pw, "http://127.0.0.1:9222")
            self.assertEqual(bt._BROWSER_BACKEND, "chromium")
            self.assertEqual(fake_pw.chromium.cdp_calls, 1)
            self.assertEqual(fake_pw.chromium.launch_calls, 1)
        asyncio.run(run())

    def test_raises_when_no_obscura_and_fallback_disabled(self):
        """Obscura 失败且 OBSCURA_DISABLE_FALLBACK=1 → 抛 RuntimeError"""
        async def run():
            bt._BACKEND_TRY_CHROMIUM = False
            fake_pw = FakePlaywright(
                cdp=lambda url: (_ for _ in ()).throw(ConnectionError("refused")),
                launch=lambda **k: FakeBrowser())
            with self.assertRaises(RuntimeError):
                await _call_init(fake_pw, "http://127.0.0.1:9222")
        asyncio.run(run())

    def test_chromium_direct_when_no_obscura_configured(self):
        """未配置 OBSCURA_CDP_URL → 直接用 Chromium（不尝试 CDP）"""
        async def run():
            fake_pw = FakePlaywright(launch=lambda **k: FakeBrowser())
            await _call_init(fake_pw, None)
            self.assertEqual(bt._BROWSER_BACKEND, "chromium")
            self.assertEqual(fake_pw.chromium.cdp_calls, 0)
            self.assertEqual(fake_pw.chromium.launch_calls, 1)
        asyncio.run(run())

    def test_chromium_caps_detected(self):
        """Chromium 后端能力探测 → file/iframe/contenteditable 全 true"""
        async def run():
            fake_pw = FakePlaywright(launch=lambda **k: FakeBrowser(caps={
                "file": True, "iframe": True, "contenteditable": True}))
            await _call_init(fake_pw, None)
            self.assertTrue(bt._BACKEND_CAPS["file"])
            self.assertTrue(bt._BACKEND_CAPS["iframe"])
            self.assertTrue(bt._BACKEND_CAPS["contenteditable"])
        asyncio.run(run())


async def _call_init(fake_pw, cdp_url):
    """在受控环境调用 _init_browser_backend：注入 fake_pw 为全局 _playwright。"""
    import agent_core.tools.browser_tools as bt
    # 直接 monkeypatch 模块级 _playwright 全局，绕开 globals().get 启动真实 playwright
    bt._playwright = fake_pw
    # 用假的 _cjk_has_glyphs 替代实际 Pillow 截图解析（测试不跑真实浏览器）
    def _fake_cjk(browser):
        return bool(browser._caps.get("cjk", True))
    with mock.patch.object(bt, "_cjk_has_glyphs", side_effect=_fake_cjk):
        with mock.patch.object(bt, "_obscura_cdp_url", return_value=cdp_url):
            return await bt._init_browser_backend()


class CapsProbeTest(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_probe_maps_browser_caps(self):
        async def run():
            caps = await bt._probe_backend_caps(FakeBrowser(caps={
                "file": False, "iframe": True, "contenteditable": False}))
            self.assertFalse(caps["file"])
            self.assertTrue(caps["iframe"])
            self.assertFalse(caps["contenteditable"])
        asyncio.run(run())

    def test_probe_defaults_false_on_error(self):
        """探测抛错 → 保守全 False 不抛"""
        class BoomBrowser:
            async def new_context(self):
                raise RuntimeError("boom")
        async def run():
            caps = await bt._probe_backend_caps(BoomBrowser())
            self.assertFalse(any(caps.values()))
        asyncio.run(run())


class CapsNoteTest(unittest.TestCase):
    """后端能力提示文案生成与工具描述注入。"""

    def setUp(self):
        _reset()

    def test_chromium_full_caps_no_note(self):
        bt._BROWSER_BACKEND = "chromium"
        bt._BACKEND_CAPS = {"file": True, "iframe": True, "contenteditable": True, "cjk": True}
        self.assertEqual(bt._backend_caps_note(), "")

    def test_obscura_missing_caps_generates_note(self):
        bt._BROWSER_BACKEND = "obscura"
        bt._BACKEND_CAPS = {"file": False, "iframe": True, "contenteditable": False, "cjk": True}
        note = bt._backend_caps_note()
        self.assertIn("文件上传", note)
        self.assertIn("富文本", note)
        self.assertNotIn("iframe", note)  # iframe=True 不提示

    def test_all_missing_includes_iframe(self):
        bt._BROWSER_BACKEND = "obscura"
        bt._BACKEND_CAPS = {"file": False, "iframe": False, "contenteditable": False, "cjk": False}
        note = bt._backend_caps_note()
        self.assertIn("iframe", note)
        self.assertIn("中文", note)  # cjk=False 也应提示

    def test_cjk_missing_adds_chinese_note(self):
        """cjk 缺位 → 能力提示包含中文豆腐块说明"""
        bt._BROWSER_BACKEND = "obscura"
        bt._BACKEND_CAPS = {"file": True, "iframe": True, "contenteditable": True, "cjk": False}
        note = bt._backend_caps_note()
        self.assertIn("中文", note)
        self.assertIn("豆腐块", note)

    def test_refresh_injects_into_tool_descriptions(self):
        bt._BROWSER_BACKEND = "obscura"
        bt._BACKEND_CAPS = {"file": False, "iframe": False, "contenteditable": False, "cjk": False}
        # 记录原描述，注入后应包含限制提示
        bt._refresh_browser_tool_notes()
        self.assertIn("当前浏览器后端能力限制", bt.browser_get_dom.description)
        self.assertIn("当前浏览器后端能力限制", bt.browser_fill.description)
        # 幂等：再次调用不重复追加
        bt._refresh_browser_tool_notes()
        self.assertEqual(bt.browser_get_dom.description.count("当前浏览器后端能力限制"), 1)

    def test_refresh_noop_when_chromium_full(self):
        bt._BROWSER_BACKEND = "chromium"
        bt._BACKEND_CAPS = {"file": True, "iframe": True, "contenteditable": True, "cjk": True}
        bt._refresh_browser_tool_notes()
        self.assertNotIn("当前浏览器后端能力限制", bt.browser_get_dom.description)


if __name__ == "__main__":
    unittest.main()