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
from contextvars import ContextVar
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

# 工作区由调用方注入（ContextVar：每个 async 请求/任务各自独立，避免并发串目录）
_workspace_ctx: ContextVar[Optional[Path]] = ContextVar("file_tools_workspace", default=None)
MAX_FILE_RETURN_CHARS = 20000
FILE_HEAD_CHARS = 8000
FILE_TAIL_CHARS = 8000

DIFF_MARKER = "__DIFF__:"
DIFF_MAX_LINES = 500   # 最多展示 500 行 diff（按显示行计算）
DIFF_CONTEXT_LINES = 3 # 每个改动块前后的上下文行数

# ── 行缓存：避免同一文件反复读取 ──
_line_cache: dict[str, tuple[list[str], float]] = {}
LINE_CACHE_TTL = 2.0  # 秒

# ── 工作区外编辑权限（按用户隔离） ──
_current_user_ctx: ContextVar[str] = ContextVar("file_tools_user", default="default")
_outside_auths: dict[str, list[str]] = defaultdict(list)


def set_current_user(uid: str) -> None:
    _current_user_ctx.set(uid)


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
    for prefix in _outside_auths.get(_current_user_ctx.get(), []):
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
    _workspace_ctx.set(path.expanduser().resolve())
    _workspace_ctx.get().mkdir(parents=True, exist_ok=True)


def resolve_workspace() -> Path:
    """返回当前真实工作区路径（供其他模块引用）"""
    return _workspace_ctx.get() or Path.home() / "agent_workspace"


def _generate_diff(file_path: Path, new_content: str, old_content_override: Optional[str] = None) -> str:
    """生成行级 diff JSON，通过 __DIFF__ 标记嵌入返回值尾。

    改进策略：以改动为中心，只展示实际变更行 + 上下文窗口，
    避免大文件（>500 行）因截断导致 +0/-0 的问题。
    """
    old_content = old_content_override
    if old_content is None and file_path.exists() and file_path.is_file():
        try:
            old_content = file_path.read_text(encoding="utf-8")
        except Exception:
            pass

    # 新文件：全部视为新增
    if old_content is None:
        new_lines = new_content.splitlines()
        added = len(new_lines)
        diff = [{"t": "+", "c": l} for l in new_lines[:DIFF_MAX_LINES]]
        if len(new_lines) > DIFF_MAX_LINES:
            diff.append({"t": "…", "c": f"... 还有 {len(new_lines) - DIFF_MAX_LINES} 行未显示"})
        return _diff_payload(added, 0, diff)

    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()

    # ── 使用 SequenceMatcher 精确定位变更块 ──
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    opcodes = sm.get_opcodes()  # [(tag, i1, i2, j1, j2), ...]

    total_added = 0
    total_removed = 0
    display_diff: list[dict] = []

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            # 不变区域：只在接近上一个/下一个变动块时展示上下文
            block_len = i2 - i1
            if block_len <= 2 * DIFF_CONTEXT_LINES + 2:
                # 块太小，全量展示
                for k in range(i1, i2):
                    display_diff.append({"t": " ", "c": old_lines[k]})
            else:
                # 只展示头部上下文
                for k in range(i1, min(i1 + DIFF_CONTEXT_LINES, i2)):
                    display_diff.append({"t": " ", "c": old_lines[k]})
                # 中间省略
                skipped = block_len - 2 * DIFF_CONTEXT_LINES
                if skipped > 0:
                    display_diff.append({"t": "…", "c": f"  ... (省略 {skipped} 行未变内容) ..."})
                # 展示尾部上下文
                for k in range(max(i1 + DIFF_CONTEXT_LINES, i2 - DIFF_CONTEXT_LINES), i2):
                    display_diff.append({"t": " ", "c": old_lines[k]})
        elif tag == "replace":
            total_removed += (i2 - i1)
            total_added += (j2 - j1)
            for k in range(i1, i2):
                display_diff.append({"t": "-", "c": old_lines[k]})
            for k in range(j1, j2):
                display_diff.append({"t": "+", "c": new_lines[k]})
        elif tag == "delete":
            total_removed += (i2 - i1)
            for k in range(i1, i2):
                display_diff.append({"t": "-", "c": old_lines[k]})
        elif tag == "insert":
            total_added += (j2 - j1)
            for k in range(j1, j2):
                display_diff.append({"t": "+", "c": new_lines[k]})

        # 提前截断显示区，但保留完整的统计计数
        if len(display_diff) >= DIFF_MAX_LINES:
            remaining_ops = len([op for op in opcodes if op[0] != "equal"])  # 粗略估计
            display_diff = display_diff[:DIFF_MAX_LINES]
            display_diff.append({"t": "…",
                                 "c": f"... 还有更多变更（仅展示了前 {DIFF_MAX_LINES} 行）"})
            break

    return _diff_payload(total_added, total_removed, display_diff)


def _diff_payload(added: int, removed: int, diff: list[dict]) -> str:
    """将 diff 数据序列化为 __DIFF__ 标记字符串。"""
    payload = json.dumps(
        {"added": added, "removed": removed, "diff": diff},
        ensure_ascii=False,
    )
    return f"\n{DIFF_MARKER}{payload}"


def _resolve(path: str, allow_outside: bool = False) -> Path:
    _ws = _workspace_ctx.get()
    workspace = (_ws or Path.home() / "agent_workspace").expanduser().resolve()
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
    if _ws:
        project_root = _ws.parent
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


def _display_path(path: Path, display_root: Optional[Path] = None) -> str:
    _ws = _workspace_ctx.get()
    workspace = (_ws or Path.home() / "agent_workspace").expanduser().resolve()
    base = (display_root or workspace).resolve(strict=False)
    try:
        return path.resolve(strict=False).relative_to(base).as_posix()
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
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False, occurrence: int = 0) -> str:
    """按文本（或正则）替换文件内容。修改代码/文本时优先使用本工具，不要改用 run_python 执行脚本去改写文件（更慢且不易校验）。

    参数:
      - old_string: 要查找并替换的旧文本。普通文本按字面匹配；以 re: 开头则按正则表达式匹配。
      - new_string: 替换后的新文本。
      - replace_all: True=替换全部匹配；False=只替换一处（默认）。
      - occurrence:  仅当 replace_all=False 时生效。指定替换第几处匹配（1=第一处，2=第二处…）。
                     为 0（默认）时要求 old_string 在文件中唯一：若出现多处会明确报错并提示匹配数量，
                     避免误改到非目标位置。此时可：① 补充上下文让 old_string 唯一；
                     ② 设 replace_all=True 替换全部；③ 设 occurrence=k 指定第 k 处（1–N）。

    提示：
      - 让 old_string 足够长且带上唯一上下文（前后各几行），可避免“不唯一”报错。
      - 跨多处的大段重构，可多次调用本工具，每次给出明确且唯一的 old_string。
      - old_string 与文件中空白/缩进/换行必须完全一致；不确定时先用 read_file 核对原文。
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
    raw_pattern = old_string[3:] if is_regex else old_string
    pattern = raw_pattern if is_regex else re.escape(raw_pattern)

    try:
        matches = list(re.finditer(pattern, old_content))
    except re.error as e:
        return f"❌ 正则错误: {e}"

    n = len(matches)
    if n == 0:
        return (
            f"⚠️ 未找到匹配: {raw_pattern[:80]!r}\n"
            f"请检查 old_string 的空白/缩进/换行是否与文件完全一致；或改用 re: 前缀走正则；"
            f"也可先用 read_file 核对原文。"
        )

    if replace_all:
        targets = matches
    elif occurrence == 0:
        if n > 1:
            preview = matches[0].group(0)[:60].replace("\n", "\\n")
            return (
                f"⚠️ old_string 在文件中出现了 {n} 处，不唯一，无法确认要改哪一处。\n"
                f"做法三选一：① 补充更多上下文让 old_string 唯一；② 设 replace_all=True 替换全部；"
                f"③ 设 occurrence=k 指定第 k 处（合法范围 1–{n}）。\n"
                f"首处匹配预览：{preview!r}"
            )
        targets = matches  # 唯一，直接替换
    else:
        if occurrence < 1 or occurrence > n:
            return f"⚠️ occurrence={occurrence} 超出范围，文件中共有 {n} 处匹配（合法范围 1–{n}）。"
        targets = [matches[occurrence - 1]]

    # 逐段拼接，仅替换目标匹配（精确替换指定位置，不改变其他内容/换行）
    # 正则模式下用 m.expand 展开反向引用（\1 等），与旧 re.sub 行为一致；普通模式按字面插入。
    parts: list[str] = []
    last = 0
    for m in targets:
        parts.append(old_content[last:m.start()])
        parts.append(m.expand(new_string) if is_regex else new_string)
        last = m.end()
    parts.append(old_content[last:])
    new_content = "".join(parts)

    if new_content == old_content:
        return f"⚠️ 替换成功但内容未变化（new_string 与匹配文本相同），共 {n} 处匹配未实际改动。"

    full.write_text(new_content, encoding="utf-8")
    _invalidate_line_cache(str(full))

    replaced = len(targets)
    first_start = targets[0].start()
    first_line = old_content.count("\n", 0, first_start) + 1  # 1-based 行号
    line_hint = f"，位于第 {first_line} 行" if replaced == 1 else f"，首处位于第 {first_line} 行"
    diff = _generate_diff(full, new_content, old_content_override=old_content)
    return f"✅ 已替换 {path}（替换了 {replaced} 处{line_hint}）{diff}"


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
def search_files(pattern: str, path: str = "", content: bool = False) -> str:
    """递归搜索文件名或文件内容。

    参数:
      - pattern: 搜索模式。content=False 时按文件名 glob 匹配（如 *.py, *test*）；
        content=True 时按文件内容关键词匹配。
      - path: 搜索目录，默认为当前工作区。
      - content: 是否搜索文件内容而非文件名。

    返回: 匹配结果列表，包含文件路径和匹配行信息。
    """
    try:
        target = _resolve(path, allow_outside=True)
    except ValueError as exc:
        return _path_error(exc)

    # ── 单文件搜索 ──
    if target.is_file():
        if not content:
            # 文件名搜索：检查文件名是否匹配 pattern
            import fnmatch
            if fnmatch.fnmatch(target.name, pattern):
                size = _fmt_size(target.stat().st_size)
                return f"找到 1 个匹配:\n  {target.name}  {size}"
            return f"未找到匹配 '{pattern}' 的文件"

        # 内容搜索：直接在单文件上 grep
        return _grep_file_content(pattern, target, display_root=target.parent)

    # ── 目录搜索 ──
    if not target.is_dir():
        return f"❌ 路径不存在或不是目录: {path or '/'}"

    display_root = target

    if not content:
        # ── 文件名搜索（原有行为） ──
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

    # ── 内容搜索（目录） ──
    return _grep_dir_content(pattern, target, display_root)


def _grep_file_content(pattern: str, file_path: Path, display_root: Path) -> str:
    """在单个文件上执行内容搜索。支持扩展正则（如 a|b）；失败时回退到字面量搜索。"""
    if not pattern.strip():
        return "❌ 内容搜索需要提供非空关键词"

    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"❌ 读取文件失败: {e}"

    lines = text.splitlines()
    regex = None
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = None

    matches = []
    for idx, line in enumerate(lines, start=1):
        hit = False
        if regex is not None:
            hit = bool(regex.search(line))
        else:
            hit = pattern.lower() in line.lower()
        if hit:
            rel = _display_path(file_path, display_root)
            matches.append(f"  {rel}:{idx}: {line}")
            if len(matches) >= 10:
                break

    if not matches:
        return f"未找到包含 '{pattern}' 的文件内容"

    header = f"找到 1 个文件包含 '{pattern}'（共 {len(matches)} 处匹配，仅展示前 10 条）:\n"
    return header + "\n".join(matches)


def _grep_dir_content(pattern: str, target: Path, display_root: Path) -> str:
    """在目录上执行递归内容搜索。"""
    import subprocess
    import shutil

    if not pattern.strip():
        return "❌ 内容搜索需要提供非空关键词"

    grep = shutil.which("grep")
    if not grep:
        return "❌ 当前环境缺少 grep，无法执行内容搜索"

    cmd = [
        grep,
        "-riIn",
        "--binary-files=without-match",
        "--exclude-dir=.venv",
        "--exclude-dir=node_modules",
        "--exclude-dir=__pycache__",
        "--exclude-dir=.git",
        "--exclude-dir=.idea",
        "--exclude-dir=.vscode",
        "--exclude-dir=dist",
        "--exclude-dir=build",
        "--exclude-dir=.egg-info",
        pattern,
        str(target),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "❌ 内容搜索超时（30 秒）"
    except Exception as e:
        return f"❌ 内容搜索失败: {e}"

    stdout = proc.stdout.strip()
    if not stdout:
        return f"未找到包含 '{pattern}' 的文件内容"

    lines = stdout.splitlines()
    file_hits: dict[str, list[str]] = {}
    for line in lines:
        if ":" not in line:
            continue
        file_path, rest = line.split(":", 1)
        try:
            lineno, _ = rest.split(":", 1)
        except ValueError:
            lineno = "?"
        rel = _display_path(Path(file_path), display_root)
        if rel not in file_hits:
            file_hits[rel] = []
        if len(file_hits[rel]) < 10:
            file_hits[rel].append(f"  {rel}:{lineno}: {rest}")

    display = []
    total_files = len(file_hits)
    for rel, hits in file_hits.items():
        display.extend(hits)
        if len(hits) >= 10:
            display.append(f"  ... {rel} 还有更多匹配")

    header = f"找到 {total_files} 个文件包含 '{pattern}'（共 {len(lines)} 处匹配）:\n"
    return header + "\n".join(display[:100])


@tool
def get_workspace_path() -> str:
    """返回当前工作区目录的绝对路径"""
    return str(_workspace_ctx.get() or Path.home() / "agent_workspace")


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
