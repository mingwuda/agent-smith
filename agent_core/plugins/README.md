# 内置插件目录

每个子目录（带 `__init__.py`）或单个 `.py` 文件就是一个插件。
插件在设置页按 id 启用；未启用的插件不会被加载，也不贡献工具。

自定义插件目录：设置项 `plugin_dirs`（`os.pathsep` 分隔，可叠加多个），
与内置目录按 id 去重，自定义目录优先。

## 插件契约

详见 `agent_core/plugin_loader.py` 顶部 docstring。全部导出均为可选：

```python
INFO     = {"id": "my_plugin", "name": "...", "description": "...", "version": "1.0"}
TOOLS    = [tool1, tool2]                       # 贡献给 agent 的工具（langchain @tool）
HOOKS    = {"on_message": fn, "on_tool_end": fn}  # 订阅宿主事件
FRONTEND = {...}                                # 前端注入清单（见下）
on_load(host) / on_unload(host)                 # 生命周期
```

## 后端注入点

| 注入点 | 机制 | 触发时机 |
|---|---|---|
| 工具 | `TOOLS` 或 `host.register_tools([...])` | Agent 初始化时并入工具集 |
| `on_message` | `HOOKS["on_message"]` | 每条用户消息进入 agent 前 |
| `on_tool_end` | `HOOKS["on_tool_end"]` | 每次工具调用结束后 |

## 前端注入点（FRONTEND）

后端 `collect_frontend()` 聚合 → `GET /system/plugin-frontend` → 前端
`desktop/js/core/plugin-ui.js` 拉取并注册。字段全部可选：

| 键 | 类型 | 注入点 |
|---|---|---|
| `css` | `str` 或 `[str]` | **F** 裸 CSS 全局注入 |
| `js` | `str` 或 `[str]` | **F** 裸 JS 全局注入 |
| `sidebar` | `{"title": str, "html": str}` | **A** 侧边栏底部手风琴区块 |
| `settings_tabs` | `[{"key","title","icon"?,"html"?}]` | **C** 设置弹窗自定义 Tab |
| `events` | `[str]` | **E** 本插件将推送的自定义 SSE 事件名 |

### 前端可用的全局 API（`window.PluginUI`）

插件注入的 JS 里可直接使用：

- `PluginUI.onEvent(eventName, cb)` — 注册自定义 SSE 事件回调（E）
- `PluginUI.loadState(id)` / `PluginUI.saveState(id, value)` — 插件设置持久化
  （落盘 `~/.desktop_agent/plugin_state_<id>.json`，经 `GET/POST /system/plugin-state/{id}`）
- `PluginUI.unregister(id)` / `PluginUI.sync([id,...])` — 移除/同步已注入的 DOM 落点

### 设置 Tab 的渲染约定

`plugin-ui.js` 在 Tab 被点击时调用 `window['__plugin_render_<plugin_id>']`（若存在），
把面板容器交给插件填充。完整示例见 `example_plugin.py` 的 `FRONTEND["js"]`。

### 后端推事件到前端（E）

```python
def _on_tool_end(payload):
    _host.push_sse("my.event", {"tool": payload.get("name", "")})
```

`push_sse` 只入队；`agent_run` 在 `on_message` / `on_tool_end` 广播后 `drain_sse()`
并 yield 成 `{type: "plugin_event", event, payload}` SSE 帧，前端
`handleStreamEvent` 的 default 分支按 event 名 dispatch 给 `PluginUI.onEvent` 的回调。

## 故障隔离

单个插件 import 失败 / `on_load` 抛异常 / 事件监听器抛异常 / `push_sse` 抛异常
→ 只记 warning 并标记该插件 `error`，**绝不影响其他插件与主流程**。
设置页会显示「加载失败 + 原因」。

## 已知上限

- 取消勾选插件后，`PluginUI.sync` 会移除其侧边栏区块与设置 Tab，但**已执行的 JS
  副作用与已注册的事件监听无法卸载**，需刷新页面才彻底清除。
- 前端注入点仅对 admin 生效（后端 `/system/plugin-frontend` 走 `_require_admin`）。

## 自检

```bash
node tests/check_plugin_ui.js     # 前端注入逻辑（isAdmin 时序 / 反注册）
python3 -m pytest tests/test_plugin_loader.py -q   # 后端加载器
```
