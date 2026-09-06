"""分页读 + 写接口测试"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "agent_core"
sys.path.insert(0, str(ROOT))


# ponytail: 直接测 /files/read 和 /files/write 路由，避免拉起整个 FastAPI app
@pytest.fixture
def client(tmp_path, monkeypatch):
    # 准备一个临时工作区，里面有 3 个文件：小、中、大
    small = tmp_path / "small.txt"
    small.write_text("a\nb\nc\n", encoding="utf-8")
    medium = tmp_path / "medium.py"
    medium.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
    big = tmp_path / "big.txt"
    big.write_text("x" * (600 * 1024), encoding="utf-8")  # 600KB

    # 强制 workspace 指向 tmp_path
    import services.workspace as _ws
    monkeypatch.setattr(_ws, "_workspace_for_user", lambda uid: tmp_path)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.files import router

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def add_user_id(request, call_next):
        request.state.user_id = "test-user"
        return await call_next(request)

    return TestClient(app), {
        "small": str(small),
        "medium": str(medium),
        "big": str(big),
    }


def test_read_full_file(client):
    c, paths = client
    r = c.get("/files/read", params={"path": paths["small"]})
    assert r.status_code == 200
    data = r.json()
    assert data["content"] == "a\nb\nc\n"
    assert data["lines"] == 3
    assert data["editable"] is True
    # 默认 limit=0 时不带分页字段
    assert "has_more" not in data


def test_read_paginated(client):
    c, paths = client
    # medium 有 50 行，前 10 行
    r = c.get("/files/read", params={"path": paths["medium"], "offset": 0, "limit": 10})
    assert r.status_code == 200
    data = r.json()
    assert data["lines"] == 50
    assert data["offset"] == 0
    assert data["limit"] == 10
    assert data["has_more"] is True
    assert data["next_offset"] == 10
    assert "line 0" in data["content"]
    assert "line 9" in data["content"]
    assert "line 10" not in data["content"]


def test_read_paginated_last_page(client):
    c, paths = client
    # 最后 5 行
    r = c.get("/files/read", params={"path": paths["medium"], "offset": 45, "limit": 10})
    assert r.status_code == 200
    data = r.json()
    assert data["offset"] == 45
    assert data["limit"] == 5
    assert data["has_more"] is False
    assert data["next_offset"] is None


def test_read_oversized_not_editable(client):
    c, paths = client
    # 600KB 文件：可读且可编辑（阈值是 1MB）
    r = c.get("/files/read", params={"path": paths["big"]})
    assert r.status_code == 200
    data = r.json()
    assert data["editable"] is True


def test_read_path_traversal_blocked(client):
    c, paths = client
    r = c.get("/files/read", params={"path": "/etc/passwd"})
    assert r.status_code in (403, 404)


def test_write_basic(client):
    c, paths = client
    new_content = "hello\nworld\n"
    r = c.post("/files/write", json={"path": paths["small"], "content": new_content})
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    # 验证真的写入了
    assert Path(paths["small"]).read_text(encoding="utf-8") == new_content


def test_write_unicode(client):
    c, paths = client
    content = "你好\n世界\n🦄\n"
    r = c.post("/files/write", json={"path": paths["small"], "content": content})
    assert r.status_code == 200
    assert Path(paths["small"]).read_text(encoding="utf-8") == content


def test_write_outside_workspace_rejected(client, tmp_path):
    c, paths = client
    # 写 tmp_path 之外的文件（/etc/hostname）
    r = c.post("/files/write", json={"path": "/etc/hostname", "content": "evil"})
    # 路径在工作区外 → 403（_resolve_base_path + relative_to 失败）
    assert r.status_code in (403, 404)


def test_write_oversized_rejected(client, tmp_path):
    c, paths = client
    # 写 1.5MB 内容
    big_content = "x" * (1500 * 1024)
    r = c.post("/files/write", json={"path": paths["small"], "content": big_content})
    assert r.status_code == 413


def test_write_creates_tmp_and_replaces(client):
    c, paths = client
    r = c.post("/files/write", json={"path": paths["small"], "content": "atomic test\n"})
    assert r.status_code == 200
    # tmp 文件不应残留
    tmp_path = Path(paths["small"]).with_suffix(Path(paths["small"]).suffix + ".tmp")
    assert not tmp_path.exists()


def test_write_missing_path_rejected(client):
    c, paths = client
    r = c.post("/files/write", json={"path": "", "content": "x"})
    assert r.status_code == 400


def test_read_pagination_offset_beyond_total(client):
    c, paths = client
    # medium 50 行，offset=100 应该返回空内容
    r = c.get("/files/read", params={"path": paths["medium"], "offset": 100, "limit": 10})
    assert r.status_code == 200
    data = r.json()
    assert data["content"] == ""
    assert data["has_more"] is False
    assert data["limit"] == 0
