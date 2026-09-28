"""插件机制测试：发现 / 加载 / 故障隔离 / 工具收集 / 事件钩子 / 热重载。

覆盖 plugin_loader 的核心契约，特别是"单个插件失败绝不影响其他插件与主流程"
这条降级保证（对应 deepseek-harness 的故障隔离设计）。
"""
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent_core"))

from plugin_loader import PluginRegistry, reset_registry  # noqa: E402


def _write_plugin(base, name, body):
    """在 base 目录写一个单文件插件，返回其路径。"""
    p = base / f"{name}.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


GOOD_PLUGIN = '''
    INFO = {"id": "good", "name": "好插件", "description": "d", "version": "1.0"}
    from langchain_core.tools import tool

    @tool
    def good_tool(text: str) -> str:
        """好插件的工具"""
        return text

    TOOLS = [good_tool]
'''

BROKEN_PLUGIN = '''
    raise RuntimeError("插件初始化爆炸")
'''

HOOK_PLUGIN = '''
    INFO = {"id": "hooked", "name": "带钩子", "version": "1.0"}
    SEEN = []

    def _on_message(payload):
        SEEN.append(("message", payload))

    def _on_tool_end(payload):
        SEEN.append(("tool_end", payload))

    HOOKS = {"on_message": _on_message, "on_tool_end": _on_tool_end}
'''

BAD_HOOK_PLUGIN = '''
    INFO = {"id": "badhook", "name": "钩子会抛异常", "version": "1.0"}

    def _boom(payload):
        raise RuntimeError("监听器炸了")

    HOOKS = {"on_message": _boom}
'''


@pytest.fixture
def plugin_dir(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    return d


def _registry(plugin_dir, enabled):
    # include_builtin=False：只扫临时目录，避免内置 example_plugin 混入断言
    return PluginRegistry(enabled_plugins=enabled, plugin_dirs=[plugin_dir],
                          include_builtin=False)


# ── 发现 ──
def test_discover_finds_single_file_plugin(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, [])
    ids = sorted(r.id for r in reg.discover())
    assert ids == ["good"]


def test_discover_skips_underscore_and_non_py(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    (plugin_dir / "_private.py").write_text("x = 1", encoding="utf-8")
    (plugin_dir / "notes.txt").write_text("hi", encoding="utf-8")
    reg = _registry(plugin_dir, [])
    assert [r.id for r in reg.discover()] == ["good"]


def test_discover_marks_enabled_from_config(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, ["good"])
    rec = reg.discover()[0]
    assert rec.enabled is True


# ── 加载与工具收集 ──
def test_disabled_plugin_is_not_loaded(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, [])
    reg.discover()
    reg.load_all()
    assert reg.collect_tools() == []
    assert reg.plugins["good"].status == "pending"


def test_enabled_plugin_contributes_tools(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, ["good"])
    reg.discover()
    reg.load_all()
    tools = reg.collect_tools()
    assert [getattr(t, "name", None) for t in tools] == ["good_tool"]
    assert reg.plugins["good"].status == "loaded"


def test_plugin_metadata_from_info(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, ["good"])
    reg.discover()
    reg.load_all()
    d = reg.plugins["good"].to_dict()
    assert d["name"] == "好插件"
    assert d["version"] == "1.0"
    assert d["tool_names"] == ["good_tool"]


# ── 故障隔离（核心保证）──
def test_broken_plugin_does_not_block_others(plugin_dir):
    _write_plugin(plugin_dir, "aaa_broken", BROKEN_PLUGIN)
    _write_plugin(plugin_dir, "zzz_good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, ["aaa_broken", "zzz_good"])
    reg.discover()
    reg.load_all()
    # 坏插件被隔离标记 error，好插件照常加载并贡献工具
    assert reg.plugins["aaa_broken"].status == "error"
    assert "爆炸" in reg.plugins["aaa_broken"].error
    assert reg.plugins["zzz_good"].status == "loaded"
    assert len(reg.collect_tools()) == 1


def test_broken_plugin_tools_not_injected(plugin_dir):
    _write_plugin(plugin_dir, "aaa_broken", BROKEN_PLUGIN)
    reg = _registry(plugin_dir, ["aaa_broken"])
    reg.discover()
    reg.load_all()
    assert reg.collect_tools() == []
    assert reg.plugins["aaa_broken"].injectable is False


# ── 事件钩子 ──
def test_hooks_receive_events(plugin_dir):
    _write_plugin(plugin_dir, "hooked", HOOK_PLUGIN)
    reg = _registry(plugin_dir, ["hooked"])
    reg.discover()
    reg.load_all()
    assert reg.emit("on_message", {"text": "hi"}) == 1
    assert reg.emit("on_tool_end", {"name": "run_shell"}) == 1
    seen = reg.plugins["hooked"].module.SEEN
    assert seen == [("message", {"text": "hi"}), ("tool_end", {"name": "run_shell"})]


def test_emit_ignores_unknown_event(plugin_dir):
    _write_plugin(plugin_dir, "hooked", HOOK_PLUGIN)
    reg = _registry(plugin_dir, ["hooked"])
    reg.discover()
    reg.load_all()
    assert reg.emit("no_such_event", {}) == 0


def test_failing_hook_listener_is_isolated(plugin_dir):
    """监听器抛异常不得向上抛，也不得影响其他监听器。"""
    _write_plugin(plugin_dir, "aaa_badhook", BAD_HOOK_PLUGIN)
    _write_plugin(plugin_dir, "zzz_hooked", HOOK_PLUGIN)
    reg = _registry(plugin_dir, ["aaa_badhook", "zzz_hooked"])
    reg.discover()
    reg.load_all()
    # 不抛异常；两个监听器都被尝试（坏的记 warning 后继续）
    assert reg.emit("on_message", {"text": "x"}) == 1
    assert reg.plugins["zzz_hooked"].module.SEEN == [("message", {"text": "x"})]


def test_disabled_plugin_hooks_not_called(plugin_dir):
    _write_plugin(plugin_dir, "hooked", HOOK_PLUGIN)
    reg = _registry(plugin_dir, [])
    reg.discover()
    reg.load_all()
    assert reg.emit("on_message", {"text": "x"}) == 0


# ── 启用 / 禁用 / 热重载 ──
def test_enable_at_runtime_loads_plugin(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, [])
    reg.discover()
    assert reg.collect_tools() == []
    reg.enable("good")
    assert len(reg.collect_tools()) == 1


def test_disable_unloads_and_drops_tools(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, ["good"])
    reg.discover()
    reg.load_all()
    assert len(reg.collect_tools()) == 1
    reg.disable("good")
    assert reg.collect_tools() == []
    assert reg.plugins["good"].status == "disabled"


def test_reload_applies_new_enabled_set(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    _write_plugin(plugin_dir, "hooked", HOOK_PLUGIN)
    reg = _registry(plugin_dir, ["good"])
    reg.discover()
    reg.load_all()
    assert len(reg.collect_tools()) == 1
    # 热重载：换成只启用 hooked
    reg.reload(enabled_plugins=["hooked"])
    assert reg.collect_tools() == []
    assert reg.plugins["hooked"].status == "loaded"
    # reload 会重新 discover，good 成为未启用的新记录（pending），其工具不再注入
    assert reg.plugins["good"].status == "pending"
    assert reg.plugins["good"].enabled is False


def test_reload_clears_stale_external_tools(plugin_dir):
    """reload 后 host.register_tools 追加的工具不得残留（否则禁用插件仍生效）。"""
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, ["good"])
    reg.discover()
    reg.load_all()
    reg.register_external_tools("good", ["fake"])
    assert "fake" in reg.collect_tools()
    reg.reload(enabled_plugins=[])
    assert reg.collect_tools() == []


# ── 单例 ──
def test_registry_singleton_and_reset(plugin_dir):
    reset_registry()
    from plugin_loader import get_registry
    a = get_registry(enabled_plugins=[], plugin_dirs=[plugin_dir])
    b = get_registry()
    assert a is b
    reset_registry()
    assert get_registry() is not a


def test_get_registry_reloads_when_enabled_set_changes(plugin_dir):
    """保存设置后 init_agent(force=True) 会用新 enabled_plugins 再调 get_registry。

    单例若静默忽略新参数，设置页的插件勾选就永远不生效（2026-09-28 实测复现：
    启动 enabled=[p1]，改配置为 [p2] 后 collect_tools 仍是 p1）。
    """
    _write_plugin(plugin_dir, "p1", GOOD_PLUGIN)
    _write_plugin(plugin_dir, "p2", GOOD_PLUGIN.replace('"good"', '"p2"')
                  .replace("good_tool", "p2_tool").replace("好插件", "P2"))
    reset_registry()
    from plugin_loader import get_registry
    reg = get_registry(enabled_plugins=["p1"], plugin_dirs=[plugin_dir])
    reg.load_all()
    assert [getattr(t, "name", None) for t in reg.collect_tools()] == ["good_tool"]
    # 同一进程内改用新启用集合（模拟保存设置后重建 Agent）
    reg2 = get_registry(enabled_plugins=["p2"], plugin_dirs=[plugin_dir])
    reg2.load_all()
    assert reg2 is reg
    assert [getattr(t, "name", None) for t in reg2.collect_tools()] == ["p2_tool"]
    # 全关：工具必须被清空，不能残留上一个启用集合的工具
    reg3 = get_registry(enabled_plugins=[], plugin_dirs=[plugin_dir])
    reg3.load_all()
    assert reg3.collect_tools() == []
    reset_registry()


def test_get_registry_no_reload_when_params_unchanged(plugin_dir):
    """参数不变时不得触发热重载（避免每次 init_agent 都重放 on_load）。"""
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reset_registry()
    from plugin_loader import get_registry
    reg = get_registry(enabled_plugins=["good"], plugin_dirs=[plugin_dir])
    reg.load_all()
    module_before = reg.plugins["good"].module
    reg2 = get_registry(enabled_plugins=["good"], plugin_dirs=[plugin_dir])
    reg2.load_all()
    assert reg2.plugins["good"].module is module_before
    reset_registry()
# ── FRONTEND 注入（A/C/F/E）──
def test_collect_frontend_ignores_plugin_without_frontend(plugin_dir):
    _write_plugin(plugin_dir, "good", GOOD_PLUGIN)
    reg = _registry(plugin_dir, ["good"])
    reg.discover()
    reg.load_all()
    assert reg.collect_frontend() == []


def test_collect_frontend_gathers_manifest(plugin_dir):
    p = '''
        INFO = {"id": "fe", "name": "前端插件", "version": "1.0"}
        FRONTEND = {
            "css": ["a{}", "b{}"],
            "js": ["window.x=1;"],
            "sidebar": {"title": "我的侧栏", "html": "<b>hi</b>"},
            "settings_tabs": [{"key": "k", "title": "T"}],
            "events": ["my.event"],
        }
    '''
    _write_plugin(plugin_dir, "fe", p)
    reg = _registry(plugin_dir, ["fe"])
    reg.discover()
    reg.load_all()
    fe = reg.collect_frontend()
    assert len(fe) == 1
    f = fe[0]
    assert f["id"] == "fe"
    assert len(f["frontend"]["css"]) == 2
    assert f["frontend"]["js"] == ["window.x=1;"]
    assert f["frontend"]["sidebar"]["title"] == "我的侧栏"
    assert f["frontend"]["settings_tabs"][0]["key"] == "k"
    assert f["frontend"]["events"] == ["my.event"]


def test_collect_frontend_normalizes_frontend_type(plugin_dir):
    """FRONTEND 非 dict 时忽略并记 warning，不崩。"""
    _write_plugin(plugin_dir, "bad_fe", 'INFO={"id":"bad_fe"}\nFRONTEND=42\n')
    reg = _registry(plugin_dir, ["bad_fe"])
    reg.discover()
    reg.load_all()
    assert reg.collect_frontend() == []


def test_disabled_plugin_frontend_not_collected(plugin_dir):
    p = 'INFO={"id":"fe"}\nFRONTEND={"css":["a{}"]}\n'
    _write_plugin(plugin_dir, "fe", p)
    reg = _registry(plugin_dir, [])
    reg.discover()
    reg.load_all()
    assert reg.collect_frontend() == []


# ── SSE 推送通道（E 注入点）──
def test_queue_and_drain_sse(plugin_dir):
    reg = _registry(plugin_dir, [])
    reg.queue_sse("ev1", {"a": 1})
    reg.queue_sse("ev2", {})
    assert reg.drain_sse() == [("ev1", {"a": 1}), ("ev2", {})]
    # 清空后再取为空
    assert reg.drain_sse() == []


def test_push_sse_updates_queue_then_drains(plugin_dir):
    """插件 on_tool_end 里 host.push_sse → 队列累积 → drain_sse 取走，供 agent_run yield。"""
    p = '''
        INFO = {"id": "pusher", "name": "p"}
        _host = None
        def _on_tool_end(payload):
            _host.push_sse("my.event", {"tool": payload.get("name", "")})
        HOOKS = {"on_tool_end": _on_tool_end}
        def on_load(host):
            global _host
            _host = host
    '''
    _write_plugin(plugin_dir, "pusher", p)
    reg = _registry(plugin_dir, ["pusher"])
    reg.discover()
    reg.load_all()
    # 通过 emit 广播 on_tool_end → 插件 push_sse
    reg.emit("on_tool_end", {"name": "run_shell"})
    queued = reg.drain_sse()
    assert queued == [("my.event", {"tool": "run_shell"})]
    assert reg.drain_sse() == []


def test_broken_push_sse_isolated(plugin_dir):
    """插件 push_sse 抛异常不影响主流程。"""
    p = '''
        INFO = {"id": "badpush"}
        def _on_tool_end(payload):
            raise RuntimeError("boom")
        HOOKS = {"on_tool_end": _on_tool_end}
    '''
    _write_plugin(plugin_dir, "badpush", p)
    reg = _registry(plugin_dir, ["badpush"])
    reg.discover()
    reg.load_all()
    # 不抛异常，只是队列为空
    reg.emit("on_tool_end", {"name": "x"})
    assert reg.drain_sse() == []