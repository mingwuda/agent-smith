"""示例插件：演示插件机制的全部 5 种贡献方式（工具 / 事件钩子 / 生命周期 / 前端注入）。

这是一个**默认不启用**的教学模板。要真正启用它，在设置页把 `example_plugin`
加入启用列表（或配置 AGENT_ENABLED_PLUGINS=example_plugin）。

FRONTEND 注入（F / A / C / E 四个前端注入点）：
  1. css  —— 裸 CSS 全局注入（F）
  2. js   —— 裸 JS 全局注入（F），负责注册设置 Tab 渲染 + 侧边栏事件刷新
  3. sidebar —— 侧边栏底部手风琴区块（A）
  4. settings_tabs —— 设置弹窗自定义 Tab（C）
  5. events —— 自定义 SSE 事件名（E），配合 on_tool_end 事件里 host.push_sse 推送
"""
from langchain_core.tools import tool

INFO = {
    "id": "example_plugin",
    "name": "示例插件",
    "description": "演示插件机制：工具 / 事件钩子 / 生命周期 / 前端注入（CSS·JS·侧边栏·设置Tab·SSE）",
    "version": "1.0.0",
}

# 事件计数（仅用于演示 HOOKS 被调用；真实插件可做审计、统计、外部通知等）
_stats = {"messages": 0, "tool_ends": 0}


@tool
def echo_plugin(text: str) -> str:
    """回显输入文本，用于验证插件贡献的工具已被 agent 加载。

    Args:
        text: 要回显的任意文本
    """
    return f"[example_plugin] echo: {text}"


TOOLS = [echo_plugin]


# ─────────────────────────────────────────
# F / A / C / E 前端注入
# ─────────────────────────────────────────
FRONTEND = {
    # F：裸 CSS 全局注入
    "css": [
        # 给本插件相关的 UI 元素上个浅色底，避免干扰
        "#plugin-accordion-example_plugin .plugin-box, "
        "#panel-plugin-example_plugin-hello .plugin-box { "
        "   border:1px dashed #007aff; border-radius:8px; padding:10px; "
        "   background:rgba(0,122,255,.05); margin:8px 0; font-size:13px; line-height:1.6; "
        "}",
        "#plugin-event-banner { "
        "   background:rgba(255,193,7,.12); border-left:3px solid #ffc107; "
        "   padding:6px 10px; border-radius:4px; margin-bottom:8px; font-size:12px; "
        "}",
    ],
    # F：裸 JS 全局注入。插件的「前端行为」都放这里，借助全局 PluginUI API
    # (PluginUI.onEvent / PluginUI.loadState / PluginUI.saveState)。
    "js": [
        "window.__example_plugin_state = { messages: 0, tool_ends: 0 };",
        # 侧边栏面板事件刷新：收到后端推来的自定义 SSE 事件后更新对应 DOM。
        "window.__example_plugin_refreshSidebar = function (p) {"
        "  var c = document.getElementById('plugin-sidebar-count');"
        "  if (c) c.textContent = '工具计数 = ' + (p && p.count != null ? p.count : 0);"
        "  var b = document.getElementById('plugin-event-banner');"
        "  if (b) b.textContent = '收到事件: ' + JSON.stringify(p);"
        "};",
        # 注册 SSE 自定义事件监听（E 注入点）：事件经由 handleStreamEvent default 分支派发过来。
        "if (window.PluginUI) {"
        "  PluginUI.onEvent('example.demo_event', function (p) {"
        "    window.__example_plugin_state.tool_ends = (p && p.count) || 0;"
        "    window.__example_plugin_refreshSidebar(p);"
        "  });"
        "}",
        # 设置 Tab 渲染：由 plugin-ui.js 在「设置 Tab 被点击」时调用。
        # 约定：插件把 render 函数挂到 window['__plugin_render_{id}']，plugin-ui.js 用它填充面板容器。
        "window['__plugin_render_example_plugin'] = function (containerEl) {"
        "  var s = window.__example_plugin_state || {};"
        "  var box = document.createElement('div'); box.className = 'plugin-box';"
        "  box.innerHTML = "
        "    '<div><b>示例插件设置面板</b></div>' + "
        "    '<div style=\"margin-top:6px;\">消息计数: <span>' + (s.messages||0) + '</span></div>' + "
        "    '<div>工具计数: <span>' + (s.tool_ends||0) + '</span></div>' + "
        "    '<div class=\"hint\" style=\"margin-top:8px;\">这是一个由插件注入的「设置页自定义 Tab」。</div>' + "
        "    '<button data-save-plugin type=\"button\" style=\"margin-top:8px;padding:5px 12px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;\">演示保存设置</button>';"
        "  containerEl.innerHTML = ''; containerEl.appendChild(box);"
        "  var btn = box.querySelector('[data-save-plugin]');"
        "  if (btn) btn.onclick = function () { PluginUI.saveState('example_plugin', window.__example_plugin_state||{}).then(function(){alert('已保存');}); };"
        "};",
    ],
    # A：侧边栏底部手风琴区块（结构 HTML；行为由上面注入的 JS 负责）
    "sidebar": {
        "title": "🧩 示例插件",
        "html": (
            "<div class='plugin-box'>这是一个侧边栏手风琴区块（A 注入）。"
            "<br/><span id='plugin-sidebar-count'>—</span></div>"
            "<div class='plugin-box' id='plugin-event-banner'>（等待工具事件…）</div>"
        ),
    },
    # C：设置弹窗自定义 Tab（内容由上面的 render 填充）
    "settings_tabs": [
        {"key": "hello", "title": "插件示例", "icon": "🧩",
         "html": "<div class='plugin-box'><i>（插件设置的示例面板）</i></div>"},
    ],
    # E：本插件将推送的自定义 SSE 事件名
    "events": ["example.demo_event"],
}


def _on_message(payload: dict):
    _stats["messages"] += 1


def _on_tool_end(payload: dict):
    _stats["tool_ends"] += 1
    # 每次工具调用结束后，向前端推送一个自定义 SSE 事件（E 注入点）。
    # on_load 里保存的 host 引用指向稳定单例注册表，push_sse 记入待发队列，
    # agent_run 在 on_tool_end 广播后 drain_sse 并 yield 成 SSE 帧。
    if _host is not None:
        try:
            _host.push_sse("example.demo_event", {
                "tool": (payload or {}).get("name", ""),
                "count": _stats["tool_ends"],
                "session": (payload or {}).get("session_id", ""),
            })
        except Exception:
            pass


HOOKS = {
    "on_message": _on_message,
    "on_tool_end": _on_tool_end,
}

_host = None


def on_load(host):
    """插件被启用并加载时调用。可做初始化，或通过 host.register_tools 动态追加工具。"""
    global _host
    _host = host  # 保存 host 引用，供 on_tool_end 里 push_sse 使用
    host.register_tools([])  # 演示：动态贡献点（此处为空，TOOLS 已静态声明）


def on_unload(host):
    """插件被禁用时调用。可做清理（关闭连接、 flush 缓冲等）。"""
    _stats["messages"] = 0
    _stats["tool_ends"] = 0