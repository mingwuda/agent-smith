"""agent_helpers 模块级辅助函数的单元测试（拆分自 agent.py 后）。

只覆盖不依赖网络/真实 LLM 的纯逻辑；导入前先导入 agent_core.main
以触发 sys.path 注入，使 `from config import ...` 等顶层导入可用。
"""
from agent_core.main import app  # noqa: F401  触发 agent_core 的 sys.path 注入
from agent_core.agent_helpers import (
    _detect_scene,
    _truncate,
    _sse,
    _loop_guard_message,
    _message_text,
    session_messages_to_langchain,
)
from langchain_core.messages import AIMessage, HumanMessage


def test_detect_scene_image_priority_over_coding():
    # 「用 Python 生成图片」同时含 python(coding) 与 生成图片(image)，应判 image
    assert _detect_scene("用Python生成一张产品图片") == "image"
    assert _detect_scene("帮我用 python 画一张流程图") == "image"
    # 纯 coding 请求仍判 coding
    assert _detect_scene("用 python 写一个爬虫脚本") == "coding"
    assert _detect_scene("帮我重构这段代码") == "coding"


def test_detect_scene_other_scenes():
    assert _detect_scene("帮我做个PPT汇报") == "ppt"
    assert _detect_scene("总结一下这篇文章") == "article"
    assert _detect_scene("分析一下为什么报错") == "analysis"
    assert _detect_scene("打开百度并截图") == "browser"
    assert _detect_scene("随便聊聊") == ""


def test_detect_scene_history_fallback():
    # 当前消息为空（纯追问）时回退到历史最后一条 user 消息
    assert _detect_scene(
        "", history=[
            {"role": "user", "content": "帮我做个PPT"},
            {"role": "assistant", "content": "好的"},
        ]
    ) == "ppt"
    # 历史里没有 user 消息
    assert _detect_scene("", history=[{"role": "assistant", "content": "hi"}]) == ""
    # 当前消息非空时忽略历史
    assert _detect_scene("直接开始", history=[{"role": "user", "content": "做个PPT"}]) == ""


def test_truncate():
    assert _truncate("short", 100) == "short"
    assert _truncate("a" * 100, 10) == "a" * 10 + "..."
    assert len(_truncate("a" * 100, 10)) == 13


def test_sse_format():
    out = _sse({"event": "delta", "data": "hi"})
    assert out.startswith("data: ")
    assert out.endswith("\n\n")
    assert '"delta"' in out


def test_loop_guard_message_contains_reason():
    msg = _loop_guard_message("重复调用同一工具", [{"tool": "run_shell", "args": {}}], 25)
    assert isinstance(msg, str)
    assert "重复调用同一工具" in msg
    assert "25" in msg


def test_message_text_normalization():
    assert _message_text(None) == ""
    assert _message_text("hello") == "hello"
    assert _message_text([{"text": "a"}, {"text": "b"}]) == "a\nb"
    assert _message_text([{"type": "text", "text": "x"}]) == "x"


def test_session_messages_to_langchain():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    converted = session_messages_to_langchain(msgs)
    assert len(converted) == 2
    assert isinstance(converted[0], HumanMessage)
    assert isinstance(converted[1], AIMessage)
    assert converted[0].content == "hi"
    assert converted[1].content == "hello"
