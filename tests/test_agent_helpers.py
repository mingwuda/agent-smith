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


# ── OCR 降级（模型不支持视觉时，历史图片不再注入 image_url）──
# 用小 PNG 生成合法的 data URL，避免依赖外部图片文件
_PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _img_data_url() -> str:
    return f"data:image/png;base64,{_PNG_1PX}"


def test_session_messages_to_langchain_ocr_fallback(monkeypatch):
    """模型不支持视觉时，历史图片转视觉模型描述文本而不是 image_url"""
    import tools.vision_router as vr
    monkeypatch.setattr(vr, "describe_image_data_url", lambda data_url: "视觉描述文本")
    msgs = [
        {"role": "user", "content": "图里写的什么", "images": [_img_data_url()]},
        {"role": "assistant", "content": "好的"},
    ]
    converted = session_messages_to_langchain(msgs, ocr_fallback=True)
    assert len(converted) == 2
    content = converted[0].content
    # 必须是纯文本（视觉模型描述结果），绝不能是 image_url 列表
    assert isinstance(content, str)
    assert "视觉描述文本" in content
    assert "[图片" in content
    # 不注入任何 image_url
    assert "image_url" not in content


def test_session_messages_to_langchain_keeps_image_for_vision():
    """模型支持视觉时，历史图片仍以 image_url 注入（保持原行为）"""
    msgs = [
        {"role": "user", "content": "看看这张图", "images": [_img_data_url()]},
    ]
    converted = session_messages_to_langchain(msgs, ocr_fallback=False)
    content = converted[0].content
    assert isinstance(content, list)
    assert any(isinstance(i, dict) and i.get("type") == "image_url" for i in content)


def test_ensure_no_image_for_non_vision(monkeypatch):
    """兜底函数：非视觉模型下把残留 image_url 转视觉模型描述文本"""
    import tools.vision_router as vr
    monkeypatch.setattr(vr, "describe_image_data_url", lambda data_url: "视觉描述文本")
    from agent_core.agent_helpers import _ensure_no_image_for_non_vision, _model_supports_vision
    from agent_core.config import AgentConfig

    cfg = AgentConfig()
    # 构造一个非视觉模型配置
    cfg.active_provider = "openai"
    cfg.model = "deepseek-chat"
    assert _model_supports_vision(cfg) is False

    msg = HumanMessage(content=[
        {"type": "text", "text": "这是说明文字"},
        {"type": "image_url", "image_url": {"url": _img_data_url()}},
    ])
    out = _ensure_no_image_for_non_vision([msg], cfg)
    content = out[0].content
    assert isinstance(content, str)
    assert "这是说明文字" in content
    assert "视觉描述文本" in content
    assert "image_url" not in content


def test_ensure_no_image_for_vision_model_unchanged():
    """视觉模型下兜底函数原样返回，不触碰消息"""
    from agent_core.agent_helpers import _ensure_no_image_for_non_vision, _model_supports_vision
    from agent_core.config import AgentConfig

    cfg = AgentConfig()
    cfg.active_provider = "openai"
    cfg.model = "gpt-4o"
    assert _model_supports_vision(cfg) is True

    msg = HumanMessage(content=[
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": _img_data_url()}},
    ])
    out = _ensure_no_image_for_non_vision([msg], cfg)
    assert out[0] is msg  # 原对象引用，未改动
    assert isinstance(out[0].content, list)


def test_model_supports_vision_deepseek_v4_flash():
    """DeepSeek v4 flash 等官方变体必须判为非视觉（回归：修复 400 image_url 报错）"""
    from agent_core.agent_helpers import _model_supports_vision
    from agent_core.config import AgentConfig

    cfg = AgentConfig()
    cfg.active_provider = "deepseek"
    cfg.model = "deepseek-v4-flash"
    assert _model_supports_vision(cfg) is False


def test_model_supports_vision_unknown_default_ocr():
    """未知模型默认不支持视觉（走 OCR 降级），绝不让纯文本模型收到 image_url"""
    from agent_core.agent_helpers import _model_supports_vision
    from agent_core.config import AgentConfig

    cfg = AgentConfig()
    cfg.active_provider = "some-vendor"
    cfg.model = "brand-new-model-x"
    assert _model_supports_vision(cfg) is False
