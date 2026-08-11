"""方案A：OCR 降级 synthetic 工具步骤 —— 让纯文本模型的图片会话在历史回放中
也能看到「调用工具: ocr_image」工作卡片。

覆盖：
  - _human_content 带 ocr_sink：收集降级记录（tool/args/result），文本含 OCR 结果
  - _human_content 不传 ocr_sink（默认 None）：行为不变，兼容旧调用
  - _human_content 视觉模型（ocr_fallback=False）：返回 multimodal，不写 sink
  - _synthetic_ocr_sse_steps：生成成对的 tool_start/tool_result 事件
"""
import time

import pytest

from agent_core.main import app  # noqa: F401  触发 agent_core 的 sys.path 注入
from agent_core.agent_helpers import _human_content, _synthetic_ocr_sse_steps

_PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _img_data_url() -> str:
    return f"data:image/png;base64,{_PNG_1PX}"


@pytest.fixture(autouse=True)
def _fake_ocr(monkeypatch):
    """把 ocr_data_url 替换为确定性假实现，避免依赖系统 tesseract"""
    import tools.ocr_tools as ocr_tools

    def fake_ocr(data_url: str) -> str:
        return f"OCR文本({len(data_url)}字节)"

    monkeypatch.setattr(ocr_tools, "ocr_data_url", fake_ocr)


def _attachments(n: int = 1) -> list:
    return [
        {"mime_type": "image/png", "data_url": _img_data_url()}
        for _ in range(n)
    ]


def test_human_content_ocr_sink_collects_records():
    """降级发生时，ocr_sink 按图片逐条收集工具记录，返回文本含 OCR 结果"""
    sink: list = []
    text = _human_content("看看这张图", _attachments(2), ocr_fallback=True, ocr_sink=sink)

    # 返回纯文本（OCR 降级），绝不包含 image_url
    assert isinstance(text, str)
    assert "[图片 1 OCR 识别结果]" in text
    assert "[图片 2 OCR 识别结果]" in text
    assert "OCR文本" in text

    # sink 按图片逐条记录
    assert len(sink) == 2
    for idx, rec in enumerate(sink, 1):
        assert rec["tool"] == "ocr_image"
        assert rec["args"]["index"] == idx
        assert "OCR文本" in rec["result"]
        assert "不支持图片输入" in rec["args"]["reason"]


def test_human_content_ocr_sink_default_none_unchanged():
    """不传 ocr_sink（默认 None）时行为不变，返回与旧实现相同的文本"""
    text_without = _human_content("看看这张图", _attachments(1), ocr_fallback=True)
    sink: list = []
    text_with = _human_content("看看这张图", _attachments(1), ocr_fallback=True, ocr_sink=sink)
    assert text_without == text_with
    assert len(sink) == 1  # 传了 sink 才收集


def test_human_content_vision_model_no_sink_write():
    """视觉模型（ocr_fallback=False）走 multimodal，不写 sink、不降级"""
    sink: list = []
    content = _human_content("看看这张图", _attachments(1), ocr_fallback=False, ocr_sink=sink)
    assert isinstance(content, list)
    assert any(isinstance(i, dict) and i.get("type") == "image_url" for i in content)
    assert sink == []  # 未发生降级，不写 sink


def test_synthetic_ocr_sse_steps_pairs():
    """synthetic 事件成对生成：每个 tool_start 有配套 tool_result，step 从 1 连续"""
    sink = [
        {"tool": "ocr_image", "args": {"index": 1}, "result": "第一张图文字"},
        {"tool": "ocr_image", "args": {"index": 2}, "result": "第二张图文字"},
    ]
    events = _synthetic_ocr_sse_steps(sink)

    assert len(events) == 4
    starts = [e for e in events if e["type"] == "tool_start"]
    results = [e for e in events if e["type"] == "tool_result"]

    assert len(starts) == 2 and len(results) == 2
    for i, (s, r) in enumerate(zip(starts, results), 1):
        # step 编号一致且连续（前端以 step 为 key 关联卡片）
        assert s["step"] == i == r["step"]
        assert s["tool"] == "ocr_image" == r["tool"]
        assert s["args"]["index"] == i
        expected = "第一张图文字" if i == 1 else "第二张图文字"
        assert r["result"] == expected
        assert r["error"] is False
        # 与真实工具事件同构：ts / duration_ms 字段存在
        assert isinstance(s.get("ts"), int)
        assert "duration_ms" in r


def test_synthetic_ocr_sse_steps_empty_sink():
    """空 sink 返回空事件列表（无图片/无降级时不产生 synthetic 卡片）"""
    assert _synthetic_ocr_sse_steps([]) == []
