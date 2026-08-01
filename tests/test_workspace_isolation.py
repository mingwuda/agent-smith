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
from agent_core.main import app  # 先导入 app，确保 services 模块可用（注入 agent_core 到 sys.path）
from agent_core.api.deps import _sign_session
import user_manager  # 扁平导入：与 services/workspace.py 的 import user_manager 同一模块对象
# （不能用 from agent_core import user_manager —— 那会是第二份模块对象，
#   各自持有独立 WORKSPACE_BASE 全局，monkeypatch 改不到 workspace.py 用的那份）


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



def test_wechat_workspace_isolated_per_user(tmp_path, monkeypatch):
    """微信用户必须按 uid 隔离工作区，禁止共享 WORKSPACE_BASE 根目录。

    回归点：历史版本 _workspace_for_user 对 wechat_* 一律返回 WORKSPACE_BASE
    （/root/agent_workspace），所有微信用户共用一个公共工作区，agent 能扫到
    其他用户的项目目录（曾发生 admin 微信用户收到 zhangcaixin 的项目内容）。
    """
    monkeypatch.setattr(user_manager, "WORKSPACE_BASE", tmp_path)
    from services.workspace import _workspace_for_user

    ws_admin = _workspace_for_user("wechat_admin")
    ws_other = _workspace_for_user("wechat_zhangcaixin")

    # 1) 各自落到 WORKSPACE_BASE/<uid>
    assert ws_admin == tmp_path / "wechat_admin"
    assert ws_other == tmp_path / "wechat_zhangcaixin"
    # 2) 互不相同，且都不是共享根目录本身
    assert ws_admin != ws_other
    assert ws_admin != tmp_path
    assert ws_other != tmp_path
    # 3) 普通 Web 用户行为保持不变
    assert _workspace_for_user("admin") == tmp_path / "admin"
    # 4) 隔离目录已自动创建（user_workspace 内部 mkdir）
    assert ws_admin.is_dir()
