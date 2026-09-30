"""图片识别工具（统一走视觉模型描述）

当 LLM 不支持图片输入（vision）时，把图片交给用户标记的视觉模型生成中文描述，
再把文本喂给当前模型——不再依赖系统 tesseract OCR。

路由策略：
  - 用户标记了视觉模型 → 调视觉模型生成描述（支持任意图像，不限于文字）
  - 未标记视觉模型 → 返回友好提示，让 agent / 用户知道需要配置视觉模型
"""
import base64
from contextvars import ContextVar
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

# ── 工作区路径（ContextVar：与 file_tools 同构，每次请求独立）──
_workspace_ctx: ContextVar[Optional[Path]] = ContextVar("ocr_tools_workspace", default=None)


def set_workspace(path: Path):
    _workspace_ctx.set(path.expanduser().resolve())


def _resolve_workspace() -> Path:
    _ws = _workspace_ctx.get()
    return (_ws or Path.home() / "agent_workspace").expanduser().resolve()


_MIME_BY_SUFFIX = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def _data_url_from_file(path: Path) -> str:
    """读取图片文件，转成 data URL（带正确 mime）。"""
    suffix = (path.suffix or ".png").lower()
    mime = _MIME_BY_SUFFIX.get(suffix, "image/png")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


@tool
def ocr_image(image: str) -> str:
    """识别图片内容（调用视觉模型生成描述）。

    当所选模型本身不支持图片输入时，agent 可调用本工具把图片交给视觉模型描述，
    再把描述用于后续工作；所选模型支持图片时也可直接丢图，不必调用本工具。

    支持的输入：
      1. 图片文件路径（工作区相对路径或绝对路径，如 "screenshot.png"、"/tmp/a.jpg"）
      2. data URL（以 data:image/ 开头，用于前端粘贴的图片）

    返回: 图片的中文描述文本；图片不存在或读取失败时给出明确报错。
    """
    data_url = image if str(image).startswith("data:image/") else ""
    if data_url:
        from tools.vision_router import image_to_text
        return image_to_text(data_url)

    raw = Path(str(image)).expanduser()
    target = raw if raw.is_absolute() else _resolve_workspace() / raw
    target = target.resolve(strict=False)
    if not target.exists():
        return f"❌ 图片不存在: {image}"
    if not target.is_file():
        return f"❌ 不是文件: {image}"
    try:
        data_url = _data_url_from_file(target)
    except Exception as e:
        return f"❌ 读取图片失败: {e}"
    from tools.vision_router import image_to_text
    return image_to_text(data_url)


TOOLS = [ocr_image]
