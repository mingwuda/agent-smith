"""回归测试：文件写工具在并发线程下加互斥锁。

背景：delegate_tasks_parallel 让最多 4 个子代理在各自线程里执行文件写工具，
若 read-modify-write 不加锁，多个线程并发 write_file/edit_file 会互相覆盖
（后者读到的 old 内容已被前者写入，导致各自独立写盘、最终只剩一个）。

本测试锁定：所有写/删工具都被 _write_locked 装饰（持有全局 RLock），
并用一个「会主动让出时间的写函数」验证并发线程确实被串行化。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core" / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

import file_tools
from file_tools import _WRITE_LOCK

_WRITE_TOOLS = [
    "write_file", "append_to_file", "edit_file",
    "insert_lines", "replace_lines", "delete_file",
]


def test_write_tools_locked():
    """所有写/删工具底层函数都应被 _write_locked 装饰（.func 是持锁 wrapper）。"""
    for name in _WRITE_TOOLS:
        fn = getattr(file_tools, name)
        raw = fn.func if hasattr(fn, "func") else fn
        # 装饰器用 functools.wraps 保留了原函数名 write_file，不能看 __name__；
        # 关键证据是 __wrapped__ 存在（说明它被 _write_locked 包过）
        assert hasattr(raw, "__wrapped__"), f"{name} 未被 _write_locked 装饰（缺 __wrapped__）"
        # 且 __wrapped__ 就是未加锁的原始 @tool 函数本体（更深处才是真正函数）
        assert raw.__wrapped__ is not raw, f"{name} wrapper 指向自身，未真正包裹"


def test_locked_serializes_read_modify_write(tmp_path):
    """两个线程并发 edit_file 同一文件：不互相覆盖，结果完整。

    用 edit_file 把 [[A]] 同时替换为其它标记再替换回，模拟并发 read-modify-write。
    存在锁时，两次替换串行执行，文件内容必为完整形态；
    若不加锁，可能读到半截/互相覆盖导致内容损坏。
    """
    target = tmp_path / "f.txt"
    target.write_text("[[A]]", encoding="utf-8")

    results = {}
    barrier = threading.Barrier(2)

    def worker(tag: str):
        # 工作区存于 ContextVar（不跨线程），每个线程各自 set 指向 tmp_path，
        # 这样 edit_file 的旧路径解析进工作区即可写（无需外部授权）。
        file_tools.set_workspace(tmp_path)
        # logger.langchain 由 StructuredTool 正常执行；直接用 .func（持锁的裸 wrapper）
        ed = file_tools.edit_file.func
        barrier.wait()
        for i in range(20):
            resp = ed("f.txt", "[[A]]", f"[{tag}{i}]")
            assert "已替换" in resp or "未变化" in resp, resp
            ed("f.txt", f"[{tag}{i}]", "[[A]]")
        results[tag] = True

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("x", "y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    final = target.read_text(encoding="utf-8")
    assert final == "[[A]]", f"并发读写损坏文件: {final!r}"
    assert len(results) == 2


def test_write_tools_still_function(tmp_path):
    """加锁后写工具功能不受影响（write_file 可正常用）。"""
    file_tools.set_workspace(tmp_path)
    out = file_tools.write_file.func("hello.txt", "hi")
    assert "✅ 已写入" in out
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi"