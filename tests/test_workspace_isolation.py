"""多用户工作目录隔离回归测试

回归点：files/projects 等接口通过 getattr(request.state, "user_id", "default")
取 uid，但 request.state.user_id 从未被赋值，导致所有用户落到共享的
WORKSPACE_BASE/default 目录，互相看到对方文件/项目。修复后 require_login
中间件把签名 cookie 中的用户名挂到 request.state.user_id。
"""
import os
import time
import tempfile

from fastapi.testclient import TestClient
from agent_core.main import app  # 先导入 app，确保 services 模块可用
from agent_core.api.deps import _sign_session
from agent_core import user_manager


def _auth_cookie(username="admin"):
    exp = int(time.time()) + 3600
    return {"desktop_agent_session": _sign_session(username, exp)}


def test_files_browse_resolves_authenticated_user_not_default(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="ws_iso_")
    monkeypatch.setattr(user_manager, "WORKSPACE_BASE", __import__("pathlib").Path(tmp))
    # 确保该用户工作目录存在，否则 /files/browse 返回 400
    os.makedirs(user_manager.user_workspace("admin"), exist_ok=True)

    with TestClient(app) as c:
        r = c.get("/files/browse", cookies=_auth_cookie("admin"))
        assert r.status_code == 200, r.text
        path = r.json()["path"]
        # 修复前 request.state.user_id 未赋值 -> 落到 "default" 共享目录；
        # 修复后应等于该登录用户(admin)自己的工作目录。
        assert path.rstrip("/").endswith("/admin"), f"期望 admin 工作目录, 实际: {path}"
        assert not path.rstrip("/").endswith("/default"), f"不应串到 default 共享目录: {path}"
