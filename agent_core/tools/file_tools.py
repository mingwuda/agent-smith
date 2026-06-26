"""文件操作工具（高性能版）

支持：
- 按行区域读 / 按字节偏移随机读
- 全量写 / 按行插入 / 按行替换 / 按关键字替换
- 所有写入操作自动生成行级 diff
"""
import difflib
import json
import linecache
import mmap
import os
import re
from collections import defaultdict
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

# ── 行缓存：避免同一文件反复读取 ──
_line_cache: dict[str, tuple[list[str], float]] = {}
LINE_CACHE_TTL = 2.0  # 秒

# ── 工作区外编辑权限（按用户隔离） ──
_current_user: str = "default"
_outside_auths: dict[str, list[str]] = defaultdict(list)


def set_current_user(uid: str) -> None:
    global _current_user
    _current_user = uid


def add_outside_auth(uid: str, path_prefix: str) -> None:
    """授予某用户对指定路径前缀的编辑权限（仅当前进程生命周期有效）。"""
    normalized = Path(path_prefix).expanduser().resolve().as_posix()
    existing = _outside_auths[uid]
    # 不重复添加
    for p in existing:
        if normalized.startswith(Path(p).as_posix()):
            return
    _outside_auths[uid].append(normalized)


def is_path_permitted(target: Path) -> bool:
    """检查目标路径是否在授权白名单中。"""
    target_str = target.as_posix()
    for prefix in _outside_auths.get(_current_user, []):
        if target_str.startswith(prefix):
            return True
    return False


PERMISSION_PREFIX = "__PERMISSION_NEEDED__"  # 前端据此识别"需要授权"


def _invalidate_line_cache(path: str):
    _line_cache.pop(str(path), None)
    linecache.clearcache()


def _get_lines_cached(path: Path) -> list[str]:
    """从 linecache（Python 内置行缓存）读取所有行。"""
    p = str(path)
    raw = linecache.getlines(p)
    if not raw:
        # linecache 没命中，手动读一遍
        raw = path.read_text(encoding="utf-8").splitlines(keepends=True)
        linecache.updatecache(p)
    return raw


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
    allowed_prefixes = []
    if _workspace:
        project_root = _workspace.parent
        if (project_root / "skills").is_dir():
            allowed_prefixes.append(project_root / "skills")
        if (project_root / "agent_core" / "samples").is_dir():
            allowed_prefixes.append(project_root / "agent_core" / "samples")
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
    # 检查用户是否已授权此路径
    if is_path_permitted(target):
        return target
    raise ValueError(f"{PERMISSION_PREFIX}:{path} 路径超出工作区，需要用户授权。当前工作区: {workspace}")


def _path_error(exc: ValueError) -> str:
    return f"❌ {exc}"


def _display_path(path: Path) -> str:
    workspace = (_workspace or Path.home() / "agent_workspace").expanduser().resolve()
    try:
        return path.resolve(strict=False).relative_to(workspace).as_posix()
    except ValueError:
        return str(path)


def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


# ═══════════════════════════════════════════════════════════════
#  读操作
# ═══════════════════════════════════════════════════════════════

@tool
def read_file(path: str, start_line: int = 0, max_lines: int = 200) -> str:
    """读取文件内容（支持按行区段）。path 可为工作区相对路径，也可为绝对路径。

    参数:
      - start_line: 从第几行开始读（默认 0）
      - max_lines: 一次最多读多少行（默认 200）

    性能：使用 linecache 缓存，重复读取同一文件不会产生磁盘 I/O。

    示例:
      - read_file("agent.py")          → 读前 200 行
      - read_file("agent.py", 200)     → 读第 200 行开始的 200 行
      - read_file("agent.py", 0, 1000) → 读前 1000 行
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

    # 读全部行（linecache 缓存）
    try:
        all_lines = _get_lines_cached(full)
    except Exception as e:
        return f"❌ 读取失败: {e}"

    total_lines = len(all_lines)
    start_line = max(0, int(start_line or 0))
    max_lines = max(1, min(int(max_lines or 200), 5000))
    end_line = min(start_line + max_lines, total_lines)

    if start_line >= total_lines:
        return f"❌ 起始行 {start_line} 超出文件总行数 {total_lines}"

    lines = all_lines[start_line:end_line]
    body = "".join(lines).rstrip("\n")

    header = (
        f"📄 {rel}\n"
        f"行范围: {start_line}–{end_line} / {total_lines} 行 | "
        f"字符数: {sum(len(l) for l in lines)} | 字节数: {file_size}\n"
        f"{'─' * 60}\n"
    )

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
def read_bytes(path: str, offset: int = 0, length: int = 4096) -> str:
    """按字节偏移量随机读取文件内容（mmap 零拷贝）。适合大文件随机访问。

    参数:
      - offset: 起始字节偏移（从 0 开始）
      - length: 读取的字节数（默认 4096，最大 51200）

    示例:
      - read_bytes("data.bin", 1024, 512)  → 从第 1024 字节起读 512 字节
      - read_bytes("log.txt", 0, 10000)    → 读文件开头的 10000 字节
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
    offset = max(0, int(offset or 0))
    length = max(1, min(int(length or 4096), 51200))

    if offset >= file_size:
        return f"❌ 偏移量 {offset} 超出文件大小 {file_size}"

    try:
        with open(full, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                actual_len = min(length, file_size - offset)
                raw = m[offset:offset + actual_len]
                text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        return f"❌ mmap 读取失败: {e}"

    header = (
        f"📄 {rel}\n"
        f"字节范围: {offset}–{offset + actual_len} / {file_size}\n"
        f"{'─' * 60}\n"
    )
    footer = ""
    if offset + actual_len < file_size:
        footer = (
            f"\n{'─' * 60}\n"
            f"💡 仍有 {file_size - offset - actual_len} 字节未读，"
            f"可调用 read_bytes(\"{path}\", offset={offset + actual_len}) 继续"
        )

    return header + text + footer


# ═══════════════════════════════════════════════════════════════
#  写操作
# ═══════════════════════════════════════════════════════════════

@tool
def write_file(path: str, content: str) -> str:
    """写入内容到文件（UTF-8）。path 相对于工作区目录。自动创建父目录。"""
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)
    full.parent.mkdir(parents=True, exist_ok=True)
    old_for_diff = None
    if full.exists() and full.is_file():
        try:
            old_for_diff = full.read_text(encoding="utf-8")
        except Exception:
            pass
    full.write_text(content, encoding="utf-8")
    _invalidate_line_cache(str(full))
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
    _invalidate_line_cache(str(full))
    diff = _generate_diff(full, old + content, old_content_override=old)
    return f"✅ 已追加到 {path}（{len(content)} 字符）{diff}"


@tool
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """按关键字替换文件内容。支持精确替换和正则替换。

    参数:
      - old_string: 要替换的旧文本（支持正则表达式，以 re: 开头）
      - new_string: 新文本
      - replace_all: True=替换全部匹配；False=仅替换第一个匹配（默认）
    """
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)
    if not full.exists():
        return f"❌ 文件不存在: {path}"

    try:
        old_content = full.read_text(encoding="utf-8")
    except Exception as e:
        return f"❌ 读取失败: {e}"

    is_regex = old_string.startswith("re:")
    pattern = old_string[3:] if is_regex else re.escape(old_string)

    try:
        if replace_all:
            new_content = re.sub(pattern, new_string, old_content)
        else:
            new_content = re.sub(pattern, new_string, old_content, count=1)
    except re.error as e:
        return f"❌ 正则错误: {e}"

    if new_content == old_content:
        return f"⚠️ 未找到匹配: {old_string[:80]}"

    full.write_text(new_content, encoding="utf-8")
    _invalidate_line_cache(str(full))
    count = old_content.count(old_string) if not is_regex else len(re.findall(pattern, old_content))
    diff = _generate_diff(full, new_content, old_content_override=old_content)
    return f"✅ 已替换 {path}（替换了 {count} 处）{diff}"


@tool
def insert_lines(path: str, line_number: int, content: str) -> str:
    """在指定行号前插入内容。行号从 0 开始。

    参数:
      - line_number: 在此行之前插入（0=文件最开头，-1=追加到末尾）
      - content: 要插入的文本内容
    """
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)

    full.parent.mkdir(parents=True, exist_ok=True)

    old_content = ""
    if full.exists() and full.is_file():
        try:
            old_content = full.read_text(encoding="utf-8")
        except Exception:
            pass

    if not old_content and full.exists():
        return f"❌ 读取失败: {path}"

    if not old_content:
        # 新建空文件
        full.write_text(content, encoding="utf-8")
        _invalidate_line_cache(str(full))
        return f"✅ 已创建文件并写入 {path}（{len(content)} 字符）{_generate_diff(full, content)}"

    all_lines = old_content.splitlines(keepends=True)
    total = len(all_lines)
    ln = int(line_number)
    if ln < 0 or ln >= total:
        ln = total  # 追加到末尾

    new_content = "".join(all_lines[:ln]) + content + "".join(all_lines[ln:])
    if not new_content.endswith("\n"):
        new_content += "\n"

    full.write_text(new_content, encoding="utf-8")
    _invalidate_line_cache(str(full))
    diff = _generate_diff(full, new_content, old_content_override=old_content)
    return f"✅ 已在第 {line_number} 行前插入 {len(content)} 字符 {diff}"


@tool
def replace_lines(path: str, start_line: int, end_line: int, content: str) -> str:
    """替换指定行区间的全部内容。行号从 0 开始。

    参数:
      - start_line: 起始行号（包含）
      - end_line: 结束行号（不包含）。传 -1 表示替换到文件末尾
      - content: 替换后的新内容
    """
    try:
        full = _resolve(path)
    except ValueError as exc:
        return _path_error(exc)
    if not full.exists():
        return f"❌ 文件不存在: {path}"

    try:
        old_content = full.read_text(encoding="utf-8")
    except Exception as e:
        return f"❌ 读取失败: {e}"

    all_lines = old_content.splitlines(keepends=True)
    total = len(all_lines)
    s = max(0, int(start_line))
    e = total if int(end_line) < 0 else min(int(end_line), total)

    if s >= total:
        return f"❌ 起始行 {s} 超出文件总行数 {total}"
    if s >= e:
        return f"❌ 起始行 {s} 大于等于结束行 {e}"

    before = "".join(all_lines[:s])
    after = "".join(all_lines[e:])
    new_content = before + content + after
    if not new_content.endswith("\n"):
        new_content += "\n"

    full.write_text(new_content, encoding="utf-8")
    _invalidate_line_cache(str(full))
    diff = _generate_diff(full, new_content, old_content_override=old_content)
    return f"✅ 已替换 {path} 的第 {s}–{e} 行 {diff}"


# ═══════════════════════════════════════════════════════════════
#  常规文件管理
# ═══════════════════════════════════════════════════════════════

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
        _invalidate_line_cache(str(full))
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
    # Python 3.13+ 的 pathlib rglob 不支持非相对模式（以 / 或 ../ 开头）
    cleaned_pattern = pattern
    for prefix in ["/", "./", "../"]:
        while cleaned_pattern.startswith(prefix):
            cleaned_pattern = cleaned_pattern[len(prefix):]
    matches = list(target.rglob(cleaned_pattern))
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


TOOLS = [
    # 读
    read_file,
    read_bytes,
    # 写
    write_file,
    append_to_file,
    edit_file,
    insert_lines,
    replace_lines,
    # 管理
    list_files,
    delete_file,
    search_files,
    get_workspace_path,
]
