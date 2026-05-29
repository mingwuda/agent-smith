"""Git repository inspection tools."""
from pathlib import Path
import os
import shlex
import subprocess
from typing import Optional

from langchain_core.tools import tool


_workspace: Optional[Path] = None

_ALLOWED_SUBCOMMANDS = {
    "add",
    "commit",
    "status",
    "diff",
    "log",
    "show",
    "branch",
    "remote",
    "rev-parse",
    "ls-files",
}
_BLOCKED_ARGS = {"--output", "--output=", "-o", "--exec", "--ext-diff"}
_MAX_OUTPUT_CHARS = 20000
_TIMEOUT_SECONDS = 20


def set_workspace(path: Path):
    global _workspace
    _workspace = path.expanduser().resolve()


def _workspace_root() -> Path:
    return (_workspace or Path.home() / "agent_workspace").expanduser().resolve()


def _resolve_repo(path: str = "") -> Path:
    workspace = _workspace_root()
    raw = Path(path or ".").expanduser()
    target = raw if raw.is_absolute() else workspace / raw
    target = target.resolve(strict=False)
    if target.is_file():
        target = target.parent
    return target


def _run_git(args: list[str], repo_path: str = "") -> str:
    if not args:
        return "❌ 请提供 git 子命令，例如 status、diff、log。"

    subcommand = args[0]
    if subcommand not in _ALLOWED_SUBCOMMANDS:
        allowed = ", ".join(sorted(_ALLOWED_SUBCOMMANDS))
        return f"❌ 当前 git 工具只允许这些命令: {allowed}"
    validation_error = _validate_args(args)
    if validation_error:
        return validation_error

    try:
        cwd = _resolve_repo(repo_path)
    except ValueError as exc:
        return f"❌ {exc}"

    if not cwd.exists() or not cwd.is_dir():
        return f"❌ Git 目录不存在: {repo_path or '.'}"

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env={
                **os.environ,
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                "GIT_PAGER": "cat",
                "GIT_EXTERNAL_DIFF": "",
            },
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return "❌ 系统未找到 git 命令"
    except subprocess.TimeoutExpired:
        return f"❌ git {' '.join(args)} 执行超过 {_TIMEOUT_SECONDS} 秒，已中止"

    output = (completed.stdout or "") + (completed.stderr or "")
    output = output.strip() or "（无输出）"
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n...（输出过长，已截断）"
    prefix = "✅" if completed.returncode == 0 else f"❌ 退出码 {completed.returncode}"
    return f"{prefix} git {' '.join(args)}\n{output}"


def _validate_args(args: list[str]) -> Optional[str]:
    for arg in args[1:]:
        if arg in _BLOCKED_ARGS or any(arg.startswith(prefix) for prefix in _BLOCKED_ARGS if prefix.endswith("=")):
            return f"❌ 不允许使用可能写文件或执行外部程序的参数: {arg}"

    subcommand = args[0]
    if subcommand == "remote":
        allowed = {"remote", "-v", "--verbose"}
        if any(arg not in allowed for arg in args):
            return "❌ git remote 仅允许查看 remote 或 remote -v"
    if subcommand == "branch":
        allowed = {"branch", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current"}
        if any(arg not in allowed for arg in args):
            return "❌ git branch 仅允许查看分支列表或当前分支"
    if subcommand == "add":
        allowed_options = {"-A", "--all", "--"}
        for arg in args[1:]:
            if arg.startswith("-") and arg not in allowed_options:
                return "❌ git add 仅允许普通暂存，不允许其它选项"
    if subcommand == "commit":
        if len(args) != 3 or args[1] != "-m" or not args[2].strip():
            return "❌ git commit 仅允许普通提交格式: git commit -m \"message\""
    return None


@tool
def git_status(path: str = "") -> str:
    """查看 Git 工作区状态。path 默认为当前工作区；也支持绝对路径。"""
    return _run_git(["status", "--short", "--branch"], path)


@tool
def git_diff(path: str = "", staged: bool = False, file_path: str = "") -> str:
    """查看 Git diff。path 支持绝对路径；staged=True 查看暂存区；file_path 可限制到某个文件。"""
    args = ["diff", "--no-ext-diff"]
    if staged:
        args.append("--staged")
    if file_path:
        args.extend(["--", file_path])
    return _run_git(args, path)


@tool
def git_log(path: str = "", limit: int = 10) -> str:
    """查看最近的 Git 提交日志。limit 控制条数，默认 10 条。"""
    safe_limit = max(1, min(int(limit or 10), 50))
    return _run_git(["log", f"-{safe_limit}", "--oneline", "--decorate"], path)


@tool
def git_show(revision: str = "HEAD", path: str = "") -> str:
    """查看指定 Git revision 的内容，例如 HEAD、HEAD~1 或提交哈希。"""
    return _run_git(["show", "--no-ext-diff", "--stat", "--patch", revision], path)


@tool
def git_add(path: str = "", file_paths: str = "", all_changes: bool = True) -> str:
    """暂存 Git 改动。默认 all_changes=True 执行 git add -A；也可用 file_paths 指定文件，空格分隔。"""
    if all_changes:
        return _run_git(["add", "-A"], path)
    if not file_paths.strip():
        return "❌ all_changes=False 时需要提供 file_paths"
    try:
        files = shlex.split(file_paths)
    except ValueError as exc:
        return f"❌ 文件路径解析失败: {exc}"
    return _run_git(["add", "--", *files], path)


@tool
def git_commit(message: str, path: str = "") -> str:
    """提交已暂存的 Git 改动。message 是提交信息。"""
    message = (message or "").strip()
    if not message:
        return "❌ 提交信息不能为空"
    return _run_git(["commit", "-m", message], path)


@tool
def git_commit_all(message: str, path: str = "") -> str:
    """暂存所有改动并提交，适合用户明确要求“帮我提交代码”。"""
    if not (message or "").strip():
        return "❌ 提交信息不能为空"
    add_result = _run_git(["add", "-A"], path)
    if not add_result.startswith("✅"):
        return add_result
    commit_result = _run_git(["commit", "-m", message], path)
    return add_result + "\n\n" + commit_result


@tool
def git_command(command: str, path: str = "") -> str:
    """执行受限的 Git 命令。允许查看类命令，以及 add/commit；不允许 push/pull/reset/restore。"""
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return f"❌ 命令解析失败: {exc}"
    if args and args[0] == "git":
        args = args[1:]
    return _run_git(args, path)


TOOLS = [git_status, git_diff, git_log, git_show, git_add, git_commit, git_commit_all, git_command]
