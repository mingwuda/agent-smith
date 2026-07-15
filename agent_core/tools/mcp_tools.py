"""MCP 工具接入模块

将外部 MCP Server 提供的工具动态包装为 LangChain Tool，
注册到 DesktopAgent 的 all_tools 列表中，使 Agent 可以调用 MCP 能力。

架构要点（修复版）：
- 单一后台事件循环：所有 MCP 操作（连接、握手、list、call）都在同一个
  长期运行的 asyncio loop 中执行，彻底消除 loop 错配。
- 正确握手：每个连接建立后立即发送 initialize + notifications/initialized。
- 线程安全：通过 loop.call_soon_threadsafe + Future 桥接同步/异步边界。
- stdin 写入串行化：同一 session 的写入用 asyncio.Lock 保护，避免字节交错。
- stderr 后台消费：子进程 stderr 持续读取并打日志，防止管道死锁。
- 支持 stdio transport；SSE 框架已预留但显式报错，避免静默失败。
- 不依赖 mcp SDK，兼容 Python 3.9+。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 1. 单一后台事件循环
# ─────────────────────────────────────────────────────────────────────────────

class _BackgroundLoop:
    """单例：一个长期运行的 asyncio 事件循环，跑在独立线程中。

    所有 MCP 连接、会话、请求都通过这个 loop 调度，从根源上消除
    "跨 loop 操作" 和 "asyncio.Lock 绑错 loop" 的问题。
    """

    _instance: Optional["_BackgroundLoop"] = None
    _init_lock = threading.Lock()

    def __new__(cls) -> "_BackgroundLoop":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._started = False
        return cls._instance

    def start(self):
        if self._started:
            return
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-bg-loop")
        self._thread.start()
        self._ready.wait(timeout=10)
        if not self._started:
            raise RuntimeError("MCP 后台事件循环启动超时")
        logger.info("[MCP] 后台事件循环已启动")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._started = True
        try:
            self._loop.run_forever()
        except Exception as exc:
            logger.error("[MCP] 后台循环异常退出: %s", exc)
        finally:
            self._loop.close()

    def stop(self):
        if not self._started:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._started = False
        logger.info("[MCP] 后台事件循环已停止")

    def submit(self, coro):
        """在后台 loop 中运行协程，返回 concurrent.futures.Future。"""
        if not self._started:
            self.start()
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut

    def get_loop(self) -> asyncio.AbstractEventLoop:
        if not self._started:
            self.start()
        return self._loop


# 全局单例
_bg_loop = _BackgroundLoop()


# ─────────────────────────────────────────────────────────────────────────────
# 2. MCP 会话（stdio transport，纯 asyncio，跑在后台 loop 上）
# ─────────────────────────────────────────────────────────────────────────────

class _StdioMCPSession:
    """stdio transport 的 MCP 会话。

    所有方法都是 async，必须在 _bg_loop 的线程中调用。
    """

    def __init__(self, name: str, proc: asyncio.subprocess.Process):
        self.name = name
        self._proc = proc
        self._req_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closed = False
        # 同一 session 的写入串行化，防止字节流交错
        self._write_lock = asyncio.Lock()

    async def start(self):
        self._reader_task = asyncio.create_task(self._read_responses())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def send_request(self, method: str, params: dict) -> dict:
        """发送 JSON-RPC 2.0 请求并等待响应。"""
        if self._closed:
            raise RuntimeError("Session 已关闭")

        if not self._reader_task:
            await self.start()

        self._req_id += 1
        req_id = self._req_id

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            # 写入加锁，防止并发调用时字节流交错
            async with self._write_lock:
                self._proc.stdin.write(line.encode("utf-8"))
                await self._proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=120)
        except asyncio.TimeoutError:
            raise TimeoutError(f"MCP 请求超时: {method}")
        finally:
            self._pending.pop(req_id, None)

    async def send_notification(self, method: str, params: dict):
        """发送 JSON-RPC 2.0 通知（不等待响应）。"""
        if self._closed:
            raise RuntimeError("Session 已关闭")

        if not self._reader_task:
            await self.start()

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        line = json.dumps(payload, ensure_ascii=False) + "\n"
        async with self._write_lock:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()

    async def _read_responses(self):
        try:
            while not self._closed:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    response = json.loads(line)
                    req_id = response.get("id")
                    if req_id in self._pending:
                        fut = self._pending[req_id]
                        if not fut.done():
                            if "error" in response:
                                fut.set_exception(RuntimeError(f"MCP 错误: {response['error']}"))
                            else:
                                fut.set_result(response.get("result", {}))
                except json.JSONDecodeError:
                    logger.debug("[MCP:%s] 忽略非 JSON 行: %s", self.name, line[:100])
        except Exception as exc:
            logger.debug("[MCP:%s] 读取响应循环结束: %s", self.name, exc)

    async def _read_stderr(self):
        """持续读取子进程 stderr，防止管道满导致死锁。"""
        try:
            while not self._closed:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("[MCP:%s] stderr: %s", self.name, text[:200])
        except Exception as exc:
            logger.debug("[MCP:%s] stderr 读取结束: %s", self.name, exc)

    async def close(self):
        self._closed = True
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            self._proc.stdin.close()
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# 3. MCP Server 连接（跑在后台 loop 上）
# ─────────────────────────────────────────────────────────────────────────────

class MCPServerConnection:
    """单个 MCP Server 的长连接会话管理器。

    注意：所有 async 方法都通过 _bg_loop.submit() 在后台 loop 中执行。
    外部调用方拿到的是 concurrent.futures.Future，用 .result() 同步等待。
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config
        self._session: Optional[_StdioMCPSession] = None
        # 连接级锁：防止并发首次连接时起多个子进程
        self._connect_lock = asyncio.Lock()

    # ── 内部 async 方法（在后台 loop 中运行） ──

    async def _ensure_connected(self):
        if self._session is not None and not self._session._closed:
            return

        # 连接级锁：同一 server 的并发首次连接只起一个子进程
        async with self._connect_lock:
            # 双重检查：可能在等锁时已被其他调用建立连接
            if self._session is not None and not self._session._closed:
                return

            command = self.config.get("command", "")
            args = self.config.get("args", [])
            env = self.config.get("env", {})

            if not command:
                raise ValueError(f"MCP server '{self.name}' 缺少 command 配置")

            merged_env = os.environ.copy()
            if env:
                merged_env.update({str(k): str(v) for k, v in env.items()})

            proc = await asyncio.create_subprocess_exec(
                command,
                *[str(a) for a in args],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
            )

            self._session = _StdioMCPSession(self.name, proc)
            await self._session.start()

            # ★ 关键修复：发送 initialize 握手（支持多版本协商）
            init_response = await self._session.send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "desktop-agent", "version": "1.0.0"},
            })

            # 读取 server 返回的协议版本并记录
            self._server_protocol_version = init_response.get("protocolVersion", "2024-11-05")
            server_caps = init_response.get("capabilities", {})
            logger.info("[MCP:%s] 握手成功，protocolVersion=%s, capabilities=%s", self.name, self._server_protocol_version, server_caps)
            # ponytail: 当前仅记录 server 协议版本，尚未实现多版本兼容分支。
            # 后续请求仍使用 client 提议的 "2024-11-05"。
            # 若遇到仅支持旧协议的 server 握手失败，升级路径：
            #   1. 在此处根据 self._server_protocol_version 分支
            #   2. 为不同版本实现 tools/list、tools/call 的格式适配
            #   3. 补充对应版本的集成测试

            # ★ 发送 notifications/initialized（协议要求）
            await self._session.send_notification("notifications/initialized", {})

            logger.info("[MCP:%s] 已连接", self.name)

    async def _list_tools_async(self) -> List[dict]:
        await self._ensure_connected()
        response = await self._session.send_request("tools/list", {})
        tools = []
        for tool in response.get("tools", []):
            tools.append({
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", {}),
            })
        return tools

    async def _call_tool_async(self, tool_name: str, arguments: dict) -> str:
        await self._ensure_connected()
        try:
            result = await self._session.send_request(
                "tools/call",
                {"name": tool_name, "arguments": arguments}
            )
            content_parts = []
            for item in result.get("content", []):
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        content_parts.append(item.get("text", ""))
                    else:
                        content_parts.append(str(item))
                else:
                    content_parts.append(str(item))
            return "\n".join(content_parts) if content_parts else str(result)
        except Exception as exc:
            error_msg = f"[MCP] 工具调用失败: {self.name}.{tool_name}, error: {type(exc).__name__}: {exc}"
            logger.warning(error_msg)
            return f"❌ {error_msg}"

    async def _close_async(self):
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    # ── 同步接口（供外部调用） ──

    def list_tools(self, timeout: int = 60) -> List[dict]:
        fut = _bg_loop.submit(self._list_tools_async())
        return fut.result(timeout=timeout)

    def call_tool(self, tool_name: str, arguments: dict, timeout: int = 120) -> str:
        fut = _bg_loop.submit(self._call_tool_async(tool_name, arguments))
        return fut.result(timeout=timeout)

    def close(self, timeout: int = 10):
        fut = _bg_loop.submit(self._close_async())
        fut.result(timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 全局连接池
# ─────────────────────────────────────────────────────────────────────────────

_connections: Dict[str, MCPServerConnection] = {}
_conn_lock = threading.Lock()


def _get_connection(name: str, config: Dict[str, Any]) -> MCPServerConnection:
    with _conn_lock:
        if name not in _connections:
            _connections[name] = MCPServerConnection(name, config)
        return _connections[name]


def close_all_connections(timeout: int = 10):
    """关闭所有 MCP 连接（应在服务关停时调用）。"""
    with _conn_lock:
        for conn in list(_connections.values()):
            try:
                conn.close(timeout=timeout)
            except Exception as exc:
                logger.warning("[MCP] 关闭连接失败: %s, error: %s", conn.name, exc)
        _connections.clear()
    # 关停后台事件循环，避免 graceful reload 时留空转 loop
    try:
        _bg_loop.stop()
    except Exception:
        logger.exception("[MCP] 停止后台循环失败")


# ─────────────────────────────────────────────────────────────────────────────
# 5. 工具包装
# ─────────────────────────────────────────────────────────────────────────────

def _pydantic_model_from_schema(schema: dict, model_name: str) -> type[BaseModel]:
    fields: Dict[str, Any] = {}
    props = schema.get("properties", {})
    required = set(schema.get("required", []))

    for field_name, field_schema in props.items():
        ftype = field_schema.get("type", "string")
        type_map = {
            "string": str, "integer": int, "number": float,
            "boolean": bool, "array": list, "object": dict,
        }
        field_type = type_map.get(ftype, str)
        description = field_schema.get("description", "")

        if field_name in required:
            fields[field_name] = (field_type, Field(..., description=description))
        else:
            fields[field_name] = (Optional[field_type], Field(default=None, description=description))

    return create_model(model_name, **fields)


def _create_mcp_tool(
    server_name: str,
    tool_def: dict[str, Any],
    connection: MCPServerConnection,
) -> StructuredTool:
    tool_name = tool_def["name"]
    description = tool_def.get("description", "")
    input_schema = tool_def.get("input_schema", {})
    langchain_name = f"mcp_{server_name}_{tool_name}"
    param_model = _pydantic_model_from_schema(input_schema, f"{langchain_name}_params")

    def _threaded_call(**kwargs: Any) -> str:
        try:
            return connection.call_tool(tool_name, kwargs)
        except Exception as exc:
            error_msg = f"[MCP] 工具调用失败: {langchain_name}, error: {type(exc).__name__}: {exc}"
            logger.warning(error_msg)
            return f"❌ {error_msg}"

    return StructuredTool.from_function(
        name=langchain_name,
        description=f"[MCP:{server_name}] {description}",
        func=_threaded_call,
        args_schema=param_model,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. 主入口
# ─────────────────────────────────────────────────────────────────────────────

def create_mcp_tools(server_configs: List[Dict[str, Any]]) -> List[StructuredTool]:
    """从 MCP Server 配置列表创建 LangChain 工具列表。

    Args:
        server_configs: MCP Server 配置列表，每个条目包含：
            - name: server 标识
            - transport: "stdio" (默认) 或 "sse"
            - command: stdio 模式下要执行的命令
            - args: 命令参数列表
            - env: 环境变量字典
            - url: SSE 模式下的服务地址

    Returns:
        LangChain StructuredTool 列表
    """
    if not server_configs:
        return []

    # 确保后台 loop 已启动
    _bg_loop.start()

    tools: List[StructuredTool] = []

    for cfg in server_configs:
        name = cfg.get("name", "")
        if not name:
            logger.warning("[MCP] 跳过未命名的 server 配置: %s", cfg)
            continue

        transport = (cfg.get("transport") or "stdio").lower()
        if transport != "stdio":
            logger.warning("[MCP] server '%s' 使用 '%s' transport，当前仅支持 stdio，已跳过", name, transport)
            continue

        try:
            connection = _get_connection(name, cfg)
            tool_defs = connection.list_tools(timeout=60)

            for tool_def in tool_defs:
                try:
                    lc_tool = _create_mcp_tool(name, tool_def, connection)
                    tools.append(lc_tool)
                    logger.debug("[MCP] 已注册工具: %s", lc_tool.name)
                except Exception as exc:
                    logger.warning("[MCP] 包装工具失败: %s.%s, error: %s", name, tool_def.get("name"), exc)

            logger.info("[MCP] server '%s' 已加载 %d 个工具", name, len(tool_defs))

        except Exception as exc:
            logger.error("[MCP] 加载 server '%s' 失败: %s", name, exc)

    return tools


# 供 main.py 导入
TOOLS: List[StructuredTool] = []


def load_tools(server_configs: List[Dict[str, Any]]) -> List[StructuredTool]:
    """加载 MCP 工具（供 main.py 调用）。"""
    global TOOLS
    TOOLS = create_mcp_tools(server_configs)
    return TOOLS
