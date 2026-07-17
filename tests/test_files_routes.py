"""文件路由新增端点单测：/files/track、/files/untrack"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.exceptions import HTTPException

ROOT = Path(__file__).resolve().parents[1] / "agent_core"
sys.path.insert(0, str(ROOT))

from api.routes.files import track_file, untrack_file


def _make_body(extra: dict | None = None):
    payload = {"project_id": "", "file_path": "sample.txt"}
    if extra:
        payload.update(extra)
    body = MagicMock()
    body.__getitem__ = lambda self, k: payload.get(k)
    body.get = lambda k, d=None: payload.get(k, d)
    return body


def _init_repo(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tracked.txt").write_text("tracked")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "untracked.txt").write_text("untracked")


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        _init_repo(p)
        yield p


def test_track_untracked_file(repo):
    req = MagicMock()
    req.state = MagicMock()
    req.state.user_id = "default"
    with patch("api.routes.files._resolve_repo_root", return_value=repo):
        resp = asyncio.run(track_file(req, _make_body({"file_path": "untracked.txt"})))
    assert resp["success"] is True
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1"], capture_output=True, text=True)
    assert "?? untracked.txt" not in out.stdout


def test_untrack_tracked_file(repo):
    req = MagicMock()
    req.state = MagicMock()
    req.state.user_id = "default"
    with patch("api.routes.files._resolve_repo_root", return_value=repo):
        resp = asyncio.run(untrack_file(req, _make_body({"file_path": "tracked.txt"})))
    assert resp["success"] is True
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "tracked.txt"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


def test_track_missing_file(repo):
    req = MagicMock()
    req.state = MagicMock()
    req.state.user_id = "default"
    with patch("api.routes.files._resolve_repo_root", return_value=repo):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(track_file(req, _make_body({"file_path": "nope.txt"})))
    assert exc.value.status_code == 404


def test_untrack_missing_file(repo):
    req = MagicMock()
    req.state = MagicMock()
    req.state.user_id = "default"
    with patch("api.routes.files._resolve_repo_root", return_value=repo):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(untrack_file(req, _make_body({"file_path": "nope.txt"})))
    assert exc.value.status_code == 404
