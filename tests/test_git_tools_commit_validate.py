"""回归测试：git_command 的 commit 白名单校验。

背景（2026-08-03）：用户要求放行 `git commit --amend -m "msg"` 用于改写
最近提交信息（amend 会改写历史，白名单需显式精确放行）。
本测试锁定白名单边界：普通提交与 --amend -m 放行，其它 amend 用法保持禁止。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "agent_core"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.git_tools import _validate_args


def test_commit_plain_allowed():
    assert _validate_args(["commit", "-m", "feat: x"]) is None


def test_commit_amend_with_message_allowed():
    assert _validate_args(["commit", "--amend", "-m", "feat: x"]) is None


def test_commit_amend_empty_message_rejected():
    assert _validate_args(["commit", "--amend", "-m", ""]) is not None


def test_commit_amend_no_edit_rejected():
    # --no-edit 会改写历史且不改信息，超出放行范围
    assert _validate_args(["commit", "--amend", "--no-edit"]) is not None


def test_commit_bare_rejected():
    assert _validate_args(["commit"]) is not None


def test_commit_missing_message_rejected():
    assert _validate_args(["commit", "-m"]) is not None
