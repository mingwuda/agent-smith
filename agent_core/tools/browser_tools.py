"""浏览器自动化工具（基于 Playwright）

支持页面导航、元素交互、截图和前端 E2E 测试。
"""
import asyncio
import base64
import concurrent.futures
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

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
    """截取当前页面截图并返回 base64 和元信息（异步版本，在事件循环内直接 await）"""
    import io
    from PIL import Image as PILImage

    timestamp = int(time.time())
    screenshot_path = None
    if _workspace:
        screenshot_dir = _workspace / ".browser_screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / f"screenshot_{timestamp}.png"

    result = {"timestamp": timestamp}

    # 直接在事件循环中 await（避免死锁）
    png_data = await page.screenshot(full_page=True, timeout=30000)

    # 保存到工作区
    if screenshot_path:
        screenshot_path.write_bytes(png_data)
        result["path"] = str(screenshot_path)

    # 生成 base64 缩略图（CPU 密集型，放到线程池）
    try:
        img = PILImage.open(io.BytesIO(png_data))
        w, h = img.size
        result["size"] = f"{w}x{h}"
        if w > 800:
            ratio = 800 / w
            new_h = int(h * ratio)
            img = img.resize((800, new_h), PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        result["thumbnail_b64"] = base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        result["thumbnail_error"] = str(e)

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
        
        # 优先使用 base64 缩略图（直接嵌入 Markdown，无需 HTTP 请求）
        if screenshot.get("thumbnail_b64"):
            result += f"![截图](data:image/png;base64,{screenshot['thumbnail_b64']})\n\n"
        elif screenshot.get("path"):
            # 降级方案：使用 HTTP URL
            from urllib.parse import quote
            path_encoded = quote(screenshot['path'], safe='')
            result += f"![截图](/api/screenshot?path={path_encoded})\n\n"
            result += f"截图已保存: {screenshot['path']}\n"
        
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
            
            # 优先使用 base64 缩略图
            if screenshot.get("thumbnail_b64"):
                result += f"![截图](data:image/png;base64,{screenshot['thumbnail_b64']})\n\n"
            elif screenshot.get("path"):
                from urllib.parse import quote
                path_encoded = quote(screenshot['path'], safe='')
                result += f"![截图](/api/screenshot?path={path_encoded})\n\n"
                result += f"截图已保存: {screenshot['path']}\n"
            
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
        
        # 优先使用 base64 缩略图
        if ss.get("thumbnail_b64"):
            parts.append(f"![截图](data:image/png;base64,{ss['thumbnail_b64']})\n\n")
        elif ss.get("path"):
            from urllib.parse import quote
            path_encoded = quote(ss['path'], safe='')
            parts.append(f"![截图](/api/screenshot?path={path_encoded})\n\n")
            parts.append(f"已保存: {ss['path']}\n")
        
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
        
        # 优先使用 base64 缩略图
        if screenshot.get("thumbnail_b64"):
            result += f"![截图](data:image/png;base64,{screenshot['thumbnail_b64']})\n\n"
        elif screenshot.get("path"):
            from urllib.parse import quote
            path_encoded = quote(screenshot['path'], safe='')
            result += f"![截图](/api/screenshot?path={path_encoded})\n\n"
        
        if screenshot.get("size"):
            result += f"页面尺寸: {screenshot['size']}\n"
        if screenshot.get("path"):
            result += f"截图已保存: {screenshot['path']}\n"
        return result

    try:
        return _run_async(_run())
    except Exception as e:
        return f"❌ 浏览器初始化失败: {type(e).__name__}: {e}"


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
]
