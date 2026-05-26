"""文件操作工具"""
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

# 工作区由调用方注入
_workspace: Optional[Path] = None

def set_workspace(path: Path):
    global _workspace
    _workspace = path.expanduser().resolve()
    _workspace.mkdir(parents=True, exist_ok=True)

def _resolve(path: str, allow_outside: bool = False) -> Path:
    workspace = (_workspace or Path.home() / "agent_workspace").expanduser().resolve()
    raw = Path(path or ".").expanduser()
    target = raw if raw.is_absolute() else workspace / raw
    target = target.resolve(strict=False)
    if allow_outside:
        return target
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"路径超出工作区: {path}。当前工作区: {workspace}") from exc
    return target

def _path_error(exc: ValueError) -> str:
    return f"❌ {exc}"

@tool
def read_file(path: str) -> str:
    """读取文件内容。path 可为工作区相对路径，也可为绝对路径。"""
    try:
        full = _resolve(path, allow_outside=True)
    except ValueError as exc:
        return _path_error(exc)
    if not full.exists():
        return f"❌ 文件不存在: {path}"
    if not full.is_file():
        return f"❌ 不是文件: {path}"
    return full.read_text(encoding="utf-8")

@tool
def write_file(path: str, content: str) -> str:
    """写入内容到文件（UTF-8）。path 相对于工作区目录。自动创建父目录。"""
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    size = len(content)
    return f"✅ 已写入 {path}（{size} 字符，{full.stat().st_size} 字节）"

@tool
def append_to_file(path: str, content: str) -> str:
    """追加内容到文件末尾。path 相对于工作区目录。"""
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)
    full.parent.mkdir(parents=True, exist_ok=True)
    with open(full, "a", encoding="utf-8") as f:
        f.write(content)
    return f"✅ 已追加到 {path}（{len(content)} 字符）"

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


TOOLS = [read_file, write_file, append_to_file, list_files, delete_file, search_files, get_workspace_path]
