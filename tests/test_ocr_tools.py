"""OCR 工具的单元测试：LLM 不支持图片输入时用 tesseract 识别图片文字。

覆盖：
  - ocr_data_url 内联降级函数（data URL 输入 / 非法 data URL）
  - ocr_image LLM 工具（data URL / 绝对路径 / 工作区相对路径 / 文件不存在）

依赖系统 tesseract（chi_sim+eng 语言包）；测试图片用 PIL 现场生成，不落仓库。
导入 agent_core.main 触发 sys.path 注入（与 test_agent_helpers.py 同模式）。
"""
import base64
import io

from PIL import Image, ImageDraw, ImageFont

from agent_core.main import app  # noqa: F401  触发 agent_core 的 sys.path 注入
from agent_core.tools.ocr_tools import ocr_data_url, ocr_image, set_workspace

_DEJAVU_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _make_test_image(text: str = "Hello OCR 2026", size=(640, 180)) -> bytes:
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


def test_ocr_data_url_inline():
    """内联降级函数：data URL 识别出图片文字。"""
    text = ocr_data_url(_data_url(_make_test_image("Hello OCR 2026")))
    assert "Hello OCR 2026" in text


def test_ocr_data_url_invalid():
    """非法 data URL 应明确报错而非抛异常：格式不符 → 非法；坏 base64 → 解码失败。"""
    assert "非法" in ocr_data_url("not-a-data-url")
    assert "解码失败" in ocr_data_url("data:image/png;base64,!!!not-base64!!!")


def test_ocr_image_data_url_input():
    """LLM 工具：data URL 输入。"""
    result = ocr_image.invoke({"image": _data_url(_make_test_image("MOSS TEST 123"))})
    assert "MOSS TEST 123" in result


def test_ocr_image_absolute_path_input(tmp_path):
    """LLM 工具：绝对文件路径输入。"""
    p = tmp_path / "shot.png"
    p.write_bytes(_make_test_image("FILE OCR OK"))
    result = ocr_image.invoke({"image": str(p)})
    assert "FILE OCR OK" in result


def test_ocr_image_workspace_relative_path(tmp_path):
    """LLM 工具：set_workspace 后，相对路径按工作区解析。"""
    set_workspace(tmp_path)
    (tmp_path / "note.png").write_bytes(_make_test_image("WORKSPACE REL"))
    result = ocr_image.invoke({"image": "note.png"})
    assert "WORKSPACE REL" in result


def test_ocr_image_missing_file():
    """LLM 工具：文件不存在时给出明确报错。"""
    result = ocr_image.invoke({"image": "/no/such/file.png"})
    assert "不存在" in result
