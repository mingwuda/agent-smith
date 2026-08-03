"""回归测试：git status --porcelain 多行输出不得丢失行首空格。

背景（2026-08-02）：
`_run_git` 曾用 `result.stdout.strip()` 处理 git 输出，strip() 会吃掉
多行输出**第一行行首的空白**。而 `git status --porcelain=v1` 中未暂存行
恰好以空格开头（` M path`，X 列占位空格），导致：
  - 排序第一的变更文件状态被误判为「已暂存」（raw_xy 从 ` M` 变 `M `）
  - 其路径被 `line[3:]` 错位截断，丢首字符（`desktop/...` → `esktop/...`）
  - 点击该文件 → /files/diff 404「该文件无变更」

修复：所有 git 输出处理统一 `strip()` → `rstrip()`（保留行首空格）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "agent_core"
sys.path.insert(0, str(ROOT))

from api.routes.files import _parse_porcelain, _git_rc


def _init_repo(tmp_path: Path):
    """建临时 git 仓库：提交 1 个文件，再修改 2 个文件（均未暂存）。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True, capture_output=True)
    for name in ("desktop/a.txt", "z.txt"):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("v1")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    # 两个文件都做未暂存修改（排序后 desktop/a.txt 在第一行）
    (tmp_path / "desktop/a.txt").write_text("v2")
    (tmp_path / "z.txt").write_text("v2")


def test_run_git_preserves_leading_space(tmp_path):
    _init_repo(tmp_path)
    stdout, _, _ = _git_rc(str(tmp_path), "status", "--porcelain=v1")
    # 核心断言：未暂存行（` M path`）行首空格必须保留，不得被 strip 吃掉
    assert stdout.startswith(" M desktop/a.txt"), repr(stdout)


def test_parse_porcelain_first_line_path_intact(tmp_path):
    """排序第一的变更文件：路径不得丢首字符，状态不得误判为已暂存。"""
    _init_repo(tmp_path)
    stdout, _, _ = _git_rc(str(tmp_path), "status", "--porcelain=v1")
    changes = _parse_porcelain(stdout)
    first = changes[0]
    assert first["path"] == "desktop/a.txt", changes
    assert first["raw_xy"] == " M", changes
    assert first["index_status"] == ""
    assert first["work_status"] == "modified"
    # 第二个文件也不受影响
    assert changes[1]["path"] == "z.txt", changes
