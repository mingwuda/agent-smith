"""视觉模型路由：把图片交给用户标记的视觉模型生成描述。

设计动机：当用户配置的当前模型不支持图片（如 agnes-2.5-flash / qwen-turbo 等），
但同厂商下标记了视觉模型（如 agnes-vision），把图片直接给当前模型会失败、提示
"模型不支持视觉"对用户不友好。本模块提供「图片描述」能力：

  - 用户消息里的图片：自动用视觉模型描述 → 文本喂给当前非视觉模型
  - agent 后续步骤调 ocr_image 工具时：同样先走视觉模型描述，失败才 OCR 兜底

依赖：通过 app_state.get_agent_config() 读取配置（无需传参），读取 active_provider
下第一个被标记为 vision_models 的模型名作为"图片描述模型"。
"""
import base64
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$")


def _data_url_to_bytes(data_url: str) -> Optional[bytes]:
    m = _DATA_URL_RE.match(data_url or "")
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1))
    except Exception:
        return None


def _resolve_vision_model():
    """从全局 agent config 中找出"用于图片描述的视觉模型"。

    优先使用 active_provider 下被标记的视觉模型；若 active 厂商没有标记任何视觉模型，
    则回退扫描所有厂商，取第一个配置了视觉模型的厂商（其 api_key/base_url 随该厂商走）。
    这样即使当前发送厂商（如 stepfun）没有视觉模型，也能借助用户标记过的其它视觉模型
    （如 agnes-2.5-flash）完成图片描述，落实"图片先路由视觉模型描述"的意图。

    返回 (api_key, base_url, model_name) 或 None（无视觉模型 / 配置不可用）。
    """
    try:
        from app_state import get_agent_config
    except Exception:
        return None
    cfg = get_agent_config()
    if cfg is None:
        return None
    providers = getattr(cfg, "providers", {}) or {}
    active_pid = getattr(cfg, "active_provider", "") or ""
    # 优先 active，其次任意配置了视觉模型的厂商
    order = [active_pid] + [p for p in providers if p != active_pid]
    for pid in order:
        prov = providers.get(pid) or {}
        vision_models = prov.get("vision_models") or []
        if not vision_models:
            continue
        model_name = vision_models[0]  # 取第一个被标记的视觉模型
        api_key = prov.get("api_key") or getattr(cfg, "api_key", "") or ""
        base_url = prov.get("base_url") or getattr(cfg, "base_url", "") or ""
        if not api_key or not model_name:
            continue
        return api_key, base_url, model_name
    return None


def _current_active_pid() -> str:
    """读取当前 active_provider id，仅用于诊断日志。失败返回空串。"""
    try:
        from app_state import get_agent_config
        cfg = get_agent_config()
    except Exception:
        return ""
    if cfg is None:
        return ""
    return str(getattr(cfg, "active_provider", "") or "")


def describe_image_data_url(data_url: str) -> Optional[str]:
    """把 data URL 图片发给视觉模型，生成中文描述。

    返回：
      - str：描述文本（成功时）
      - None：无视觉模型配置 / 调用失败（调用方应回退 OCR）

    异常：本函数吞掉所有视觉模型调用异常并返回 None，调用方无需 try/except。
    ponytail：失败时一律打 warning 日志，方便排查为什么视觉路由没命中——
    否则调用方只会看到"OCR 兜底了"而不知道根本原因。
    """
    triple = _resolve_vision_model()
    if not triple:
        logger.warning(
            "[vision_router] 未解析到任何视觉模型（active_provider=%s），回退 OCR/错误提示",
            _current_active_pid(),
        )
        return None
    api_key, base_url, vision_model = triple

    # 防御：data_url 必须可解 base64
    if _data_url_to_bytes(data_url) is None:
        logger.warning("[vision_router] data_url 解析失败（不是合法的 data:image/...;base64,...）")
        return None

    try:
        # 使用 langchain 的 ChatOpenAI（兼容 OpenAI 风格接口的厂商都可用）
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
    except Exception as e:
        logger.warning("[vision_router] langchain 依赖缺失: %s", e)
        return None

    try:
        llm = ChatOpenAI(
            model=vision_model,
            api_key=api_key,
            base_url=base_url or None,
            timeout=30,
            max_retries=0,
        )
        prompt_text = (
            "请用中文详细描述这张图片的内容，包括主体、场景、文字（如果有）、布局、风格等关键信息。"
            "描述应能让看不到图片的人完整重建图片内容。控制在 500 字以内。"
        )
        msg = HumanMessage(content=[
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ])
        resp = llm.invoke([msg])
        text = str(getattr(resp, "content", "") or "").strip()
        if not text:
            logger.warning("[vision_router] %s 调用成功但返回内容为空", vision_model)
            return None
        logger.info("[vision_router] %s 视觉描述成功（%d 字符）", vision_model, len(text))
        return text
    except Exception as e:
        logger.warning(
            "[vision_router] 调用视觉模型 %s 失败（base=%s）: %s: %s",
            vision_model, base_url or "(default)", type(e).__name__, e,
        )
        return None


def describe_image_file(file_path: str) -> Optional[str]:
    """从文件路径读取图片，调视觉模型描述。文件不存在/读失败/非图片返回 None。"""
    from pathlib import Path
    try:
        p = Path(file_path).expanduser()
        if not p.is_file():
            return None
        suffix = (p.suffix or ".png").lower()
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        }.get(suffix, "image/png")
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return describe_image_data_url(f"data:{mime};base64,{b64}")
    except Exception:
        return None