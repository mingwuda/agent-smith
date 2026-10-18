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


def test_locked_concurrent_edits_isolated(tmp_path):
    """两个线程并发编辑各自文件的 read-modify-write：锁保证互不干扰、不死锁。

    设计说明（旧版为何是竞态用例）：旧版让两线程对同一共享 token [[A]]
    各自做「替换-还原」——一个线程改掉 [[A]] 后，另一个线程的替换必然
    「未找到匹配」，锁越正确该交错越必然发生，纯属调度侥幸才能通过。
    工具级锁能保证的是：并发下不死锁、不报错、各写各的；跨 read+write
    两次工具调用的 RMW 原子性无法在工具粒度回归，由
    test_write_lock_serializes_critical_section 直接验锁互斥补足。
    """
    results = {}
    barrier = threading.Barrier(2)

    def worker(tag: str):
        # 工作区存于 ContextVar（不跨线程），每个线程各自 set 指向 tmp_path，
        # 这样 edit_file 的旧路径解析进工作区即可写（无需外部授权）。
        file_tools.set_workspace(tmp_path)
        # logger.langchain 由 StructuredTool 正常执行；直接用 .func（持锁的裸 wrapper）
        ed = file_tools.edit_file.func
        own = f"f_{tag}.txt"
        (tmp_path / own).write_text("[[A]]", encoding="utf-8")
        barrier.wait()  # 同时起跑，制造锁竞争
        for i in range(20):
            resp = ed(own, "[[A]]", f"[{tag}{i}]")
            assert "已替换" in resp or "未变化" in resp, resp
            resp = ed(own, f"[{tag}{i}]", "[[A]]")
            assert "已替换" in resp or "未变化" in resp, resp
        results[tag] = True

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("x", "y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
        assert not t.is_alive(), "线程 20s 未结束——写锁死锁？"

    assert len(results) == 2, f"有线程中途失败: {results}"
    for tag in ("x", "y"):
        assert (tmp_path / f"f_{tag}.txt").read_text(encoding="utf-8") == "[[A]]"


def test_write_lock_serializes_critical_section():
    """_WRITE_LOCK 确实互斥：临界区内不允许另一线程进入（确定性验证串行化）。"""
    order = []

    def worker(tag: str):
        with file_tools._WRITE_LOCK:
            order.append(f"{tag}-enter")
            time.sleep(0.05)  # 撑住临界区，给另一线程闯入的机会
            order.append(f"{tag}-exit")

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("x", "y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    # enter/exit 必须严格交替（x-enter,x-exit,y-enter,y-exit 的任意线程顺序），
    # 不允许出现 x-enter,y-enter 这种交错
    assert len(order) == 4, f"进入次数异常: {order}"
    for i in range(0, 4, 2):
        pair = order[i:i + 2]
        assert pair[0].endswith("-enter") and pair[1].endswith("-exit"), \
            f"临界区被闯入（enter 后紧跟另一线程 enter）: {order}"
        assert pair[0].split("-")[0] == pair[1].split("-")[0], \
            f"enter/exit 不属于同一线程: {order}"


def test_write_tools_still_function(tmp_path):
    """加锁后写工具功能不受影响（write_file 可正常用）。"""
    file_tools.set_workspace(tmp_path)
    out = file_tools.write_file.func("hello.txt", "hi")
    assert "✅ 已写入" in out
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi"