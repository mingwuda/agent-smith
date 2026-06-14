"""文件操作工具"""
import difflib
import json
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

# 工作区由调用方注入
_workspace: Optional[Path] = None
MAX_FILE_RETURN_CHARS = 20000
FILE_HEAD_CHARS = 8000
FILE_TAIL_CHARS = 8000

DIFF_MARKER = "__DIFF__:"
DIFF_MAX_LINES = 500  # 最多保留 500 行 diff，避免 SSE 事件过大

def set_workspace(path: Path):
    global _workspace
    _workspace = path.expanduser().resolve()
    _workspace.mkdir(parents=True, exist_ok=True)


def resolve_workspace() -> Path:
    """返回当前真实工作区路径（供其他模块引用）"""
    return _workspace or Path.home() / "agent_workspace"


def _generate_diff(file_path: Path, new_content: str, old_content_override: Optional[str] = None) -> str:
    """生成行级 diff JSON，通过 __DIFF__ 标记嵌入返回值尾。"""
    old_content = old_content_override
    if old_content is None and file_path.exists() and file_path.is_file():
        try:
            old_content = file_path.read_text(encoding="utf-8")
        except Exception:
            pass

    if old_content is None:
        new_lines = new_content.splitlines()
        added = len(new_lines)
        diff = [{"t": "+", "c": l} for l in new_lines[:DIFF_MAX_LINES]]
    else:
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        differ = difflib.Differ()
        diff_gen = differ.compare(old_lines, new_lines)
        diff = []
        added = 0
        removed = 0
        for line in diff_gen:
            if len(diff) >= DIFF_MAX_LINES:
                diff.append({"t": "…", "c": f"... 还有更多变更（仅展示了前 {DIFF_MAX_LINES} 行）"})
                break
            if line.startswith("  "):
                diff.append({"t": " ", "c": line[2:]})
            elif line.startswith("+ "):
                diff.append({"t": "+", "c": line[2:]})
                added += 1
            elif line.startswith("- "):
                diff.append({"t": "-", "c": line[2:]})
                removed += 1
            elif line.startswith("? "):
                continue  # 跳过差异提示行

    payload = json.dumps(
        {"added": added, "removed": removed, "diff": diff},
        ensure_ascii=False,
    )
    return f"\n{DIFF_MARKER}{payload}"

def _resolve(path: str, allow_outside: bool = False) -> Path:
    workspace = (_workspace or Path.home() / "agent_workspace").expanduser().resolve()
    raw = Path(path or ".").expanduser()
    target = raw if raw.is_absolute() else workspace / raw
    target = target.resolve(strict=False)
    if allow_outside:
        return target
    # 检查是否在工作区内
    try:
        target.relative_to(workspace)
    except ValueError:
        pass
    else:
        return target
    # 检查是否在允许写入的白名单路径中
    # 用于 skills/ 等项目目录在 Docker 等环境下也能被写入
    allowed_prefixes = []
    if _workspace:
        # 项目根目录（workspace 的父级或邻近目录）
        project_root = _workspace.parent
        if (project_root / "skills").is_dir():
            allowed_prefixes.append(project_root / "skills")
        if (project_root / "agent_core" / "samples").is_dir():
            allowed_prefixes.append(project_root / "agent_core" / "samples")
        # Docker 环境下的 /app/skills/
        if Path("/app/skills").is_dir():
            allowed_prefixes.append(Path("/app/skills"))
        if Path("/app/agent_core/samples").is_dir():
            allowed_prefixes.append(Path("/app/agent_core/samples"))
    for prefix in allowed_prefixes:
        try:
            target.relative_to(prefix)
        except ValueError:
            continue
        else:
            return target
    raise ValueError(f"路径超出工作区: {path}。当前工作区: {workspace}")

def _path_error(exc: ValueError) -> str:
    return f"❌ {exc}"

@tool
def read_file(path: str, start_line: int = 0, max_lines: int = 200) -> str:
    """读取文件内容（支持分段）。path 可为工作区相对路径，也可为绝对路径。

    参数:
      - start_line: 从第几行开始读（默认 0）
      - max_lines: 一次最多读多少行（默认 200，避免大文件塞爆上下文）

    示例:
      - read_file("agent.py")  → 读前 200 行
      - read_file("agent.py", 200, 300)  → 读 200-500 行
      - read_file("agent.py", 0, 1000)  → 读前 1000 行（显式大窗口）
    """
    try:
        full = _resolve(path, allow_outside=True)
    except ValueError as exc:
        return _path_error(exc)
    if not full.exists():
        return f"❌ 文件不存在: {path}"
    if not full.is_file():
        return f"❌ 不是文件: {path}"

    rel = _display_path(full)
    file_size = full.stat().st_size

    # 1. 文件总行数（用二分文件估计，避免大文件开销）
    try:
        with full.open("rb") as f:
            total_lines = sum(1 for _ in f)
    except Exception as e:
        return f"❌ 读取失败: {e}"

    # 2. 规范化参数
    start_line = max(0, int(start_line or 0))
    max_lines = max(1, min(int(max_lines or 200), 5000))  # 单次最多 5000 行
    end_line = min(start_line + max_lines, total_lines)

    if start_line >= total_lines:
        return f"❌ 起始行 {start_line} 超出文件总行数 {total_lines}"

    # 3. 用 seek 高效定位 + 逐行读区间
    try:
        lines = []
        with full.open("rb") as f:
            for lineno in range(start_line):
                f.readline()  # 跳过
            for lineno in range(start_line, end_line):
                line = f.readline()
                if not line:
                    break
                lines.append(line.decode("utf-8", errors="replace").rstrip("\n"))
    except Exception as e:
        return f"❌ 读取失败: {e}"

    header = (
        f"📄 {rel}\n"
        f"行范围: {start_line}–{end_line} / {total_lines} 行 | "
        f"字符数: {sum(len(l) for l in lines)} | 字节数: {file_size}\n"
        f"{'─' * 60}\n"
    )
    body = "\n".join(lines)

    # 4. 区间内有更多行时给出提示
    footer = ""
    if end_line < total_lines:
        footer = (
            f"\n{'─' * 60}\n"
            f"💡 仍有 {total_lines - end_line} 行未读，"
            f"可调用 read_file(\"{path}\", start_line={end_line}, max_lines={max_lines}) 继续"
        )
    elif start_line > 0:
        footer = f"\n{'─' * 60}\n💡 已读至文件末尾"

    return header + body + footer

@tool
def write_file(path: str, content: str) -> str:
    """写入内容到文件（UTF-8）。path 相对于工作区目录。自动创建父目录。"""
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)
    full.parent.mkdir(parents=True, exist_ok=True)
    # 写入前保存旧内容用于 diff
    old_for_diff = None
    if full.exists() and full.is_file():
        try:
            old_for_diff = full.read_text(encoding="utf-8")
        except Exception:
            pass
    full.write_text(content, encoding="utf-8")
    size = len(content)
    diff = _generate_diff(full, content, old_content_override=old_for_diff)
    return f"✅ 已写入 {path}（{size} 字符，{full.stat().st_size} 字节）{diff}"

@tool
def append_to_file(path: str, content: str) -> str:
    """追加内容到文件末尾。path 相对于工作区目录。"""
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)
    full.parent.mkdir(parents=True, exist_ok=True)
    old = ""
    if full.exists() and full.is_file():
        try:
            old = full.read_text(encoding="utf-8")
        except Exception:
            pass
    with open(full, "a", encoding="utf-8") as f:
        f.write(content)
    diff = _generate_diff(full, old + content, old_content_override=old)
    return f"✅ 已追加到 {path}（{len(content)} 字符）{diff}"

@tool
def list_files(path: str = "") -> str:
    """列出文件和文件夹。path 为空则列工作区根目录；也支持绝对路径。"""
    try:
        target = _resolve(path, allow_outside=True)
    except ValueError as exc:
        return _path_error(exc)
    if not target.exists():
        return f"❌ 目录不存在: {path or '/'}"
    if not target.is_dir():
        return f"❌ 不是目录: {path}"
    
    items = []
    for f in sorted(target.iterdir()):
        icon = "📁" if f.is_dir() else "📄"
        size = f.stat().st_size if f.is_file() else 0
        if f.is_file():
            items.append(f"{icon} {f.name}  ({_fmt_size(size)})")
        else:
            items.append(f"{icon} {f.name}/")
    return "\n".join(items) if items else "（空目录）"

@tool
def delete_file(path: str) -> str:
    """删除文件或空目录。path 相对于工作区目录。"""
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)
    if not full.exists():
        return f"❌ 不存在: {path}"
    if full.is_file():
        full.unlink()
        return f"✅ 已删除文件: {path}"
    if full.is_dir():
        try:
            full.rmdir()
            return f"✅ 已删除空目录: {path}"
        except OSError:
            return f"❌ 目录非空，无法删除: {path}"

@tool
def search_files(pattern: str, path: str = "") -> str:
    """递归搜索匹配的文件名（支持通配符如 *.py, *test*）。path 可为绝对路径。"""
    try:
        target = _resolve(path, allow_outside=True)
    except ValueError as exc:
        return _path_error(exc)
    if not target.is_dir():
        return f"❌ 目录不存在: {path or '/'}"
    
    display_root = target
    matches = list(target.rglob(pattern))
    if not matches:
        return f"未找到匹配 '{pattern}' 的文件"
    
    lines = []
    for f in matches[:50]:
        resolved = f.resolve(strict=False)
        try:
            rel = resolved.relative_to(display_root)
        except ValueError:
            rel = resolved
        size = _fmt_size(f.stat().st_size) if f.is_file() else ""
        lines.append(f"  {rel}  {size}")
    
    if len(matches) > 50:
        lines.append(f"  ... 还有 {len(matches) - 50} 个匹配")
    
    return f"找到 {len(matches)} 个匹配:\n" + "\n".join(lines)

@tool
def get_workspace_path() -> str:
    """返回当前工作区目录的绝对路径"""
    return str(_workspace or Path.home() / "agent_workspace")


def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _display_path(path: Path) -> str:
    workspace = (_workspace or Path.home() / "agent_workspace").expanduser().resolve()
    try:
        return path.resolve(strict=False).relative_to(workspace).as_posix()
    except ValueError:
        return str(path)


TOOLS = [read_file, write_file, append_to_file, list_files, delete_file, search_files, get_workspace_path]
