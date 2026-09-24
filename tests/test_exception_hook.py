"""全局未捕获异常钩子（logger._install_exception_hook）测试。

验证：
  - setup_logging 后钩子已安装（sys + threading）
  - 主线程未捕获异常 → 被记入日志（含"未捕获异常"标记）
  - 后台线程未捕获异常 → 被记入日志（含线程名）
  - 幂等：重复调用 install 只安装一次
  - 转发：原钩子仍被调用（不破坏既有行为）

运行：python -m pytest tests/test_exception_hook.py -q
"""
import sys
import threading
import traceback

import pytest

from agent_core import logger as logger_mod


@pytest.fixture
def logs(caplog):
    """用 caplog 捕获 logger("uncaught") 的 ERROR 输出。"""
    with caplog.at_level("ERROR", logger="uncaught") as cap:
        yield cap


def test_hooks_installed_by_setup():
    """setup_logging 后 sys/threading excepthook 不再是默认内置。"""
    import logging
    orig_sys = sys.excepthook
    orig_thread = threading.excepthook
    try:
        logger_mod.setup_logging(log_dir="/tmp/agent-test-logs", console=False)
        # 装上我们的钩子后，应该能作为可调用对象且不崩溃
        sys.excepthook(ValueError, ValueError("boom"), None)
        assert True
    finally:
        sys.excepthook = orig_sys
        threading.excepthook = orig_thread
        import logging as _l
        _l.getLogger().handlers.clear()


def test_sys_hook_logs_uncaught(caplog):
    """主线程未捕获异常 → 记入日志。"""
    orig = sys.excepthook
    errs = []
    sys.excepthook = lambda *a: errs.append(a)
    try:
        logger_mod._exception_hook_installed = False
        logger_mod._install_exception_hook()
        with caplog.at_level("ERROR", logger="uncaught"):
            sys.excepthook(ValueError, ValueError("系统异常"), None)
    finally:
        sys.excepthook = orig

    assert caplog.records
    assert any("未捕获异常" in r.getMessage() for r in caplog.records)
    # 原钩子被转发
    assert errs and errs[0][0] is ValueError


def test_thread_hook_logs_with_thread_name(caplog):
    """后台线程未捕获异常 → 记入日志且带线程名。"""
    orig = threading.excepthook
    captured = []
    threading.excepthook = lambda args: captured.append(args)
    try:
        logger_mod._exception_hook_installed = False
        logger_mod._install_exception_hook()

        class _FakeArgs:
            exc_type = RuntimeError
            exc_value = RuntimeError("worker died")
            exc_traceback = None
            thread = None  # 手动模拟无 thread 的旧版本
        class _ArgsWithThread(_FakeArgs):
            thread = threading.Thread(name="worker-1", target=lambda: None)
        with caplog.at_level("ERROR", logger="uncaught"):
            threading.excepthook(_ArgsWithThread())
    finally:
        threading.excepthook = orig

    assert caplog.records
    got = [r.getMessage() for r in caplog.records]
    assert any("worker-1" in m for m in got)
    assert any("后台线程" in m for m in got)
    # 原钩子被转发
    assert captured and captured[0].exc_type is RuntimeError


def test_idempotent():
    """重复安装只设一次（记录到标记位）。"""
    orig_s = sys.excepthook
    orig_t = threading.excepthook
    try:
        logger_mod._exception_hook_installed = False
        logger_mod._install_exception_hook()
        first_s, first_t = sys.excepthook, threading.excepthook
        logger_mod._install_exception_hook()  # 第二次
        assert sys.excepthook is first_s
        assert threading.excepthook is first_t
    finally:
        sys.excepthook = orig_s
        threading.excepthook = orig_t
        logger_mod._exception_hook_installed = False


def test_real_background_thread_exception_is_logged(caplog):
    """真实后台线程抛未捕获异常 → 钩子捕获并记日志。"""
    orig_s, orig_t = sys.excepthook, threading.excepthook
    # 用空钩子避免默认行为把内容打到测试 stderr
    sys.excepthook = lambda *a: None
    threading.excepthook = lambda *a: None
    logger_mod._exception_hook_installed = False
    logger_mod._install_exception_hook()
    try:
        result = {}

        def _worker():
            raise RuntimeError("thread-crash-demo")

        t = threading.Thread(target=_worker, name="real-worker", daemon=True)
        with caplog.at_level("ERROR", logger="uncaught"):
            t.start()
            t.join(timeout=3)
        got = [r.getMessage() for r in caplog.records if "real-worker" in r.getMessage()]
        assert got and "thread-crash-demo" in got[0]
    finally:
        sys.excepthook = orig_s
        threading.excepthook = orig_t
        logger_mod._exception_hook_installed = False