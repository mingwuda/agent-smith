"""代码执行工具"""
import difflib
import json
import os
import sys
import threading
from io import StringIO
from pathlib import Path

from langchain_core.tools import tool

from tools.file_tools import resolve_workspace

MAX_PYTHON_OUTPUT_CHARS = 20000
PYTHON_OUTPUT_HEAD_CHARS = 8000
PYTHON_OUTPUT_TAIL_CHARS = 8000

DIFF_MARKER = "__DIFF__:"
DIFF_MAX_LINES = 500

# ── 实时输出流 ──
_progress_lock = threading.Lock()
_progress_lines: list[str] = []
_progress_running = False


def get_progress_since(index: int) -> tuple[list[str], int]:
    """获取 index 之后的新增行，返回 (新行列表, 当前总行数)。供 SSE 端点调用。"""
    with _progress_lock:
        return list(_progress_lines[index:]), len(_progress_lines)


def is_progress_running() -> bool:
    return _progress_running


class _ProgressIO(StringIO):
    """写日志的同时推送到全局进度列表"""
    def write(self, s: str) -> int:
        n = super().write(s)
        if s:
            with _progress_lock:
                _progress_lines.append(s)
        return n


def _gen_diff_lines(old_content: str, new_content: str) -> list[dict] | None:
    """对比新旧内容字符串，返回行级 diff 列表"""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    differ = difflib.Differ()
    diff = []
    added = 0
    removed = 0
    for line in differ.compare(old_lines, new_lines):
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
            continue
    if added == 0 and removed == 0:
        return None
    return [{"t": d["t"], "c": d["c"]} for d in diff]


def _diff_to_json(lines: list[dict], added: int, removed: int) -> str:
    payload = json.dumps(
        {"added": added, "removed": removed, "diff": lines},
        ensure_ascii=False,
    )
    return f"\n{DIFF_MARKER}{payload}"


def _gen_file_diff(before_path: Path, after_content: str) -> str | None:
    """对比旧文件和 new_content，返回 diff JSON 字符串或 None（供 file_tools 使用）"""
    old_content = ""
    try:
        if before_path.exists():
            old_content = before_path.read_text(encoding="utf-8")
    except Exception:
        return None
    diff_lines = _gen_diff_lines(old_content, after_content)
    if diff_lines is None:
        return None
    added = sum(1 for d in diff_lines if d["t"] == "+")
    removed = sum(1 for d in diff_lines if d["t"] == "-")
    return _diff_to_json(diff_lines, added, removed)


def _snapshot_files(workspace: Path) -> dict[str, str]:
    """扫描工作区文件，返回 {路径: 内容} 快照"""
    snap: dict[str, str] = {}
    count = 0
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or count >= 200:
                break
            fp = os.path.join(root, f)
            try:
                if os.path.getsize(fp) < 2_000_000:  # 跳过 >2MB 文件
                    snap[fp] = Path(fp).read_text(encoding="utf-8")
                    count += 1
            except Exception:
                pass
        if count >= 200:
            break
    return snap


@tool
def run_python(code: str) -> str:
    """执行 Python 代码并返回 stdout 输出。对于计算、数据分析、脚本测试非常有用。"""
    global _progress_running, _progress_lines

    # ── 重置进度记录 ──
    with _progress_lock:
        _progress_lines = []
        _progress_running = True

    # ── 执行前文件内容快照 ──
    workspace = resolve_workspace()
    before_snapshot = _snapshot_files(workspace)

    # ── 执行代码（使用 _ProgressIO 实时推送到全局进度）──
    old_stdout = sys.stdout
    captured = _ProgressIO()
    sys.stdout = captured
    try:
        exec(code, {"__builtins__": __builtins__})
    except Exception as e:
        return f"❌ 执行出错: {type(e).__name__}: {e}"
    finally:
        sys.stdout = old_stdout
        _progress_running = False
    output = captured.getvalue()

    # ── 执行后检测文件内容变化 ──
    diffs = []
    after_snapshot = _snapshot_files(workspace)
    for fp_str, before_content in before_snapshot.items():
        after_content = after_snapshot.get(fp_str, "")
        if after_content != before_content:
            lines = _gen_diff_lines(before_content, after_content)
            if lines:
                added = sum(1 for d in lines if d["t"] == "+")
                removed = sum(1 for d in lines if d["t"] == "-")
                diffs.append(_diff_to_json(lines, added, removed))
    # 检查新增文件
    for fp_str, after_content in after_snapshot.items():
        if fp_str not in before_snapshot:
            if after_content.strip():
                lines = _gen_diff_lines("", after_content)
                if lines:
                    added = sum(1 for d in lines if d["t"] == "+")
                    removed = sum(1 for d in lines if d["t"] == "-")
                    diffs.append(_diff_to_json(lines, added, removed))

    # ── 组装输出 ──
    if not output:
        output = "（代码执行完成，无输出）"
    elif len(output) > MAX_PYTHON_OUTPUT_CHARS:
        output = (
            "⚠️ Python 输出较大，未将完整日志放入模型上下文。\n"
            f"输出字符数: {len(output)}\n"
            "请将需要长期保存的日志写入工作区文件，或针对关键片段继续分析。\n\n"
            f"--- 输出开头 {PYTHON_OUTPUT_HEAD_CHARS} 字符 ---\n"
            f"{output[:PYTHON_OUTPUT_HEAD_CHARS]}\n\n"
            f"--- 输出结尾 {PYTHON_OUTPUT_TAIL_CHARS} 字符 ---\n"
            f"{output[-PYTHON_OUTPUT_TAIL_CHARS:]}"
        )

    result = output.rstrip()
    if diffs:
        all_diffs = []
        total_added = 0
        total_removed = 0
        for d in diffs:
            try:
                # d 格式: "\n__DIFF__:{json}"
                idx = d.index(DIFF_MARKER) + len(DIFF_MARKER)
                payload = json.loads(d[idx:])
                all_diffs.extend(payload.get("diff", []))
                total_added += payload.get("added", 0)
                total_removed += payload.get("removed", 0)
            except Exception:
                continue
        if all_diffs:
            combined = json.dumps(
                {"added": total_added, "removed": total_removed, "diff": all_diffs[:DIFF_MAX_LINES]},
                ensure_ascii=False,
            )
            result += f"\n{DIFF_MARKER}{combined}"
    return result


TOOLS = [run_python]
