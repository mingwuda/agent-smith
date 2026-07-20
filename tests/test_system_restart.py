"""系统重启接口测试"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1] / "agent_core"
sys.path.insert(0, str(ROOT))

from agent_core.api.routes.system import restart_backend, RestartResponse


class _FakeRequest:
    """模拟带管理员权限的 Request"""
    class State:
        pass
    state = State()
    cookies = {"desktop_agent_session": "admin:9999999999:fake"}


def test_restart_returns_ok():
    """管理员调用重启接口应返回成功响应并启动后台线程"""
    req = _FakeRequest()
    with patch("agent_core.api.routes.system.sys.exit") as mock_exit, \
         patch("agent_core.api.routes.system.os._exit") as mock_os_exit, \
         patch("threading.Thread") as mock_thread:
        mock_thread.return_value.start = MagicMock()
        resp = restart_backend(req)
        assert isinstance(resp, RestartResponse)
        assert resp.status == "ok"
        assert "重启" in resp.message or "restart" in resp.message.lower()
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()


def test_restart_triggers_async_exit():
    """重启接口应在后台线程中触发进程退出"""
    req = _FakeRequest()
    exited = []

    def fake_exit(code):
        exited.append(code)
        raise SystemExit(code)

    with patch("agent_core.api.routes.system.sys.exit", side_effect=fake_exit), \
         patch("agent_core.api.routes.system.os._exit", side_effect=fake_exit), \
         patch("threading.Thread") as mock_thread:
        mock_thread.return_value.start = MagicMock()
        resp = restart_backend(req)
        assert resp.status == "ok"
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()


def test_restart_requires_admin():
    """非 admin 调用应被拒绝"""
    from api.deps import _require_admin
    from fastapi import HTTPException

    req = _FakeRequest()
    req.cookies = {"desktop_agent_session": "user:9999999999:fake"}
    try:
        _require_admin(req)
    except HTTPException as e:
        assert e.status_code == 403
    else:
        raise AssertionError("应抛出 403")
