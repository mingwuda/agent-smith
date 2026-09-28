"""插件加载器：为 agent 提供插件化能力扩展（参考 deepseek-harness 插件思想提炼）。

## 设计思想（提炼自 deepseek-harness 的 plugin-manager）
- **贡献点（contribution）**：插件通过标准导出符号声明能"注入"什么。
  本项目最贴合 agent 架构的贡献点是**工具（TOOLS）**，外加可选的生命周期钩子
  （on_load / on_unload）。这与 deepseek 的 "profile patch 声明贡献行" 对应，
  但面向 Python 模块而非 npm 包。
- **启用/禁用**：按插件 id 独立开关（config.plugins），关闭的不加载，不污染主流程。
- **故障隔离**：单个插件加载/初始化失败只记录 warning，绝不影响其他插件、
  也不影响主流程启动（对 deepseek "保留已完成步骤，绝不崩坏主流程" 的映射）。

## 插件契约（一个插件 = 一个 Python 模块/包）
可选导出，除 id 外均可省略：
    INFO    = {"id": "my_plugin", "name": "...", "description": "...", "version": "1.0"}
             # 必填 id；其余用于展示
    TOOLS   = [tool1, tool2, ...]          # 可选：贡献的工具（langchain tool 对象）
    on_load(self)   # 可选：插件被启用并加载时调用，可做初始化（self 是 PluginHost）
    on_unload(self) # 可选：插件被禁用时调用，可做清理

## 目录约定
- 内置插件目录：`agent_core/plugins/`（每个子目录带 __init__.py，或一个 .py 模块）
- 自定义插件目录：config.plugin_dirs（等同 deepseek 的 profile 层，可叠加多个）

## 事件钩子（对应 deepseek-harness 的 ctx.on 事件广播）
插件可导出 HOOKS 字典订阅宿主事件，宿主在对应时机调用 `emit(event, payload)`：
    HOOKS = {
        "on_message": lambda payload: ...,   # 每条用户消息进入 agent 前
        "on_tool_end": lambda payload: ...,  # 每次工具调用结束后
    }
事件名与 payload 结构见 PluginHost.emit 文档。监听器抛异常同样被隔离，
只记 warning，绝不影响主流程。

## 故障隔离
- 加载某插件时其模块 import 抛异常 → 记为 error，跳过该插件，继续加载其余。
- on_load 抛异常 → 同样隔离，插件标记 error，其 TOOLS 不注入。
- 事件监听器抛异常 → 同样隔离，只记 warning，不影响其他监听器与主流程。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _tool_name(t: Any) -> str:
    """安全取出工具名。

    langchain 的 StructuredTool 用 pydantic __getattr__ 代理属性访问，
    对不存在的属性抛 AttributeError 而非返回默认值，所以
    `getattr(t, "name", fallback)` 这种写法会直接炸，必须 try/except。
    """
    try:
        name = getattr(t, "name", None)
        if name:
            return str(name)
    except Exception:
        pass
    return getattr(t, "__name__", str(t))


class PluginRecord:
    """一个已发现插件的记录（加载结果 + 元数据 + 贡献物）。"""

    __slots__ = ("id", "name", "description", "version", "enabled", "status",
                 "error", "tools", "module", "source", "injectable")

    def __init__(self, plugin_id: str, source: str):
        self.id = plugin_id
        self.source = source                # 插件所在目录名（用于区分同名）
        self.name = plugin_id
        self.description = ""
        self.version = ""
        self.enabled = False                # config 层是否启用
        self.status = "pending"             # pending | loaded | error
        self.error = ""
        self.tools: list = []
        self.module: Any = None
        self.injectable = False             # 是否可以注入工具（loaded 且未失败）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled,
            "status": self.status,
            "error": self.error,
            "tool_count": len(self.tools),
            "tool_names": [_tool_name(t) for t in self.tools],
        }


class PluginHost:
    """传给插件 on_load / on_unload 的运行环境（对应 deepseek 的 ctx）。

    提供两类宿主能力：
    - 贡献点：register_tools() 让插件在加载期动态追加工具。
    - 事件广播：emit() 向所有已加载插件声明的 HOOKS 监听器派发事件，
      单个监听器抛异常被隔离（只记 warning），不影响其他监听器与主流程。
    """

    def __init__(self, registry: "PluginRegistry"):
        self.registry = registry

    def register_tools(self, tools: list):
        """插件在加载期向全局注册表追加工具（贡献点之一）。"""
        self.registry.register_external_tools(self.registry.current_plugin_id, tools)

    def emit(self, event: str, payload: Any = None) -> int:
        """向所有已加载插件的 HOOKS[event] 监听器广播事件。

        返回成功调用的监听器数量。监听器异常一律隔离：
        记 warning 后继续调用其余监听器，绝不向上抛。

        已知事件（宿主在对应时机调用）：
          on_message  payload={"text": str, "user_id": str, "session_id": str}
                     每条用户消息进入 agent 处理前。
          on_tool_end payload={"name": str, "args": dict, "result": str}
                     每次工具调用结束后。
        """
        called = 0
        for rec in self.registry.plugins.values():
            if not (rec.enabled and rec.injectable) or rec.module is None:
                continue
            hooks = getattr(rec.module, "HOOKS", None)
            if not isinstance(hooks, dict):
                continue
            listener = hooks.get(event)
            if not callable(listener):
                continue
            try:
                listener(payload)
                called += 1
            except Exception as e:
                logger.warning("[插件] %s 的事件 %s 监听器执行失败: %s，"
                               "已隔离，不影响主流程", rec.id, event, e)
        return called


class PluginRegistry:
    """插件注册表：发现 / 加载 / 启用禁用 / 收集工具。

    线程安全不做特殊处理：在启动装配（单线程）阶段调用，运行期不并发增删。
    """

    def __init__(self, enabled_plugins: Optional[list[str]] = None,
                 plugin_dirs: Optional[list] = None,
                 include_builtin: bool = True):
        self._plugins_dir = Path(__file__).resolve().parent / "plugins"
        self.custom_dirs = [Path(p) for p in (plugin_dirs or [])]
        # include_builtin=False 时只扫自定义目录（测试隔离用；生产恒为 True）
        self._include_builtin = include_builtin
        self.enabled = set(enabled_plugins or [])
        self.plugins: dict[str, PluginRecord] = {}
        self.external_tools: list = []       # 插件通过 host.register_tools 追加的工具
        self.current_plugin_id: str = ""
        self._plugin_modules: dict[str, Any] = {}  # id -> module（供卸载用）

    # ── 插件目录扫描 ──
    def discover(self) -> list[PluginRecord]:
        """扫描内置 + 自定义目录，识别候选插件（不加载）。返回去重后的记录。

        两种插件形态都识别：
        - 包形态：`<dir>/<name>/__init__.py`
        - 单文件形态：`<dir>/<name>.py`（同名时包形态优先）
        """
        self.plugins = {}
        seen: set[str] = set()
        dirs = list(self.custom_dirs) + ([self._plugins_dir] if self._include_builtin else [])
        for d in dirs:
            if not d.is_dir():
                continue
            for entry in sorted(d.iterdir()):
                if entry.name.startswith("_"):
                    continue
                if entry.is_dir():
                    if not (entry / "__init__.py").exists():
                        continue
                    key = entry.name
                elif entry.suffix == ".py":
                    key = entry.stem
                else:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                rec = PluginRecord(key, entry.name)
                rec.enabled = key in self.enabled
                self.plugins[key] = rec
        return list(self.plugins.values())

    def load_all(self) -> list[PluginRecord]:
        """加载所有**已启用**且**未加载**的插件，收集工具。返回加载后的记录。"""
        for rec in self.plugins.values():
            if rec.enabled and rec.status == "pending":
                self.load_one(rec.id)
        return list(self.plugins.values())

    def load_one(self, plugin_id: str) -> PluginRecord:
        """加载启用状态的单个插件。故障隔离：失败只置 error，不影响其他插件。"""
        rec = self.plugins.get(plugin_id)
        if rec is None:
            return None
        if rec.status == "loaded":
            return rec
        candidates = self._find_module(plugin_id)
        for (dir_kind, mod_path) in candidates:
            try:
                module = self._import_module(mod_path)
                info = getattr(module, "INFO", None) or {}
                pid = info.get("id") or plugin_id
                rec.id = pid
                rec.name = info.get("name") or pid
                rec.description = info.get("description") or ""
                rec.version = str(info.get("version", "") or "")
                tools = list(getattr(module, "TOOLS", None) or [])
                rec.tools = tools
                rec.module = module
                # 生命周期钩子：on_load（失败即隔离）
                host = PluginHost(self)
                self.current_plugin_id = pid
                on_load = getattr(module, "on_load", None)
                if callable(on_load):
                    on_load(host)
                self.current_plugin_id = ""
                rec.status = "loaded"
                rec.injectable = True
                rec.error = ""
                self._plugin_modules[pid] = module
                logger.info("[插件] 已加载 %s（%s） 工具=%d 来自 %s",
                            pid, dir_kind, len(tools), mod_path)
                return rec
            except Exception as e:
                logger.warning("[插件] 加载 %s 失败（来自 %s）: %s，"
                               "被隔离跳过，不影响其他插件/主流程",
                               plugin_id, mod_path, e)
                rec.status = "error"
                rec.error = str(e)
                # 继续尝试下一个候选来源（如内置目录也有同名）
                self.current_plugin_id = ""
        if rec.status != "loaded":
            rec.status = "error"
            rec.error = rec.error or "未找到可导入的插件模块"
        return rec

    # ── 工具收集 ──
    def collect_tools(self) -> list:
        """返回所有启用且加载成功插件的工具（含 host.register_tools 追加的）。"""
        tools = list(self.external_tools)
        for rec in self.plugins.values():
            if rec.enabled and rec.injectable:
                tools.extend(rec.tools)
        return tools

    # ── 启用 / 禁用（运行时切换，对应前端开关）──
    def enable(self, plugin_id: str) -> PluginRecord:
        """启用一个插件（加载失败仍标记 enabled，用于展示）"""
        rec = self.plugins.get(plugin_id)
        if rec is None:
            raise KeyError(plugin_id)
        self.enabled.add(plugin_id)
        rec.enabled = True
        if rec.status == "pending":
            self.load_one(plugin_id)
        return rec

    def disable(self, plugin_id: str) -> PluginRecord:
        """禁用一个插件；调用 on_unload 并卸载模块。"""
        rec = self.plugins.get(plugin_id)
        if rec is None:
            raise KeyError(plugin_id)
        self.enabled.discard(plugin_id)
        rec.enabled = False
        if rec.module is not None:
            on_unload = getattr(rec.module, "on_unload", None)
            if callable(on_unload):
                try:
                    on_unload(PluginHost(self))
                except Exception as e:
                    logger.warning("[插件] %s on_unload 失败: %s", plugin_id, e)
            self._plugin_modules.pop(plugin_id, None)
            rec.module = None
        rec.injectable = False
        rec.status = "disabled"
        return rec

    # ── 热重载（对应 deepseek 的 HMR：配置变更后重新发现并加载）──
    def reload(self, enabled_plugins: Optional[list[str]] = None,
               plugin_dirs: Optional[list] = None) -> list[PluginRecord]:
        """重新扫描目录并按新的启用集合加载插件。

        先卸载全部已加载插件（调用 on_unload），再 discover + load_all。
        用于设置页保存插件开关后刷新运行期状态。
        """
        for pid in list(self._plugin_modules.keys()):
            rec = self.plugins.get(pid)
            if rec is not None:
                try:
                    self.disable(pid)
                except Exception as e:
                    logger.warning("[插件] 热重载卸载 %s 失败: %s", pid, e)
        if enabled_plugins is not None:
            self.enabled = set(enabled_plugins)
        if plugin_dirs is not None:
            self.custom_dirs = [Path(p) for p in plugin_dirs]
        self.external_tools = []
        self.discover()
        return self.load_all()

    # ── 事件广播 ──
    def emit(self, event: str, payload: Any = None) -> int:
        """向所有已加载插件的 HOOKS 监听器广播事件（见 PluginHost.emit）。"""
        return PluginHost(self).emit(event, payload)

    # ── 内部工具 ──
    def register_external_tools(self, plugin_id: str, tools: list):
        """供 PluginHost 将插件回注册的工具并入全局工具列表。"""
        self.external_tools.extend(tools)

    def _find_module(self, plugin_id: str):
        """在自定义目录、内置目录中查找插件模块路径。返回 [(kind, path)]。

        每个目录内包形态优先于单文件形态；自定义目录优先于内置目录。
        """
        result = []
        for d in self.custom_dirs:
            p = d / plugin_id / "__init__.py"
            if p.exists():
                result.append(("custom", p))
            p = d / f"{plugin_id}.py"
            if p.exists():
                result.append(("custom", p))
        if self._include_builtin:
            p = self._plugins_dir / plugin_id / "__init__.py"
            if p.exists():
                result.append(("builtin", p))
            p = self._plugins_dir / f"{plugin_id}.py"
            if p.exists():
                result.append(("builtin", p))
        return result

    def _import_module(self, mod_path: Path):
        """从给定路径导入插件模块（用唯一模块名，避免 sys 冲突）。

        模块名由**文件 stem + 路径哈希**构成：单文件插件（a.py）与包插件
        （a/__init__.py）的 parent.name 都不可靠（前者是目录名、后者是自身名），
        仅用 stem 会让不同目录下的同名插件互相串模块；加路径哈希保证
        「同一路径 → 同一模块（可复用），不同路径 → 不同模块（不串）」。
        """
        mod_name = f"_agent_plugin_{mod_path.stem}_{hash(str(mod_path)) & 0xFFFFFF:x}"
        if mod_name in sys.modules:
            # 已加载过（重复 enable），回用现有模块
            return sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(mod_name, mod_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module


_registry: Optional[PluginRegistry] = None


def get_registry(enabled_plugins: Optional[list[str]] = None,
                 plugin_dirs: Optional[list] = None) -> PluginRegistry:
    """进程级单例插件注册表。

    首次调用时创建并**立即 discover()**（否则 load_all() 遍历空的 plugins
    什么都不会加载——这是原实现的隐患：单例建好了但从未扫描目录）。
    后续调用返回同一实例；传入的参数仅在首次创建时生效，运行期变更请用
    registry.reload(enabled_plugins=..., plugin_dirs=...)。
    """
    global _registry
    if _registry is None:
        _registry = PluginRegistry(enabled_plugins=enabled_plugins,
                                   plugin_dirs=plugin_dirs)
        _registry.discover()
    return _registry


def reset_registry():
    """测试用：重置注册表单例。"""
    global _registry
    _registry = None