"""终端 WebSocket 路由的最小自检：不拉起真实 shell，只验证鉴权与 cwd 口径。

ponytail: 交互式 PTY 的端到端行为由浏览器实测覆盖，这里只守住两个容易静默坏掉的
边界——未登录不得拿到 shell、shell 的 cwd 必须与 /files/* 用同一套项目目录解析。
"""
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "agent_core"
sys.path.insert(0, str(ROOT))


def test_ws_requires_login():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.terminal import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    # HTTP middleware 不覆盖 WebSocket，路由必须自己校验 cookie；未登录应直接被拒绝
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/terminal") as ws:
            ws.receive_text()


def test_resolve_cwd_defaults_to_workspace(monkeypatch):
    import services.workspace as _ws
    from api.routes import terminal

    # 用临时目录而非 pytest 的 tmp_path：本环境的 sitecustomize 会让
    # pytest-of-unknown 目录偶发 EEXIST，与被测逻辑无关。
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        monkeypatch.setattr(_ws, "_workspace_for_user", lambda uid: base)
        assert terminal._resolve_cwd("u1", "") == base.resolve()


def test_resolve_cwd_uses_project_directory(monkeypatch):
    import services.workspace as _ws
    import session_store
    from api.routes import terminal

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        project = base / "proj"
        project.mkdir()
        monkeypatch.setattr(_ws, "_workspace_for_user", lambda uid: base)
        monkeypatch.setattr(session_store, "get_project",
                            lambda uid, pid: {"directory_path": str(project)})
        assert terminal._resolve_cwd("u1", "p1") == project.resolve()
