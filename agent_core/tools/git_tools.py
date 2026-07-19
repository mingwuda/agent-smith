"""Git repository inspection tools."""
from contextvars import ContextVar
from pathlib import Path
import os
import shlex
import subprocess
from typing import Optional

from langchain_core.tools import tool


_workspace_ctx: ContextVar[Optional[Path]] = ContextVar("git_tools_workspace", default=None)

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
    "push",
    "revert",
    "worktree",
    "merge",
    "checkout",
}
_BLOCKED_ARGS = {"--output", "--output=", "-o", "--exec", "--ext-diff"}
_MAX_OUTPUT_CHARS = 20000
_TIMEOUT_SECONDS = 20


def set_workspace(path: Path):
    _workspace_ctx.set(path.expanduser().resolve())


def _workspace_root() -> Path:
    return (_workspace_ctx.get() or Path.home() / "agent_workspace").expanduser().resolve()


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
        allowed_view = {"branch", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current"}
        if all(arg in allowed_view for arg in args):
            return None
        # 允许删除分支: git branch -d/-D <name>
        if len(args) == 3 and args[1] in ("-d", "-D") and _is_safe_git_ref(args[2]):
            return None
        return "❌ git branch 仅允许查看或删除分支 (git branch -d/-D <name>)"
    if subcommand == "add":
        allowed_options = {"-A", "--all", "--"}
        for arg in args[1:]:
            if arg.startswith("-") and arg not in allowed_options:
                return "❌ git add 仅允许普通暂存，不允许其它选项"
    if subcommand == "commit":
        if len(args) != 3 or args[1] != "-m" or not args[2].strip():
            return "❌ git commit 仅允许普通提交格式: git commit -m \"message\""
    if subcommand == "push":
        if len(args) == 1:
            return None
        if len(args) == 3 and _is_safe_git_ref(args[1]) and _is_safe_git_ref(args[2]):
            return None
        if (
            len(args) == 4
            and args[1] in {"-u", "--set-upstream"}
            and _is_safe_git_ref(args[2])
            and _is_safe_git_ref(args[3])
        ):
            return None
        return "❌ git push 仅允许: git push、git push origin branch 或 git push -u origin branch"
    if subcommand == "revert":
        if len(args) == 2 and _is_safe_single_revision(args[1]):
            return None
        if len(args) == 3 and args[1] in {"--no-commit", "-n"} and _is_safe_single_revision(args[2]):
            return None
        return "❌ git revert 仅允许: git revert <revision> 或 git revert --no-commit <revision>"
    if subcommand == "worktree":
        if len(args) == 2 and args[1] == "list":
            return None
        if len(args) == 2 and args[1] == "prune":
            return None
        # worktree add <path> -b <branch> 或 worktree add <path> <branch>
        if len(args) >= 4 and args[1] == "add" and _is_safe_git_ref(args[2]):
            return None
        # worktree add -b <branch> <path>
        if len(args) >= 5 and args[1] == "add" and args[2] == "-b" and _is_safe_git_ref(args[3]) and _is_safe_git_ref(args[4]):
            return None
        if len(args) == 3 and args[1] == "add" and _is_safe_git_ref(args[2]):
            return None
        # worktree remove <path>
        if len(args) >= 3 and args[1] == "remove" and _is_safe_git_ref(args[2]):
            return None
        return "❌ git worktree 仅允许: list / add <path> [-b <branch>] / remove <path> / prune"
    if subcommand == "merge":
        if len(args) == 2 and _is_safe_git_ref(args[1]):
            return None
        if len(args) == 3 and args[1] in ("--no-ff", "--ff-only") and _is_safe_git_ref(args[2]):
            return None
        return "❌ git merge 仅允许: git merge <branch>"
    if subcommand == "checkout":
        if len(args) == 2 and _is_safe_git_ref(args[1]):
            return None
        # 允许 checkout -b <new-branch> 创建并切换
        if len(args) == 3 and args[1] == "-b" and _is_safe_git_ref(args[2]):
            return None
        return "❌ git checkout 仅允许: git checkout <branch> 或 git checkout -b <branch>"
    return None


def _is_safe_git_ref(value: str) -> bool:
    if not value or value.startswith("-"):
        return False
    blocked_chars = set(" \t\n\r:;|&<>`$\\")
    return not any(char in blocked_chars for char in value)


def _is_safe_single_revision(value: str) -> bool:
    if not _is_safe_git_ref(value):
        return False
    blocked_patterns = ("..", "^@", "^!", "^{")
    return not any(pattern in value for pattern in blocked_patterns)


def _current_branch(path: str = "") -> str:
    cwd = _resolve_repo(path)
    completed = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=cwd,
        env={
            **os.environ,
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GIT_PAGER": "cat",
        },
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    return (completed.stdout or "").strip()


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
def git_push(path: str = "", remote: str = "origin", branch: str = "", set_upstream: bool = False) -> str:
    """推送当前 Git 分支。只有用户明确要求推送代码时使用；不支持 force、tags、delete 等高风险参数。"""
    remote = (remote or "origin").strip()
    branch = (branch or "").strip()
    if not _is_safe_git_ref(remote):
        return "❌ remote 名称不安全或为空"
    if branch and not _is_safe_git_ref(branch):
        return "❌ branch 名称不安全"

    if set_upstream:
        if not branch:
            try:
                branch = _current_branch(path)
            except Exception as exc:
                return f"❌ 获取当前分支失败: {exc}"
        if not branch:
            return "❌ 当前不在普通分支上，无法设置 upstream"
        return _run_git(["push", "-u", remote, branch], path)

    if branch:
        return _run_git(["push", remote, branch], path)
    return _run_git(["push"], path)


@tool
def git_revert(revision: str, path: str = "", no_commit: bool = False) -> str:
    """回退指定 Git 提交。默认创建一个反向提交；no_commit=True 时只应用反向改动不提交，便于检查。"""
    revision = (revision or "").strip()
    if not _is_safe_single_revision(revision):
        return "❌ revision 不能为空，且不能包含危险字符"
    if no_commit:
        return _run_git(["revert", "--no-commit", revision], path)
    return _run_git(["revert", revision], path)


@tool
def git_command(command: str, path: str = "") -> str:
    """执行受限的 Git 命令。允许查看类命令、add/commit、受限 push/revert；不允许 pull/reset/restore。"""
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return f"❌ 命令解析失败: {exc}"
    if args and args[0] == "git":
        args = args[1:]
    return _run_git(args, path)


TOOLS = [
    git_status,
    git_diff,
    git_log,
    git_show,
    git_add,
    git_commit,
    git_commit_all,
    git_push,
    git_revert,
    git_command,
]
