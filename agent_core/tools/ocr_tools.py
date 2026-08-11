"""OCR 图片文字识别工具

当 LLM 不支持图片输入（vision）时，用系统 tesseract 识别图片中的文字。
- 零新依赖：复用系统已安装的 tesseract 5.x（chi_sim+eng 语言包）
- PIL 预处理（灰度/放大）提升小字、低对比度识别率
"""
import base64
import io
import re
import shutil
import subprocess
import tempfile
from contextvars import ContextVar
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

# ── 工作区路径（ContextVar：与 file_tools 同构，每次请求独立）──
_workspace_ctx: ContextVar[Optional[Path]] = ContextVar("ocr_tools_workspace", default=None)

_DEFAULT_LANG = "chi_sim+eng"
_MAX_OUTPUT_CHARS = 8000
_TIMEOUT_SECONDS = 120


def set_workspace(path: Path):
    _workspace_ctx.set(path.expanduser().resolve())


def _resolve_workspace() -> Path:
    _ws = _workspace_ctx.get()
    return (_ws or Path.home() / "agent_workspace").expanduser().resolve()


def _tesseract_path() -> Optional[str]:
    """定位系统 tesseract 可执行文件。"""
    return shutil.which("tesseract")


def _ocr_image_bytes(img_bytes: bytes, lang: str = _DEFAULT_LANG) -> str:
    """对图片字节执行 OCR，返回识别文本。"""
    tesseract = _tesseract_path()
    if not tesseract:
        return "❌ 系统未安装 tesseract，无法执行 OCR。请先安装：apt install tesseract-ocr tesseract-ocr-chi-sim"

    try:
        from PIL import Image, ImageOps
    except ImportError:
        return "❌ 缺少 PIL 库，无法预处理图片。请安装：pip install pillow"

    # PIL 预处理：转灰度 + 放大 2 倍，显著提升小字/低对比度识别率
    try:
        with Image.open(io.BytesIO(img_bytes)) as img:
            img = ImageOps.exif_transpose(img)  # 修正手机照片方向
            gray = img.convert("L")
            w, h = gray.size
            scale = max(1, min(2, 2400 // max(w, 1)))  # 目标长边约 2400px，最多 2 倍
            if scale > 1:
                gray = gray.resize((w * scale, h * scale), Image.LANCZOS)
            buf = io.BytesIO()
            gray.save(buf, format="PNG")
            preprocessed = buf.getvalue()
    except Exception as e:
        return f"❌ 图片预处理失败: {e}"

    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
        tmp.write(preprocessed)
        tmp.flush()
        try:
            proc = subprocess.run(
                [tesseract, tmp.name, "stdout", "-l", lang],
                capture_output=True, timeout=_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"❌ OCR 超时（>{_TIMEOUT_SECONDS}s），图片过大或 tesseract 卡住"
        except Exception as e:
            return f"❌ OCR 执行失败: {e}"

    if proc.returncode != 0:
        return f"❌ OCR 失败: {proc.stderr.decode('utf-8', 'replace')[:500]}"

    text = proc.stdout.decode("utf-8", "replace").strip()
    if not text:
        return "（未识别到文字：图片可能不含文字，或文字过小/模糊）"
    if len(text) > _MAX_OUTPUT_CHARS:
        head = text[:_MAX_OUTPUT_CHARS // 2]
        tail = text[-_MAX_OUTPUT_CHARS // 2:]
        text = f"⚠️ OCR 结果过长（共 {len(text)} 字符），仅保留头尾各 {_MAX_OUTPUT_CHARS // 2} 字符。\n\n{head}\n\n...\n\n{tail}"
    return text


def ocr_data_url(data_url: str, lang: str = _DEFAULT_LANG) -> str:
    """识别 data URL（data:image/...;base64,...）中的图片文字。

    供前端粘贴图片自动降级链路调用（不经 LLM 工具调用，直接内联）。
    """
    m = re.match(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", data_url)
    if not m:
        return "❌ 非法的 data URL 格式"
    try:
        img_bytes = base64.b64decode(m.group(1))
    except Exception as e:
        return f"❌ base64 解码失败: {e}"
    return _ocr_image_bytes(img_bytes, lang)


@tool
def ocr_image(image: str, lang: str = "chi_sim+eng") -> str:
    """识别图片中的文字（OCR），返回识别出的文本内容。

    适用于模型不支持图片输入、或需要从截图/票据/文档图片中提取文字的场景。
    image 参数支持两种格式：
      1. 图片文件路径（工作区相对路径或绝对路径，如 "screenshot.png"、"/tmp/a.jpg"）
      2. data URL（以 data:image/ 开头，用于前端粘贴的图片）

    参数:
      - image: 图片路径或 data URL
      - lang: OCR 语言，默认 "chi_sim+eng"（简体中文+英文）；纯英文可传 "eng"

    返回: 图片中的文字内容；图片不含文字或识别失败时返回相应说明。
    """
    data_url = image if str(image).startswith("data:image/") else ""
    if data_url:
        return ocr_data_url(data_url, lang)

    # 文件路径：支持工作区相对与绝对路径
    raw = Path(str(image)).expanduser()
    target = raw if raw.is_absolute() else _resolve_workspace() / raw
    target = target.resolve(strict=False)
    if not target.exists():
        return f"❌ 图片不存在: {image}"
    if not target.is_file():
        return f"❌ 不是文件: {image}"
    try:
        img_bytes = target.read_bytes()
    except Exception as e:
        return f"❌ 读取图片失败: {e}"
    return _ocr_image_bytes(img_bytes, lang)


TOOLS = [ocr_image]
