"""文件浏览器路由 — 安全浏览项目目录、读取文件内容、Git 变更查看"""
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Body
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["files"])

# 允许浏览的文件类型（白名单）
_BROWSABLE_EXTENSIONS = {
    # 文本/代码
    '.txt', '.md', '.json', '.yaml', '.yml', '.xml', '.toml', '.ini',
    '.cfg', '.conf', '.log', '.env', '.gitignore', '.dockerignore',
    '.html', '.css', '.js', '.ts', '.jsx', '.tsx', '.vue', '.svelte',
    '.py', '.java', '.c', '.cpp', '.h', '.hpp', '.go', '.rs', '.rb',
    '.php', '.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd', '.sql',
    '.csv', '.tsv', '.svg', '.graphql', '.proto', '.makefile', '.cmake',
    # 配置文件
    '.editorconfig', '.eslintrc', '.prettierrc', 'Dockerfile', 'Makefile',
}
# 最大读取文件大小（2MB）
_MAX_FILE_SIZE = 2 * 1024 * 1024


def _resolve_base_path(request: Request) -> Path:
    """从请求中解析用户可访问的根目录（项目目录或工作目录）"""
    from services.workspace import _workspace_for_user
    uid = getattr(request.state, "user_id", "default")
    # 如果传了 project_id，优先用项目的 directory_path
    return _workspace_for_user(uid)


def _resolve_allowed_root(request: Request, path: Optional[str] = None) -> Path:
    """
    解析允许访问的根目录。
    - path 为空：返回默认 workspace
    - path 为绝对路径：验证是否在 workspace 下（或直接使用该路径作为项目目录）
    - 返回安全的根路径对象
    """
    if not path or not path.strip():
        base = _resolve_base_path(request)
        if not base:
            base = Path.home()
        return base

    target = Path(path).resolve()

    # 安全检查：不允许路径穿越到敏感目录
    _dangerous_prefixes = ['/etc', '/usr', '/var', '/System', '/Library',
                           '/bin', '/sbin', '/Applications']
    for prefix in _dangerous_prefixes:
        try:
            if str(target).startswith(prefix):
                raise HTTPException(status_code=403, detail=f"禁止访问系统目录: {prefix}")
        except (ValueError, TypeError):
            pass

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")

    return target


def _is_text_file(file_path: Path) -> bool:
    """判断是否为可预览的文本文件"""
    ext = file_path.suffix.lower()
    if ext in _BROWSABLE_EXTENSIONS:
        return True
    # 无扩展名但较小的文件也尝试以文本打开
    if file_path.is_file() and file_path.stat().st_size < _MAX_FILE_SIZE and ext == '':
        return True
    return False


@router.get("/files/browse")
async def browse_directory(
    request: Request,
    path: str = Query("", description="要浏览的目录路径（相对或绝对）"),
    project_id: str = Query("", description="项目 ID，用于确定根目录"),
):
    """列出目录结构（树形数据），前端渲染为文件浏览器"""
    root = _resolve_allowed_root(request, path)

    if not root.is_dir():
        raise HTTPException(status_code=400, detail="指定路径不是目录")

    entries = []
    try:
        for item in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            # 跳过隐藏文件/目录（以 . 开头的）和 node_modules / __pycache__ 等
            _skip_names = {'.git', '__pycache__', 'node_modules', '.idea', '.vscode',
                          '.next', '.nuxt', 'dist', 'build', '.venv', 'venv',
                          '.DS_Store', 'Thumbs.db'}
            if item.name in _skip_names:
                continue
            stat = item.stat()
            entry = {
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
            if item.is_file():
                entry["ext"] = item.suffix.lower()
                entry["previewable"] = _is_text_file(item)
            entries.append(entry)

        return {
            "path": str(root),
            "name": root.name,
            "entries": entries,
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限访问: {path}")


@router.get("/files/read")
async def read_file(
    request: Request,
    path: str = Query(..., description="要读取的文件完整路径"),
    project_id: str = Query("", description="项目 ID"),
):
    """读取单个文件的内容（文本文件返回内容，二进制返回错误）"""
    from services.workspace import _workspace_for_user

    target = Path(path).resolve()

    # 安全检查：不允许路径穿越
    _dangerous_prefixes = ['/etc', '/usr', '/var', '/System', '/Library']
    for prefix in _dangerous_prefixes:
        try:
            if str(target).startswith(prefix):
                raise HTTPException(status_code=403, detail=f"禁止访问系统目录")
        except (ValueError, TypeError):
            pass

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"不是文件: {path}")

    size = target.stat().st_size
    if size > _MAX_FILE_SIZE:
        raise HTTPException(status_code=413,
                            detail=f"文件过大 ({size} 字节)，最大支持 {_MAX_FILE_SIZE // 1024 // 1024}MB")

    if not _is_text_file(target):
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {target.suffix}")

    try:
        # 尝试 UTF-8 编码，失败则尝试其他常见编码
        content = target.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        try:
            content = target.read_text(encoding='gbk')
        except UnicodeDecodeError:
            try:
                content = target.read_text(encoding='latin-1')
            except Exception:
                raise HTTPException(status_code=400, detail="无法解码文件内容")

    return {
        "path": str(target),
        "name": target.name,
        "content": content,
        "size": size,
        "lines": content.count('\n') + 1,
    }


def _run_git(repo_dir: str, *args: str, timeout: int = 15) -> tuple[str | None, str]:
    """在指定目录执行 git 命令，返回 (stdout, stderr)"""
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir] + list(args),
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
        return result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="未找到 git 命令，请确认系统已安装 git")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"Git 命令超时（{timeout}s）")


@router.get("/files/changes")
async def get_changed_files(
    request: Request,
    project_id: str = Query("", description="项目 ID"),
):
    """
    获取当前工作区相对于最近一次提交的变更文件列表。
    返回每个文件的变更状态（M/A/D/R/U 等）和路径。
    """
    from services.workspace import _workspace_for_user
    uid = getattr(request.state, "user_id", "default")
    base = _workspace_for_user(uid)

    # 如果传了 project_id 且项目有 directory_path，优先用项目目录
    if project_id and project_id.strip():
        import session_store as _ss
        project = _ss.get_project(uid, project_id)
        if project and project.get("directory_path"):
            base = Path(project["directory_path"])

    if not base or not base.is_dir():
        raise HTTPException(status_code=400, detail="无法确定项目根目录")

    # 确认是 Git 仓库
    _, err = _run_git(str(base), "rev-parse", "--is-inside-work-tree")
    if err:
        raise HTTPException(status_code=400, detail="当前目录不是 Git 仓库")

    stdout, _ = _run_git(str(base), "status", "--porcelain=v1")

    changes = []
    for line in (stdout or "").splitlines():
        if len(line) < 4:
            continue
        # 安全解析：用 split 分割 XY 和 path（兼容不同数量的分隔空格）
        # porcelain v1 格式为 "XY path"，但某些边界情况下空格数可能不固定
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        xy = parts[0].ljust(2)   # 保证长度为 2（单字符状态补空格）
        path_raw = parts[1]
        # 处理 rename 格式：old -> new
        if "\x00" in path_raw:
            parts_path = path_raw.split("\x00")
            old_path = parts_path[0]
            new_path = parts_path[1] if len(parts_path) > 1 else old_path
        elif " -> " in path_raw:
            old_path, new_path = (p.strip() for p in path_raw.split(" -> ", 1))
        else:
            old_path = new_path = path_raw

        status_map = {
            "M": "modified", "A": "added", "D": "deleted",
            "R": "renamed", "C": "copied", "U": "unmerged",
            "?": "untracked", "!": "ignored",
        }
        x_status = status_map.get(xy[0], "unknown") if xy[0].strip() else ""
        y_status = status_map.get(xy[1], "unknown") if xy[1].strip() else ""

        entry = {
            "path": new_path,
            "status": x_status or y_status,
            "index_status": x_status,
            "work_status": y_status,
            "raw_xy": xy,
        }
        if x_status == "renamed" or y_status == "renamed":
            entry["old_path"] = old_path
        changes.append(entry)

    # 排序：已跟踪的在前，未跟踪在后；同组按路径排序
    def sort_key(c):
        is_untracked = c["status"] == "untracked"
        return (is_untracked, c["path"].lower())

    changes.sort(key=sort_key)

    return {
        "repo_root": str(base),
        "total_changes": len(changes),
        "changes": changes,
    }


@router.get("/files/unpushed-count")
async def get_unpushed_count(
    request: Request,
    project_id: str = Query("", description="项目 ID"),
):
    """
    获取当前 Git 仓库未推送的提交数量。
    返回 {"unpushed_count": N}，N=0 表示没有未推送提交。
    """
    from services.workspace import _workspace_for_user
    uid = getattr(request.state, "user_id", "default")
    base = _workspace_for_user(uid)

    if project_id and project_id.strip():
        import session_store as _ss
        project = _ss.get_project(uid, project_id)
        if project and project.get("directory_path"):
            base = Path(project["directory_path"])

    if not base or not base.is_dir():
        raise HTTPException(status_code=400, detail="无法确定项目根目录")

    _, err = _run_git(str(base), "rev-parse", "--is-inside-work-tree")
    if err:
        raise HTTPException(status_code=400, detail="当前目录不是 Git 仓库")

    # 获取当前分支名
    branch, _ = _run_git(str(base), "rev-parse", "--abbrev-ref", "HEAD")
    if not branch:
        return {"unpushed_count": 0}

    # 检查是否有 upstream
    upstream, _ = _run_git(str(base), "rev-parse", "--abbrev-ref", "@{upstream}")
    if not upstream:
        # 没有 upstream，视为有未推送提交（需要先 push -u）
        return {"unpushed_count": -1}

    # 比较本地与远程的提交数
    count_out, _ = _run_git(str(base), "rev-list", "--count", "HEAD", "--not", upstream)
    try:
        count = int(count_out.strip()) if count_out.strip() else 0
    except ValueError:
        count = 0

    return {"unpushed_count": count}


def _parse_status(status_out: str) -> str | None:
    """从 `git status --porcelain` 的输出推断单个文件的有效状态。

    返回 'added' | 'deleted' | 'renamed' | 'copied' | 'unmerged' | 'modified' | None
    未跟踪（??）也视为 added —— 整份内容对仓库而言都是新增。
    """
    for line in (status_out or "").splitlines():
        if len(line) < 4:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        xy = parts[0].ljust(2)
        x, y = xy[0].strip(), xy[1].strip()
        if x == "A" or y == "A" or "?" in xy:
            return "added"
        if x == "D" or y == "D":
            return "deleted"
        if x == "R" or y == "R":
            return "renamed"
        if x == "C" or y == "C":
            return "copied"
        if x == "U" or y == "U":
            return "unmerged"
        if x or y:
            return "modified"
    return None


@router.get("/files/diff")
async def get_file_diff(
    request: Request,
    file_path: str = Query(..., description="要查看 diff 的文件相对或绝对路径"),
    staged: bool = Query(False, description="是否查看暂存区的 diff"),
    project_id: str = Query("", description="项目 ID"),
):
    """
    获取单个文件的 diff 内容。
    返回格式化的 diff 文本，前端可高亮展示。
    对「新增 / 未跟踪」文件（git diff 无输出）自动合成全量 diff，按「新增」展示。
    """
    from services.workspace import _workspace_for_user
    uid = getattr(request.state, "user_id", "default")
    base = _workspace_for_user(uid)

    if project_id and project_id.strip():
        import session_store as _ss
        project = _ss.get_project(uid, project_id)
        if project and project.get("directory_path"):
            base = Path(project["directory_path"])

    if not base or not base.is_dir():
        raise HTTPException(status_code=400, detail="无法确定项目根目录")

    _, err = _run_git(str(base), "rev-parse", "--is-inside-work-tree")
    if err:
        raise HTTPException(status_code=400, detail="当前目录不是 Git 仓库")

    # 1) 先确定该文件的 git 状态（未跟踪也视为新增）
    status_out, _ = _run_git(str(base), "status", "--porcelain=v1", "--", file_path)
    effective_status = _parse_status(status_out)

    # 2) 取 diff：未暂存优先，空则回退暂存区
    stdout, stderr = _run_git(str(base), "diff", "--", file_path)
    used_staged = False
    if not stdout:
        cached, _ = _run_git(str(base), "diff", "--cached", "--", file_path)
        if cached:
            stdout = cached
            used_staged = True

    # 3) 新增 / 未跟踪文件：git diff 无输出 → 整文件视为新增，合成全量 diff
    if not stdout and effective_status == "added":
        target = Path(base) / file_path
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在或已被删除")
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raise HTTPException(status_code=400, detail="无法读取文件内容")
        content_lines = raw.split("\n")
        if content_lines and content_lines[-1] == "":
            content_lines = content_lines[:-1]  # 去掉末尾换行产生的空行
        n = len(content_lines)
        synth = "@@ -0,0 +1,%d @@\n" % n
        for ln in content_lines:
            synth += "+" + ln + "\n"
        stdout = synth

    if not stdout and not stderr:
        if effective_status == "deleted":
            raise HTTPException(status_code=404, detail="文件已删除，无内容可显示")
        raise HTTPException(status_code=404, detail="该文件无变更（或文件不在仓库中）")

    lines = stdout.splitlines()
    # 解析统计信息
    stats = {"additions": 0, "deletions": 0}
    for line in lines:
        if line.startswith("+") and not line.startswith("+++"):
            stats["additions"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            stats["deletions"] += 1

    return {
        "file_path": file_path,
        "staged": used_staged,
        "diff_text": stdout,
        "stats": stats,
        "line_count": len(lines),
        "effective_status": effective_status or "modified",
        "is_new": effective_status == "added",
    }


@router.post("/files/track")
async def track_file(request: Request, payload: dict = Body(...)):
    """将未跟踪的文件加入 git 跟踪（git add -- <file_path>）。"""
    project_id = (payload or {}).get("project_id", "")
    file_path = (payload or {}).get("file_path", "")
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path 不能为空")
    base = _resolve_repo_root(request, project_id)
    target = (base / file_path).resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")
    _, err, code = _git_rc(str(base), "add", "--", str(target))
    if code != 0:
        return {"success": False, "output": err or "git add 失败"}
    return {"success": True, "output": f"已跟踪: {file_path}"}


@router.post("/files/untrack")
async def untrack_file(request: Request, payload: dict = Body(...)):
    """将已跟踪的文件从 git 索引移除（git rm --cached），保留工作区文件。"""
    project_id = (payload or {}).get("project_id", "")
    file_path = (payload or {}).get("file_path", "")
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path 不能为空")
    base = _resolve_repo_root(request, project_id)
    target = (base / file_path).resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")
    _, err, code = _git_rc(str(base), "rm", "--cached", "--", str(target))
    if code != 0:
        return {"success": False, "output": err or "git rm --cached 失败"}
    return {"success": True, "output": f"已取消跟踪: {file_path}"}


def _resolve_repo_root(request: Request, project_id: str) -> Path:
    """解析 project_id 对应的 git 仓库根目录（与 /files/changes 同源逻辑）。"""
    try:
        from services.workspace import _workspace_for_user
    except ImportError:
        from agent_core.services.workspace import _workspace_for_user
    uid = getattr(request.state, "user_id", "default")
    base = _workspace_for_user(uid)
    if project_id and project_id.strip():
        import session_store as _ss
        project = _ss.get_project(uid, project_id)
        if project and project.get("directory_path"):
            base = Path(project["directory_path"])
    if not base or not base.is_dir():
        raise HTTPException(status_code=400, detail="无法确定项目根目录")
    _, err = _run_git(str(base), "rev-parse", "--is-inside-work-tree")
    if err:
        raise HTTPException(status_code=400, detail="当前目录不是 Git 仓库")
    return base


def _llm_commit_message(diff_text: str, stat_text: str, untracked: list) -> Optional[str]:
    """用 LLM 基于 diff 生成提交信息；失败返回 None（调用方降级为规则生成）。"""
    try:
        from config import AgentConfig
    except ImportError:
        from agent_core.config import AgentConfig
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
        cfg = AgentConfig.load()
        api_key = cfg.api_key or "sk-no-key-required"
        llm = ChatOpenAI(
            model=cfg.model,
            api_key=api_key,
            base_url=cfg.base_url or None,
            temperature=0,
            timeout=30,
        )
        diff_clip = (diff_text or "")[:8000]
        untracked_str = "\n".join("- " + u for u in (untracked or [])) or "（无）"
        prompt = (
            "你是一个资深的 Git 提交信息生成器。根据下面的代码改动，生成一条简洁、准确的中文提交信息，"
            "遵循 Conventional Commits 风格（如 feat/fix/refactor/docs/chore 等 + 简短描述，必要时可补一行正文）。"
            "只输出提交信息本身，不要解释、不要使用引号或代码块包裹。\n\n"
            "变更统计:\n" + (stat_text or "（无）") + "\n\n"
            "未跟踪的新文件:\n" + untracked_str + "\n\n"
            "diff (节选):\n" + diff_clip
        )
        resp = llm.invoke([HumanMessage(content=prompt)])
        msg = (getattr(resp, "content", "") or "").strip().strip('"').strip("'").strip("`").strip()
        return msg or None
    except Exception as e:
        logger.warning("[generate-commit-message] LLM 生成失败，降级规则生成: %s", e)
        return None


def _rule_commit_message(stat_text: str, untracked: list) -> str:
    """规则降级：基于变更统计生成朴素提交信息。"""
    changed = [l for l in (stat_text or "").splitlines() if "|" in l]
    total = len(changed) + len(untracked or [])
    if total == 0:
        return "chore: 更新代码"
    return f"chore: 更新 {total} 个文件"


def _git_rc(repo: str, *args: str, timeout: int = 15) -> tuple[str, str, int]:
    """执行 git 命令并返回 (stdout, stderr, returncode)，用 returncode 判成功（不被 stderr 回显误导）。"""
    try:
        r = subprocess.run(
            ["git", "-C", repo] + list(args),
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="未找到 git 命令，请确认系统已安装 git")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"Git 命令超时（{timeout}s）")


def _commit_at(repo: str, message: str) -> tuple[bool, str]:
    """git add -A + git commit，返回 (是否成功, 输出文本)。"""
    _, add_err, add_code = _git_rc(repo, "add", "-A")
    if add_code != 0 and add_err:
        return False, add_err
    commit_out, commit_err, commit_code = _git_rc(repo, "commit", "-m", message)
    if commit_code != 0:
        return False, (commit_err or "提交失败（可能无改动可提交）")
    return True, commit_out or "已提交"


def _push_at(repo: str) -> tuple[bool, str]:
    """git push（origin 当前分支）；若无 upstream 自动 -u origin <当前分支>。返回 (是否成功, 输出)。"""
    # 检查当前分支是否已配置 upstream
    up_out, _, up_code = _git_rc(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if up_code != 0 or not up_out.strip():
        # 无 upstream → 自动设置 -u origin <branch>
        branch_out, _, _ = _git_rc(repo, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch_out.strip() or "main"
        rem_out, _, _ = _git_rc(repo, "remote", "get-url", "origin")
        if not rem_out.strip():
            return False, "未配置 origin 远程仓库，无法推送（请先 git remote add origin <url> 或手动推送）"
        _, push_err, push_code = _git_rc(repo, "push", "-u", "origin", branch, timeout=60)
        if push_code != 0:
            return False, (push_err or "推送失败")
        return True, f"已推送至 origin/{branch}"
    # 已配置 upstream → 直接推送
    _, push_err, push_code = _git_rc(repo, "push", timeout=60)
    if push_code != 0:
        return False, (push_err or "推送失败")
    return True, "已推送"


@router.post("/files/generate-commit-message")
async def generate_commit_message(request: Request, payload: dict = Body(...)):
    """基于当前未提交的改动，生成一条提交信息（LLM 优先，失败降级规则）。"""
    project_id = (payload or {}).get("project_id", "")
    base = _resolve_repo_root(request, project_id)

    stat_out, _ = _run_git(str(base), "diff", "HEAD", "--stat")
    diff_out, _ = _run_git(str(base), "diff", "HEAD")
    status_out, _ = _run_git(str(base), "status", "--porcelain=v1")
    untracked = []
    for line in (status_out or "").splitlines():
        if len(line) >= 2 and line[0] == "?" and line[1] == "?":
            parts = line.split(None, 1)
            if len(parts) >= 2:
                untracked.append(parts[1])

    message = _llm_commit_message(diff_out, stat_out, untracked)
    if not message:
        message = _rule_commit_message(stat_out, untracked)
    return {"message": message}


@router.post("/files/commit")
async def commit_changes(request: Request, payload: dict = Body(...)):
    """暂存所有改动并提交（不推送）。"""
    project_id = (payload or {}).get("project_id", "")
    message = (payload or {}).get("message", "")
    if not (message or "").strip():
        raise HTTPException(status_code=400, detail="提交信息不能为空")
    base = _resolve_repo_root(request, project_id)
    ok, output = _commit_at(str(base), message.strip())
    return {"success": ok, "output": output}


@router.post("/files/commit-and-push")
async def commit_and_push(request: Request, payload: dict = Body(...)):
    """暂存、提交并推送到远程。"""
    project_id = (payload or {}).get("project_id", "")
    message = (payload or {}).get("message", "")
    if not (message or "").strip():
        raise HTTPException(status_code=400, detail="提交信息不能为空")
    base = _resolve_repo_root(request, project_id)
    ok, output = _commit_at(str(base), message.strip())
    if not ok:
        return {"success": False, "output": "提交失败：\n" + output}
    pushed, pout = _push_at(str(base))
    return {"success": pushed, "output": output + "\n\n" + (pout or "")}


@router.post("/files/push")
async def push_commits(request: Request, payload: dict = Body(...)):
    """推送当前分支到远程（不提交，只 push）。"""
    project_id = (payload or {}).get("project_id", "")
    base = _resolve_repo_root(request, project_id)
    pushed, output = _push_at(str(base))
    return {"success": pushed, "output": output}
