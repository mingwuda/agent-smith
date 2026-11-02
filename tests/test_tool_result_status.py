"""工具结果状态归类测试（P1-5）。

核心约定（评审后修正）：
1. **只信前缀，不扫正文** —— 这个 agent 日常在读日志、grep 报错，
   扫关键字会把 `echo '日志：未发现错误'` 判成失败（实测复现过的误报）。
2. 转后台（⏱）是成功路径，不是失败。
3. 非零退出码是语义，不是失败。
"""
import sys

from agent_core.main import app  # noqa: F401  (sys.path 注入)
from agent_core.tool_result import annotate_for_model, classify, is_error


def test_empty_is_not_error():
    """空输出是 empty，不是 error —— 这是本条修复的核心价值。"""
    assert classify("") == "empty"
    assert classify("   \n\t ") == "empty"
    assert is_error("empty") is False


def test_async_handoff_is_ok_not_failure():
    """评审 P0-1：⏱ 转后台是成功路径（命令仍在前台跑得好好的），不得报失败。

    改造前这里被判成 timeout → is_error=True，前端卡片变红、诊断日志记为 error，
    而 npm install / pip install 这类长命令高频走这条路。
    """
    msg = "⏱ 命令前台执行 30s 仍未完成，已转后台继续运行。\n- task_id: abc"
    assert classify(msg) == "ok"
    assert is_error("ok") is False


def test_body_keywords_do_not_trigger_error():
    """评审 P0-3：正文里的「错误 / 失败 / failed」不能当判据。

    这些是 agent 读日志时的常见内容，误判会让成功命令显示成失败。
    """
    samples = [
        "✅ 命令已执行 (exit code: 0, 耗时: 0.1s)\n\n日志：未发现错误",
        "✅ 命令已执行 (exit code: 0, 耗时: 0.1s)\n\nERROR: 0 条，WARN: 3 条",
        "✅ 命令已执行 (exit code: 0, 耗时: 0.1s)\n\n测试 failed: 0 passed: 120",
        "✅ 命令已执行 (exit code: 0, 耗时: 0.1s)\n\n超时配置 timeout=30s 已生效",
    ]
    for text in samples:
        assert classify(text) == "ok", f"误判为 {classify(text)}: {text[:50]}"


def test_nonzero_exit_code_is_neutral():
    """评审 P0-2：非零退出码是语义（grep 无匹配 / git diff 有改动），不是失败。

    run_shell 现在用中性的 ✅ 措辞，不再复用 ❌。
    """
    msg = "✅ 命令已执行 (exit code: 1, 耗时: 0.1s)"
    assert classify(msg) == "ok"
    assert is_error("ok") is False


def test_explicit_error_prefix_is_error():
    """只有工具自己写 ❌ 才是硬失败。"""
    assert classify("❌ 执行失败: PermissionError") == "error"
    assert is_error("error") is True


def test_explicit_timeout_marker():
    """超时必须由工具显式标注（⌛），不能从正文猜。"""
    assert classify("⌛ 脚本执行超时（300s），已强制终止。") == "timeout"
    assert is_error("timeout") is True


def test_control_markers_are_not_errors():
    """需用户确认/授权不是失败，不该报红。"""
    assert classify("__CONFIRM_NEEDED__::递归删除::__CMD__::rm -rf x") == "ok"
    assert classify("__PERMISSION_NEEDED__/etc/hosts") == "ok"


def test_empty_marker_is_still_empty():
    """工具自报的 [状态: empty] 语义仍是 empty（用于模型侧提示）。"""
    assert classify("✅ 命令已执行 (exit code: 0)[状态: empty] 不要原样重试同一命令。") == "empty"


def test_annotate_only_for_empty_and_timeout():
    """只给模型补最易误判的两种前缀，ok/error 不加（省 token 且不干扰）。"""
    assert annotate_for_model("", "empty").startswith("[状态: empty]")
    assert annotate_for_model("⌛ ...", "timeout").startswith("[状态: timeout]")
    assert annotate_for_model("正常输出", "ok") == "正常输出"
    assert annotate_for_model("❌ 失败", "error") == "❌ 失败"


def test_classify_never_raises_on_weird_input():
    """classify 在工具出口调用，任何输入都不能抛异常打断整轮对话。"""
    for value in (None, 0, b"bytes", "x" * 100000, {"a": 1}, ["x"]):
        result = classify(value)  # 不抛异常即通过
        assert result in ("ok", "empty", "error", "timeout")
