"""MCP 工具模块测试（修复后）"""
import json
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

# 确保能导入 agent_core 模块
sys.path.insert(0, str(Path(__file__).parent.parent / "agent_core"))

from tools.mcp_tools import (
    _pydantic_model_from_schema,
    _create_mcp_tool,
    MCPServerConnection,
    _StdioMCPSession,
    _BackgroundLoop,
    _bg_loop,
    create_mcp_tools,
    close_all_connections,
)


class TestPydanticModelFromSchema:
    def test_simple_string_field(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "名称"}},
            "required": ["name"]
        }
        model = _pydantic_model_from_schema(schema, "TestModel")
        instance = model(name="test")
        assert instance.name == "test"

    def test_optional_field(self):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer", "description": "数量"}}
        }
        model = _pydantic_model_from_schema(schema, "TestModel")
        instance = model()
        assert instance.count is None

    def test_multiple_types(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "enabled": {"type": "boolean"},
                "tags": {"type": "array"},
                "meta": {"type": "object"}
            },
            "required": ["name"]
        }
        model = _pydantic_model_from_schema(schema, "TestModel")
        instance = model(name="test", count=10, ratio=0.5, enabled=True, tags=["a"], meta={"k": "v"})
        assert instance.name == "test"
        assert instance.count == 10


class TestCreateMCPTool:
    def test_tool_name_prefix(self):
        tool_def = {
            "name": "read_file",
            "description": "读取文件",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
        connection = MCPServerConnection("filesystem", {"command": "echo"})
        tool = _create_mcp_tool("filesystem", tool_def, connection)
        assert tool.name == "mcp_filesystem_read_file"
        assert "filesystem" in tool.description

    def test_tool_schema_mapping(self):
        tool_def = {
            "name": "query",
            "description": "查询数据库",
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL"},
                    "limit": {"type": "integer", "description": "限制"}
                },
                "required": ["sql"]
            }
        }
        connection = MCPServerConnection("db", {"command": "echo"})
        tool = _create_mcp_tool("db", tool_def, connection)
        assert "sql" in tool.args_schema.model_fields
        assert "limit" in tool.args_schema.model_fields


class TestCreateMCPTools:
    def test_empty_config(self):
        tools = create_mcp_tools([])
        assert tools == []

    def test_invalid_server_config(self):
        tools = create_mcp_tools([{"transport": "sse"}])
        assert tools == []

    def test_connection_failure(self):
        tools = create_mcp_tools([{
            "name": "bad_server",
            "command": "nonexistent_command_12345",
            "args": []
        }])
        assert isinstance(tools, list)


class TestBackgroundLoop:
    """测试单一后台事件循环"""

    def test_singleton(self):
        loop1 = _BackgroundLoop()
        loop2 = _BackgroundLoop()
        assert loop1 is loop2

    def test_start_stop(self):
        loop = _BackgroundLoop()
        # 重置状态以便测试
        loop._started = False
        loop.start()
        assert loop._started is True
        assert loop._loop.is_running()
        loop.stop()
        assert loop._started is False

    def test_submit(self):
        loop = _BackgroundLoop()
        # 重置单例状态以便测试
        loop._started = False
        if hasattr(loop, '_thread') and loop._thread.is_alive():
            loop.stop()
        loop.start()

        async def coro():
            return 42

        fut = loop.submit(coro())
        result = fut.result(timeout=5)
        assert result == 42

        loop.stop()


class TestStdioMCPSession:
    """测试 stdio MCP 会话（用 cat 作为 mock server）"""

    @pytest.fixture
    def mock_mcp_server(self):
        """启动一个简单的 JSON-RPC echo server 作为 mock"""
        # 用 Python 脚本模拟 MCP server
        server_script = '''
import sys
import json

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            method = req.get("method", "")
            req_id = req.get("id")
            
            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}}
                    }
                }
            elif method == "notifications/initialized":
                # 通知不需要响应
                continue
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": [
                            {"name": "echo", "description": "Echo tool", "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}}}
                        ]
                    }
                }
            elif method == "tools/call":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "echoed"}]
                    }
                }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": "Method not found"}
                }
            
            if method != "notifications/initialized":
                print(json.dumps(resp), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "error": {"message": str(e)}}), flush=True)

if __name__ == "__main__":
    main()
'''
        return server_script

    def test_real_handshake_and_list(self, mock_mcp_server):
        """测试真实的 initialize 握手 + tools/list"""
        import tempfile
        import subprocess

        # 写入 mock server 脚本
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(mock_mcp_server)
            server_path = f.name

        try:
            conn = MCPServerConnection("mock", {
                "command": sys.executable,
                "args": [server_path]
            })

            # 测试 list_tools（会触发 initialize 握手）
            tools = conn.list_tools(timeout=10)
            assert len(tools) == 1
            assert tools[0]["name"] == "echo"
            assert tools[0]["description"] == "Echo tool"

            # 测试 call_tool
            result = conn.call_tool("echo", {"msg": "hello"}, timeout=10)
            assert "echoed" in result

            conn.close(timeout=5)
        finally:
            Path(server_path).unlink(missing_ok=True)

    def test_concurrent_calls(self, mock_mcp_server):
        """测试同一 server 的并发调用（验证 stdin 写入串行化）"""
        import tempfile

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(mock_mcp_server)
            server_path = f.name

        try:
            conn = MCPServerConnection("mock", {
                "command": sys.executable,
                "args": [server_path]
            })

            # 先 list 一次建立连接
            conn.list_tools(timeout=10)

            # 并发调用 5 次
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [
                    executor.submit(conn.call_tool, "echo", {"msg": f"msg{i}"}, 10)
                    for i in range(5)
                ]
                results = [f.result(timeout=15) for f in futures]

            # 所有调用都应该成功
            assert all("echoed" in r for r in results)

            conn.close(timeout=5)
        finally:
            Path(server_path).unlink(missing_ok=True)


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
