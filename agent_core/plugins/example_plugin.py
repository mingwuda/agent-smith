"""示例插件：演示插件机制的三种贡献方式（工具 / 事件钩子 / 生命周期）。

这是一个**默认不启用**的教学模板。要真正启用它，在设置页把 `example_plugin`
加入启用列表（或配置 AGENT_ENABLED_PLUGINS=example_plugin）。

它演示了：
1. TOOLS —— 贡献一个 agent 可调用的工具（echo_plugin）
2. HOOKS  —— 订阅 on_message / on_tool_end 事件
3. on_load / on_unload —— 生命周期钩子
"""
from langchain_core.tools import tool

INFO = {
    "id": "example_plugin",
    "name": "示例插件",
    "description": "演示插件机制：贡献工具 + 事件钩子 + 生命周期（教学模板，默认不启用）",
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


def _on_message(payload: dict):
    _stats["messages"] += 1


def _on_tool_end(payload: dict):
    _stats["tool_ends"] += 1


HOOKS = {
    "on_message": _on_message,
    "on_tool_end": _on_tool_end,
}


def on_load(host):
    """插件被启用并加载时调用。可做初始化，或通过 host.register_tools 动态追加工具。"""
    host.register_tools([])  # 演示：动态贡献点（此处为空，TOOLS 已静态声明）


def on_unload(host):
    """插件被禁用时调用。可做清理（关闭连接、 flush 缓冲等）。"""
    _stats["messages"] = 0
    _stats["tool_ends"] = 0
