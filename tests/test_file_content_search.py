"""文件内容搜索回归测试

验证 search_files 在传入 content 参数时：
- 能匹配文件内容并返回命中文件
- 仍保留原有文件名匹配能力
- 对二进制文件 / 无匹配结果有合理返回
"""
import os
import tempfile
from pathlib import Path
from agent_core.tools.file_tools import search_files, set_workspace, _workspace_ctx


def _make_workspace():
    """创建临时工作区并写入测试文件，返回 (workspace_path, cleanup)。"""
    tmp = tempfile.mkdtemp(prefix="file_search_")
    root = Path(tmp)

    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)

    # 命中文件
    (root / "src" / "auth.py").write_text(
        "def login(username, password):\n    return check(username, password)\n",
        encoding="utf-8",
    )
    (root / "src" / "user.py").write_text(
        "def get_user():\n    pass\n",
        encoding="utf-8",
    )
    (root / "docs" / "readme.md").write_text(
        "# 登录说明\n请使用 login 接口进行认证。\n",
        encoding="utf-8",
    )

    # 不命中文件
    (root / "src" / "utils.py").write_text(
        "def helper():\n    return 1\n",
        encoding="utf-8",
    )

    # 二进制文件（应被跳过）
    (root / "binary.dat").write_bytes(bytes(range(256)))

    return root


def test_search_by_filename():
    root = _make_workspace()
    try:
        set_workspace(root)
        result = search_files.invoke({"pattern": "auth.py"})
        assert "src/auth.py" in result, result
    finally:
        _workspace_ctx.set(None)


def test_search_by_content():
    root = _make_workspace()
    try:
        set_workspace(root)
        result = search_files.invoke({"pattern": "login", "content": True})
        # 应命中 auth.py 和 readme.md
        assert "src/auth.py" in result, result
        assert "docs/readme.md" in result, result
        # 不应命中不相关文件
        assert "src/utils.py" not in result, result
    finally:
        _workspace_ctx.set(None)


def test_search_by_content_no_match():
    root = _make_workspace()
    try:
        set_workspace(root)
        result = search_files.invoke({"pattern": "nonexistent_keyword_xyz", "content": True})
        assert "未找到包含" in result, result
    finally:
        _workspace_ctx.set(None)


def test_search_by_content_skips_binary():
    root = _make_workspace()
    try:
        set_workspace(root)
        # binary.dat 包含可打印字符，但应被跳过
        result = search_files.invoke({"pattern": "content", "content": True})
        assert "binary.dat" not in result, result
    finally:
        _workspace_ctx.set(None)


def test_search_without_content_flag_does_not_search_body():
    """默认行为应只搜文件名，不读文件内容。"""
    root = _make_workspace()
    try:
        set_workspace(root)
        # auth.py 文件名不含 login，默认不应命中
        result = search_files.invoke({"pattern": "login"})
        assert "src/auth.py" not in result, result
        assert "docs/readme.md" not in result, result
    finally:
        _workspace_ctx.set(None)


if __name__ == "__main__":
    pytest = __import__("pytest", fromlist=["main"])
    pytest.main([__file__, "-v"])
