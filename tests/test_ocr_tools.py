"""图片识别工具的单元测试：调用视觉模型生成图片描述（不再依赖 tesseract）。

覆盖：
  - ocr_image LLM 工具：data URL / 绝对路径 / 工作区相对路径 / 文件不存在
  - 无视觉模型配置时返回友好提示（不抛异常、不崩溃）

测试环境无真实视觉模型，故用 monkeypatch 把 vision_router.describe_image_data_url
替换为确定性实现；图片用 PIL 现场生成，不落仓库。
导入 agent_core.main 触发 sys.path 注入（与 test_agent_helpers.py 同模式）。
"""
import base64
import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from agent_core.main import app  # noqa: F401  触发 agent_core 的 sys.path 注入
from agent_core.tools.ocr_tools import ocr_image, set_workspace

_DEJAVU_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _make_test_image(text: str = "Hello Vision 2026", size=(640, 180)) -> bytes:
    """生成一张白底黑字的 PNG 测试图片（返回字节）。"""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_DEJAVU_FONT, 36)
    except Exception:
        font = ImageFont.load_default()
    d.text((40, 60), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _data_url(img_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(img_bytes).decode()


@pytest.fixture(autouse=True)
def _fake_vision(monkeypatch):
    """把视觉模型描述替换为确定性假实现，避免依赖真实视觉模型 / 网络。"""
    import tools.vision_router as vr

    monkeypatch.setattr(vr, "describe_image_data_url", lambda data_url: "视觉模型描述：图中写着测试文字")


def test_ocr_image_data_url_input():
    """LLM 工具：data URL 输入 → 调用视觉模型得到描述。"""
    result = ocr_image.invoke({"image": _data_url(_make_test_image("MOSS TEST 123"))})
    assert "视觉模型描述" in result


def test_ocr_image_absolute_path_input(tmp_path):
    """LLM 工具：绝对文件路径输入。"""
    p = tmp_path / "shot.png"
    p.write_bytes(_make_test_image("FILE VISION OK"))
    result = ocr_image.invoke({"image": str(p)})
    assert "视觉模型描述" in result


def test_ocr_image_workspace_relative_path(tmp_path):
    """LLM 工具：set_workspace 后，相对路径按工作区解析。"""
    set_workspace(tmp_path)
    (tmp_path / "note.png").write_bytes(_make_test_image("WORKSPACE REL"))
    result = ocr_image.invoke({"image": "note.png"})
    assert "视觉模型描述" in result


def test_ocr_image_missing_file():
    """LLM 工具：文件不存在时给出明确报错。"""
    result = ocr_image.invoke({"image": "/no/such/file.png"})
    assert "不存在" in result


def test_ocr_image_no_vision_model_friendly():
    """未配置视觉模型（describe_image_data_url 返回 None）时返回友好提示而非崩溃。"""
    import tools.vision_router as vr
    from unittest.mock import patch

    with patch.object(vr, "describe_image_data_url", lambda data_url: None):
        result = ocr_image.invoke({"image": _data_url(_make_test_image())})
    assert "未配置视觉模型" in result or "视觉模型调用失败" in result
