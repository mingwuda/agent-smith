"""浏览器自动化工具（基于 Playwright）

支持页面导航、元素交互、截图、验证码识别和前端 E2E 测试。
"""
import asyncio
import base64
import concurrent.futures
import io
import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 专用浏览器线程和事件循环，避免与主线程事件循环冲突
_browser_loop: Optional[asyncio.AbstractEventLoop] = None
_browser_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()


def _ensure_browser_loop():
    """启动专用浏览器事件循环线程（如果未启动）"""
    global _browser_loop, _browser_thread
    
    with _loop_lock:
        if _browser_loop is not None and _browser_loop.is_running():
            return _browser_loop
        
        # 创建新事件循环
        _browser_loop = asyncio.new_event_loop()
        
        def _run_loop():
            asyncio.set_event_loop(_browser_loop)
            _browser_loop.run_forever()
        
        _browser_thread = threading.Thread(
            target=_run_loop,
            daemon=True,
            name="browser-event-loop"
        )
        _browser_thread.start()
        
        # 等待事件循环启动
        deadline = time.time() + 5
        while time.time() < deadline:
            if _browser_loop.is_running():
                break
            time.sleep(0.05)
        
        if not _browser_loop.is_running():
            raise RuntimeError("浏览器事件循环启动失败")
        
        return _browser_loop


def _run_async(coro) -> any:
    """在同步上下文中执行异步协程。
    
    使用专用浏览器线程的事件循环，避免与主线程事件循环冲突。
    """
    loop = _ensure_browser_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=90)


def _stop_browser_loop():
    """停止浏览器事件循环（进程退出时调用）"""
    global _browser_loop, _browser_thread
    if _browser_loop is not None:
        _browser_loop.call_soon_threadsafe(_browser_loop.stop)
        if _browser_thread is not None:
            _browser_thread.join(timeout=5)
        _browser_loop = None
        _browser_thread = None


# 全局浏览器实例（跨工具调用复用）
_browser = None
_browser_context = None
_page = None
_playwright = None
_page_lock: Optional[asyncio.Lock] = None  # 延迟初始化

# 工作区（用于保存截图）
_workspace: Optional[Path] = None

# 进程退出时清理浏览器资源
import atexit
atexit.register(_stop_browser_loop)


def set_workspace(path: Path):
    global _workspace
    _workspace = path.expanduser().resolve()


async def _ensure_browser():
    """惰性初始化浏览器实例（每个进程只启动一次）"""
    global _browser, _browser_context, _page, _playwright, _page_lock
    if _page is not None:
        return _page

    from playwright.async_api import async_playwright

    _playwright = await async_playwright().start()
    headless = os.environ.get("BROWSER_HEADLESS", "1") == "1"
    _browser = await _playwright.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
        ],
    )
    _browser_context = await _browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    _page = await _browser_context.new_page()
    
    # 在当前事件循环中创建锁
    if _page_lock is None:
        _page_lock = asyncio.Lock()
    
    logger.info("✅ 浏览器已启动")
    return _page


async def _close_browser():
    """关闭浏览器实例"""
    global _browser, _browser_context, _page, _playwright
    try:
        if _browser_context:
            await _browser_context.close()
        if _browser:
            await _browser.close()
        if _playwright:
            await _playwright.stop()
    except Exception:
        pass
    finally:
        _browser = None
        _browser_context = None
        _page = None
        _playwright = None


async def _save_screenshot(page) -> dict:
    """截取当前页面截图并保存到工作区（异步版本）"""
    timestamp = int(time.time())
    screenshot_path = None
    if _workspace:
        screenshot_dir = _workspace / ".browser_screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / f"screenshot_{timestamp}.png"

    result = {"timestamp": timestamp}

    # 截图并保存
    png_data = await page.screenshot(full_page=True, timeout=30000)

    if screenshot_path:
        screenshot_path.write_bytes(png_data)
        path_str = str(screenshot_path)
        result["path"] = path_str
        
        # token = 文件名（不包含扩展名），端点通过 workspace/.browser_screenshots/{token}.png 查找
        result["token"] = screenshot_path.stem
        
        # 获取图片尺寸
        try:
            import io
            from PIL import Image as PILImage
            img = PILImage.open(io.BytesIO(png_data))
            w, h = img.size
            result["size"] = f"{w}x{h}"
        except Exception:
            pass

    return result


async def _page_info(page) -> str:
    """提取当前页面关键信息（异步版本）"""
    try:
        title = await asyncio.wait_for(page.title(), timeout=10)
        url = await asyncio.wait_for(
            page.evaluate("window.location.href"), timeout=10
        )
        return f"当前页面: {title}\n当前 URL: {url}"
    except Exception as e:
        return f"（无法获取页面信息: {e}）"


@tool
def browser_navigate(url: str) -> str:
    """导航到指定 URL 并返回页面标题和截图。

    参数:
      url: 完整的网页地址（包含 http:// 或 https://）
    """
    async def _run():
        page = await _ensure_browser()
        try:
            logger.info(f"🌐 正在导航到: {url}")
            # 先尝试 networkidle，失败后用 domcontentloaded 兜底
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                logger.info("networkidle 超时，但页面已加载（domcontentloaded）")
            logger.info(f"✅ 导航完成: {url}")
        except Exception as e:
            logger.error(f"导航失败: {e}")
            return f"❌ 导航失败: {type(e).__name__}: {e}"

        info = await _page_info(page)
        screenshot = await _save_screenshot(page)
        result = f"✅ 已导航到 {url}\n\n{info}\n\n"
        
        # 使用 token URL（不暴露绝对路径，防止 LLM 错误引用）
        if screenshot.get("token"):
            result += f"![截图](/api/screenshot?token={screenshot['token']})\n\n"
        
        if screenshot.get("size"):
            result += f"页面尺寸: {screenshot['size']}\n"
        return result

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 导航失败: {type(e).__name__}: {e}"


@tool
def browser_click(selector: str) -> str:
    """点击页面中指定的元素。

    参数:
      selector: CSS 选择器（如 #submit-btn, button.primary, a[href="/login"]）
    """
    async def _run():
        page = await _ensure_browser()
        try:
            await page.click(selector, timeout=10000)
            await asyncio.sleep(0.5)  # 等待可能的页面响应
            info = await _page_info(page)
            screenshot = await _save_screenshot(page)
            result = f"✅ 已点击: {selector}\n\n{info}\n\n"
            
            # 使用 token URL
            if screenshot.get("token"):
                result += f"![截图](/api/screenshot?token={screenshot['token']})\n\n"
            
            return result
        except Exception as e:
            return f"❌ 点击失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 点击失败: {type(e).__name__}: {e}"


@tool
def browser_fill(selector: str, value: str) -> str:
    """在页面输入框中填入文本。

    参数:
      selector: CSS 选择器（如 #username, input[name="email"]）
      value: 要填入的文本内容
    """
    async def _run():
        page = await _ensure_browser()
        try:
            await page.fill(selector, value, timeout=10000)
            return f"✅ 已填入: {selector} = \"{value[:50]}{'...' if len(value) > 50 else ''}\""
        except Exception as e:
            return f"❌ 填入失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 填入失败: {type(e).__name__}: {e}"


@tool
def browser_select(selector: str, value: str) -> str:
    """选择下拉框中的选项。

    参数:
      selector: <select> 元素的 CSS 选择器
      value: 要选择的 option 的 value 或 label
    """
    async def _run():
        page = await _ensure_browser()
        try:
            await page.select_option(selector, value, timeout=10000)
            return f"✅ 已选择: {selector} = {value}"
        except Exception as e:
            return f"❌ 选择失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 选择失败: {type(e).__name__}: {e}"


@tool
def browser_get_text(selector: str) -> str:
    """获取页面中指定元素的文本内容。

    参数:
      selector: CSS 选择器
    """
    async def _run():
        page = await _ensure_browser()
        try:
            element = await page.wait_for_selector(selector, timeout=10000)
            if not element:
                return f"❌ 未找到元素: {selector}"
            text = await element.inner_text()
            return text[:5000] + ("\n...（内容较长，已截断）" if len(text) > 5000 else "")
        except Exception as e:
            return f"❌ 获取文本失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 获取文本失败: {type(e).__name__}: {e}"


@tool
def browser_screenshot(full_page: bool = True) -> str:
    """截取当前浏览器页面的截图。

    参数:
      full_page: 是否截取完整页面（包括滚动部分），默认 true
    """
    async def _run():
        page = await _ensure_browser()
        ss = await _save_screenshot(page)
        info = await _page_info(page)
        parts = [f"📸 已截图\n\n{info}\n\n"]
        
        # 使用 token URL
        if ss.get("token"):
            parts.append(f"![截图](/api/screenshot?token={ss['token']})\n\n")
        
        if ss.get("size"):
            parts.append(f"尺寸: {ss['size']}\n")
        return "".join(parts)

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 截图失败: {type(e).__name__}: {e}"


@tool
def browser_evaluate(script: str) -> str:
    """在浏览器中执行 JavaScript 并返回结果。

    用于获取页面数据、检查元素状态、调用前端函数等。

    参数:
      script: JavaScript 代码字符串（如 "document.title"、"JSON.stringify(window.__INITIAL_STATE__)"）
    """
    async def _run():
        page = await _ensure_browser()
        try:
            result = await page.evaluate(script)
            text = str(result)
            return text[:5000] + ("\n...（结果较长，已截断）" if len(text) > 5000 else "")
        except Exception as e:
            return f"❌ JS 执行失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ JS 执行失败: {type(e).__name__}: {e}"


@tool
def browser_wait(ms: int = 2000) -> str:
    """等待指定毫秒数，常用于等待页面渲染或动画完成。

    参数:
      ms: 等待毫秒数（默认 2000）
    """
    async def _run():
        page = await _ensure_browser()
        await asyncio.sleep(ms / 1000)
        info = await _page_info(page)
        return f"⏳ 已等待 {ms}ms\n{info}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 等待失败: {type(e).__name__}: {e}"


@tool
def browser_takeover() -> str:
    """获取当前浏览器的完整控制权，返回当前页面状态。"""
    async def _run():
        page = await _ensure_browser()
        info = await _page_info(page)
        screenshot = await _save_screenshot(page)
        result = f"🌐 浏览器已就绪\n\n{info}\n\n"
        
        # 使用 token URL
        if screenshot.get("token"):
            result += f"![截图](/api/screenshot?token={screenshot['token']})\n\n"
        
        if screenshot.get("size"):
            result += f"页面尺寸: {screenshot['size']}\n"
        return result

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 浏览器初始化失败: {type(e).__name__}: {e}"


@tool
def browser_drag(source: str, target: str) -> str:
    """将页面元素拖拽到目标元素上（Drag-and-Drop）。

    用于实现拖拽排序、滑块验证、文件拖放等交互。

    参数:
      source: 被拖拽元素的 CSS 选择器
      target: 目标元素的 CSS 选择器
    """
    async def _run():
        page = await _ensure_browser()
        try:
            src_el = await page.wait_for_selector(source, timeout=10000)
            if not src_el:
                return f"❌ 未找到源元素: {source}"
            tgt_el = await page.wait_for_selector(target, timeout=10000)
            if not tgt_el:
                return f"❌ 未找到目标元素: {target}"
            
            # 获取源元素位置信息
            src_box = await src_el.bounding_box()
            await src_el.drag_to(tgt_el, timeout=10000)
            await asyncio.sleep(0.5)
            
            info = await _page_info(page)
            screenshot = await _save_screenshot(page)
            result = f"✅ 已将 {source} 拖拽到 {target}\n\n{info}\n\n"
            if screenshot.get("token"):
                result += f"![截图](/api/screenshot?token={screenshot['token']})\n\n"
            return result
        except Exception as e:
            return f"❌ 拖拽失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 拖拽失败: {type(e).__name__}: {e}"


@tool
def browser_slide(selector: str, offset_x: int, offset_y: int = 0) -> str:
    """水平或垂直滑动页面元素（模拟鼠标拖拽），常用于滑块验证码。

    通过模拟人类操作轨迹逐步移动鼠标，避免被反爬机制检测。
    支持任意方向的滑动（水平、垂直或斜向）。

    参数:
      selector: 滑块元素的 CSS 选择器
      offset_x: 水平滑动的像素距离（正数向右，负数向左）
      offset_y: 垂直滑动的像素距离（正数向下，负数向上），默认 0
    """
    async def _run():
        page = await _ensure_browser()
        try:
            el = await page.wait_for_selector(selector, timeout=10000)
            if not el:
                return f"❌ 未找到滑块元素: {selector}"
            
            box = await el.bounding_box()
            if not box:
                return f"❌ 无法获取元素位置: {selector}"
            
            # 起始位置：元素中心
            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2
            end_x = start_x + offset_x
            end_y = start_y + offset_y
            
            logger.info(f"🖱️ 开始滑动: ({start_x:.0f}, {start_y:.0f}) → ({end_x:.0f}, {end_y:.0f})")
            
            # 模拟人类滑动轨迹：先快速移动大部分距离，再缓慢微调
            await page.mouse.move(start_x, start_y)
            await page.mouse.down()
            
            # 生成人类化的运动轨迹（贝塞尔曲线模拟）
            steps = max(20, min(60, abs(offset_x) // 5 + abs(offset_y) // 5))
            for i in range(1, steps + 1):
                t = i / steps
                # 缓动函数：先快后慢（ease-out）
                eased = 1 - (1 - t) ** 2
                # 添加微小随机抖动，模拟人手的不稳定性
                import random
                jitter_x = random.uniform(-1.5, 1.5)
                jitter_y = random.uniform(-1.5, 1.5)
                x = start_x + offset_x * eased + jitter_x
                y = start_y + offset_y * eased + jitter_y
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.005, 0.015))
            
            await page.mouse.up()
            await asyncio.sleep(0.5)
            
            logger.info("✅ 滑动完成")
            info = await _page_info(page)
            screenshot = await _save_screenshot(page)
            result = f"✅ 已滑动 {selector} ({offset_x}px, {offset_y}px)\n\n{info}\n\n"
            if screenshot.get("token"):
                result += f"![截图](/api/screenshot?token={screenshot['token']})\n\n"
            if screenshot.get("size"):
                result += f"页面尺寸: {screenshot['size']}\n"
            return result
        except Exception as e:
            return f"❌ 滑动失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 滑动失败: {type(e).__name__}: {e}"


# ── 验证码识别（多模态 LLM） ─────────────────────────────────

# 系统级 LLM 客户端缓存：避免每个验证码调用都重建连接
_captcha_llm_client: Optional[httpx.AsyncClient] = None
_captcha_llm_config: Optional[dict] = None


def _get_captcha_config() -> dict:
    """读取当前激活的模型配置（从 config.json）。"""
    try:
        from config import AgentConfig  # 延迟导入避免循环
        cfg = AgentConfig.load()
        return {
            "base_url": cfg.base_url or "https://api.openai.com/v1",
            "api_key": cfg.api_key or "",
            "model": cfg.model or "gpt-4o",
        }
    except Exception as e:
        logger.warning("读取验证码模型配置失败: %s", e)
        return {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-4o"}


def _build_captcha_prompt() -> str:
    """构造多模态验证码识别 prompt。

    期望返回 JSON：
    {
      "type": "click" | "text" | "slider" | "unknown",
      "chars": "abc123",                  // text 类型：直接给出字母/数字
      "clicks": [
        {"char": "风", "x": 234, "y": 156}, // click 类型：依次点击的字符和坐标
        ...
      ],
      "confidence": 0.0~1.0,
      "explain": "..."
    }
    """
    return (
        "你是一个验证码识别专家。请观察图片中的验证码并判断其类型。\n\n"
        "可能类型：\n"
        "1. 文字输入验证码（type=text）：图片中显示一个或多个扭曲的字母/数字，"
        "用户需要按图片中的字符顺序输入。识别后输出 chars 字段（不含空格）。\n"
        "2. 文字点选验证码（type=click）：图片显示 1-6 个汉字或字符，"
        "页面提示用户按顺序依次点击。识别后：\n"
        "   - 在 clicks 数组中按页面要求的点击顺序给出每个字符及其在图片中的中心点像素坐标\n"
        "   - 图片原始宽高用 w/h 字段输出\n"
        "3. 滑块验证码（type=slider）：图片显示一个滑块缺口，返回 type=slider 即可，"
        "可附加说明缺口位置。\n"
        "4. 看图选物验证码（type=click）：图片要求按文字顺序点击物品，"
        "同样输出 clicks 列表。\n\n"
        "重要：\n"
        "- 像素坐标 (x, y) 是相对于图片左上角的整数\n"
        "- clicks 顺序必须严格遵守页面文字提示\n"
        "- 不要猜测看不清的字符，宁可降低 confidence\n"
        "- 字符之间有引号包裹的也按视觉顺序识别\n\n"
        "严格返回 JSON（不要包含其他文字或 Markdown 标记）：\n"
        "{\n"
        '  "type": "click|text|slider|unknown",\n'
        '  "chars": "字母数字字符串",\n'
        '  "clicks": [{"char": "字", "x": 0, "y": 0}],\n'
        '  "w": 图片宽度,\n'
        '  "h": 图片高度,\n'
        '  "confidence": 0.0,\n'
        '  "explain": "识别说明"\n'
        "}"
    )


def _extract_json_from_text(text: str) -> Optional[dict]:
    """从模型输出中提取 JSON（兼容 Markdown 包裹）。"""
    text = text.strip()
    # 去掉代码块围栏
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    # 尝试整体解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 提取首个 { ... } 子串
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


async def _call_vision_llm(png_data: bytes, config: dict) -> str:
    """调用多模态 LLM 识别验证码。返回原始文本。"""
    base_url = config["base_url"].rstrip("/")
    if not base_url.endswith("/v1"):
        # 自动补全 /v1（若用户填的是根 URL）
        if "/v1" not in base_url:
            base_url = base_url + "/v1"
    url = f"{base_url}/chat/completions"

    b64 = base64.b64encode(png_data).decode()
    payload = {
        "model": config["model"],
        "temperature": 0,
        "max_tokens": 600,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                    {"type": "text", "text": _build_captcha_prompt()},
                ],
            }
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}" if config["api_key"] else "Bearer none",
    }
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


def _image_dimensions(png_data: bytes) -> tuple[int, int]:
    """读取 PNG 尺寸（无 Pillow 时基于 IHDR 头解析）。"""
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(png_data))
        return img.size
    except Exception:
        pass
    # 兜底：从 PNG 头读取
    try:
        w = int.from_bytes(png_data[16:20], "big")
        h = int.from_bytes(png_data[20:24], "big")
        return w, h
    except Exception:
        return 0, 0


def _format_result(parsed: dict, img_w: int, img_h: int) -> str:
    """把识别结果格式化为给 Agent 的可读文本。"""
    ctype = (parsed.get("type") or "unknown").lower()
    conf = parsed.get("confidence", 0)
    explain = parsed.get("explain", "")

    lines = [f"🔐 验证码类型: {ctype}    置信度: {conf}"]
    lines.append(f"📐 验证码尺寸: {img_w} x {img_h}")

    if ctype == "text":
        chars = str(parsed.get("chars", "")).strip()
        lines.append(f"🔤 识别字符: `{chars}`")
        lines.append(f"→ 调用 browser_fill(captcha_selector, \"{chars}\") 填入")
    elif ctype == "click":
        clicks = parsed.get("clicks") or []
        if not clicks:
            lines.append("⚠️ 未识别到点击目标")
        else:
            lines.append(f"🎯 共 {len(clicks)} 个点击目标（按页面提示的顺序）:")
            for i, c in enumerate(clicks, 1):
                if not isinstance(c, dict):
                    continue
                char = c.get("char", "?")
                x = c.get("x", 0)
                y = c.get("y", 0)
                # 坐标按图片原始尺寸输出
                lines.append(f"  {i}. `{char}` @ ({x}, {y})")
            lines.append(
                "→ 在视口中找到验证码图片元素，"
                f"使用 page.mouse.click(element_box.x + {x or 0} * scale, ...) 依次点击"
            )
    elif ctype == "slider":
        lines.append("🧩 滑块验证码：先识别缺口位置，再用 browser_slide 拖动滑块")
    else:
        lines.append("❓ 无法识别验证码类型，请人工介入或刷新验证码重试")

    if explain:
        lines.append(f"💡 {explain}")
    return "\n".join(lines)


@tool
def browser_captcha_recognize(source: str = "page") -> str:
    """识别当前浏览器页面中的验证码图片（基于多模态大模型）。

    适用于以下场景的登录/注册流程：
      - 简单字母/数字验证码：直接返回要输入的字符
      - 文字点选验证码（"请依次点击 X Y Z"）：返回字符和点击坐标
      - 滑块验证码：标记为 slider 类型，告知需要拖动
      - 看图选物验证码：与点选相同处理

    参数:
      source: 识别图片来源，可选值
        - "page"（默认）：截取当前整页并识别
        - "selector:<css_selector>"：截取指定元素区域

    返回: JSON 字符串 + 友好说明，包含类型、置信度、字符或点击坐标。
    """
    async def _run():
        page = await _ensure_browser()
        try:
            # 1. 截取验证码图片
            if source == "page":
                png_data = await page.screenshot(full_page=False, timeout=30000)
            elif source.startswith("selector:"):
                sel = source[len("selector:"):].strip()
                el = await page.wait_for_selector(sel, timeout=10000)
                if not el:
                    return f"❌ 未找到元素: {sel}"
                png_data = await el.screenshot(timeout=30000)
            else:
                return f"❌ 不支持的 source: {source}"

            img_w, img_h = _image_dimensions(png_data)
            logger.info("🔍 验证码图片尺寸: %dx%d (%d bytes)", img_w, img_h, len(png_data))

            # 2. 调用多模态 LLM 识别
            config = _get_captcha_config()
            if not config.get("api_key"):
                return (
                    "❌ 未配置 LLM API Key。\n"
                    "请在设置中配置模型 API Key 后重试。\n"
                    "(Settings → 选择 Provider → 填入 API Key)"
                )

            try:
                text = await _call_vision_llm(png_data, config)
            except httpx.HTTPStatusError as e:
                return f"❌ 调用视觉 LLM 失败: HTTP {e.response.status_code} {e.response.text[:200]}"
            except Exception as e:
                return f"❌ 调用视觉 LLM 异常: {type(e).__name__}: {e}"

            logger.info("🧠 视觉 LLM 原始响应: %s", text[:300])
            parsed = _extract_json_from_text(text)
            if not parsed:
                return (
                    "⚠️ 视觉 LLM 返回无法解析的内容。\n"
                    f"原始输出:\n{text[:500]}\n\n"
                    "可重试或人工识别后用 browser_fill 填入。"
                )

            return _format_result(parsed, img_w, img_h)
        except Exception as e:
            return f"❌ 验证码识别失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 验证码识别失败: {type(e).__name__}: {e}"


@tool
def browser_captcha_click_sequence(
    selector: str, clicks: str, image_w: int = 0, image_h: int = 0
) -> str:
    """在验证码图片区域上按指定顺序模拟点击（用于点选型验证码）。

    参数:
      selector: 验证码图片元素的 CSS 选择器（用于计算缩放比例）
      clicks: JSON 字符串，格式 '[{"char":"字","x":100,"y":50}, ...]'
              x/y 是相对图片原始像素的坐标，工具会自动换算到视口坐标
      image_w: 验证码图片原始宽度（来自 browser_captcha_recognize 的输出）
      image_h: 验证码图片原始高度

    返回: 每次点击的结果
    """
    async def _run():
        page = await _ensure_browser()
        try:
            data = json.loads(clicks) if isinstance(clicks, str) else clicks
            if not isinstance(data, list) or not data:
                return "❌ clicks 参数必须是 JSON 数组"

            el = await page.wait_for_selector(selector, timeout=10000)
            if not el:
                return f"❌ 未找到验证码元素: {selector}"
            box = await el.bounding_box()
            if not box:
                return f"❌ 无法获取元素位置: {selector}"

            # 截图实际尺寸 vs 原始尺寸的比例
            actual_w = int(box["width"])
            actual_h = int(box["height"])
            if image_w and image_h:
                scale_x = actual_w / image_w
                scale_y = actual_h / image_h
            else:
                scale_x = scale_y = 1.0

            log = [f"🎯 即将在 {selector} 上点击 {len(data)} 次 (scale={scale_x:.2f}x{scale_y:.2f})"]
            for i, c in enumerate(data, 1):
                if not isinstance(c, dict):
                    continue
                x = float(c.get("x", 0)) * scale_x + box["x"]
                y = float(c.get("y", 0)) * scale_y + box["y"]
                char = c.get("char", "?")
                # 加入微小随机抖动，更像真人
                jx = random.uniform(-1.5, 1.5)
                jy = random.uniform(-1.5, 1.5)
                await page.mouse.click(x + jx, y + jy)
                await asyncio.sleep(random.uniform(0.3, 0.7))
                log.append(f"  {i}. 点击 `{char}` @ ({int(x)}, {int(y)})")

            await asyncio.sleep(0.5)
            screenshot = await _save_screenshot(page)
            result = "\n".join(log) + "\n\n✅ 点击序列完成\n"
            if screenshot.get("token"):
                result += f"![截图](/api/screenshot?token={screenshot['token']})\n"
            return result
        except Exception as e:
            return f"❌ 点击序列失败: {type(e).__name__}: {e}"

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 点击序列失败: {type(e).__name__}: {e}"


TOOLS = [
    browser_navigate,
    browser_click,
    browser_fill,
    browser_select,
    browser_get_text,
    browser_screenshot,
    browser_evaluate,
    browser_wait,
    browser_takeover,
    browser_drag,
    browser_slide,
    browser_captcha_recognize,
    browser_captcha_click_sequence,
]
