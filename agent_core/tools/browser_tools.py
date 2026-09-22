"""浏览器自动化工具（基于 Playwright）

支持页面导航、元素交互、截图、验证码识别和前端 E2E 测试。

线程隔离：每个 LangGraph 会话（thread_id）拥有独立的浏览器页面，
避免不同会话之间的页面状态和截图串扰。
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
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 截图保留天数，超时自动清理（与日志滚动策略一致）
SCREENSHOT_RETENTION_DAYS = 7

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


# 每会话独立页面锁，key=thread_id，确保同一会话的操作串行
_page_locks: dict[str, asyncio.Lock] = {}

# 当前正在执行工具调用的 thread_id（用于追踪截图归属）
_active_thread_id: str = "default"


def _run_browser(coro, thread_id: str = "default") -> any:
    """在浏览器线程执行协程，使用 thread_id 对应的页面锁防止同一会话的并发冲突。

    不同 thread_id 的页面可并行操作，同一 thread_id 的操作串行执行。
    """
    global _active_thread_id
    _active_thread_id = thread_id  # 追踪当前操作的会话

    async def _locked():
        global _page_locks
        if thread_id not in _page_locks:
            _page_locks[thread_id] = asyncio.Lock()
        async with _page_locks[thread_id]:
            return await coro
    return _run_async(_locked())


def _stop_browser_loop():
    """停止浏览器事件循环（进程退出时调用）"""
    global _browser_loop, _browser_thread
    if _browser_loop is not None:
        _browser_loop.call_soon_threadsafe(_browser_loop.stop)
        if _browser_thread is not None:
            _browser_thread.join(timeout=5)
        _browser_loop = None
        _browser_thread = None


# 全局浏览器实例（跨会话共享同一个 Chromium 进程）
_browser = None
_playwright = None

# 每个会话独立页面（key=thread_id，避免会话间页面状态串扰）
_pages: dict[str, "Page"] = {}
_contexts: dict[str, "BrowserContext"] = {}

# 工作区（用于保存截图，ContextVar：每个 async 请求各自独立）
_workspace_ctx: ContextVar[Optional[Path]] = ContextVar("browser_tools_workspace", default=None)

# 进程退出时清理浏览器资源
import atexit
atexit.register(_stop_browser_loop)


def set_workspace(path: Path):
    _workspace_ctx.set(path.expanduser().resolve())
    # 工作区变更时清理过期截图
    _cleanup_expired_screenshots()


def _cleanup_expired_screenshots():
    """清理超过保留天数的截图文件（与日志滚动策略一致，默认 7 天）。"""
    _ws = _workspace_ctx.get()
    if not _ws:
        return
    screenshot_dir = _ws / ".browser_screenshots"
    if not screenshot_dir.is_dir():
        return
    cutoff = time.time() - SCREENSHOT_RETENTION_DAYS * 86400
    removed = 0
    for fpath in screenshot_dir.iterdir():
        if not fpath.is_file() or fpath.suffix.lower() != ".png":
            continue
        try:
            if fpath.stat().st_mtime < cutoff:
                fpath.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("🧹 已清理 %d 个过期截图文件（超过 %d 天）",
                     removed, SCREENSHOT_RETENTION_DAYS)


async def _ensure_browser(thread_id: str = "default"):
    """惰性初始化浏览器实例，每个 thread_id 创建独立的上下文和页面。

    浏览器进程（Chromium）全局唯一，但每个会话拥有独立的
    BrowserContext（隔离 cookie/localStorage）和 Page。
    """
    global _browser, _playwright

    # 已有该会话的页面 → 直接返回
    if thread_id in _pages and _pages[thread_id] is not None:
        return _pages[thread_id]

    from playwright.async_api import async_playwright

    # 首次启动浏览器（全局唯一，只启动一次）
    if _browser is None:
        # 在浏览器事件循环中用 asyncio.Lock 防止重复启动
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
        logger.info("✅ 浏览器已启动（全局共享）")

    # 为该会话创建独立的 BrowserContext 和 Page
    ctx = await _browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    page = await ctx.new_page()
    _contexts[thread_id] = ctx
    _pages[thread_id] = page

    logger.info("✅ 已为会话 %s 创建独立浏览器页面", thread_id[:16])
    return page


async def _close_browser():
    """关闭浏览器实例（释放所有会话的页面）"""
    global _browser, _playwright, _pages, _contexts
    try:
        # 关闭所有独立上下文和页面
        for tid, ctx in list(_contexts.items()):
            try:
                await ctx.close()
            except Exception:
                pass
        _contexts.clear()
        _pages.clear()

        # 关闭浏览器进程
        if _browser:
            await _browser.close()
        if _playwright:
            await _playwright.stop()
    except Exception:
        pass
    finally:
        _browser = None
        _playwright = None


def release_browser_page(thread_id: str):
    """释放指定会话的浏览器页面和上下文。

    在会话结束时调用，释放不再使用的资源。
    非阻塞：将清理任务提交到浏览器事件循环后立即返回。

    截图文件位于磁盘，与浏览器是否初始化无关，因此无条件先清理，
    ponytail: 避免浏览器未启动时（_browser is None）跳过清理导致旧截图 token 残留、继续渲染旧图。
    """
    # 截图文件在磁盘上，独立于浏览器状态，无论浏览器是否启动都先清理
    _cleanup_screenshots(thread_id)

    # 浏览器从未初始化 → 无需释放页面/上下文
    if _browser is None:
        return

    async def _release():
        global _pages, _contexts, _page_locks
        if thread_id in _contexts:
            try:
                await _contexts[thread_id].close()
            except Exception:
                pass
            del _contexts[thread_id]
        _pages.pop(thread_id, None)
        _page_locks.pop(thread_id, None)

        logger.info("🧹 已释放会话 %s 的浏览器页面", thread_id[:16])
    try:
        loop = _ensure_browser_loop()
        # fire-and-forget: 不阻塞等待结果，避免在 finally 块中阻塞 SSE 流
        asyncio.run_coroutine_threadsafe(_release(), loop)
    except Exception:
        pass


# 记录每个 thread_id 产生过的截图 token，用于精确清理
_session_screenshot_tokens: dict[str, set[str]] = {}


def _register_screenshot_token(thread_id: str, token: str):
    """记录某会话产生的截图 token，供 release 时清理"""
    if thread_id not in _session_screenshot_tokens:
        _session_screenshot_tokens[thread_id] = set()
    _session_screenshot_tokens[thread_id].add(token)


def _cleanup_screenshots(thread_id: str):
    """清理指定会话的所有截图文件"""
    global _session_screenshot_tokens
    tokens = _session_screenshot_tokens.pop(thread_id, set())
    _ws = _workspace_ctx.get()
    if not tokens or not _ws:
        return
    screenshot_dir = _ws / ".browser_screenshots"
    removed = 0
    for token in tokens:
        fpath = screenshot_dir / f"{token}.png"
        try:
            if fpath.exists():
                fpath.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info("🗑️ 已清理会话 %s 的 %d 个截图文件", thread_id[:16], removed)


async def _save_screenshot(page) -> dict:
    """截取当前页面截图并保存到工作区（异步版本）"""
    timestamp = int(time.time())
    screenshot_path = None
    _ws = _workspace_ctx.get()
    if _ws:
        screenshot_dir = _ws / ".browser_screenshots"
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

        # 记录截图归属，会话结束时自动清理
        _register_screenshot_token(_active_thread_id, result["token"])
        
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
def browser_navigate(url: str, config: RunnableConfig) -> str:
    """导航到指定 URL 并返回页面标题和截图。

    参数:
      url: 完整的网页地址（包含 http:// 或 https://）
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            logger.info(f"🌐 正在导航到: {url}")
            # 先尝试 networkidle，失败后用 domcontentloaded 兜底
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
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
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 导航失败: {type(e).__name__}: {e}"


@tool
def browser_click(selector: str, config: RunnableConfig) -> str:
    """点击页面中指定的元素。

    参数:
      selector: CSS 选择器（如 #submit-btn, button.primary, a[href="/login"]）
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
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
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 点击失败: {type(e).__name__}: {e}"


@tool
def browser_fill(selector: str, value: str, config: RunnableConfig) -> str:
    """在页面输入框中填入文本。

    参数:
      selector: CSS 选择器（如 #username, input[name="email"]）
      value: 要填入的文本内容
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            await page.fill(selector, value, timeout=10000)
            return f"✅ 已填入: {selector} = \"{value[:50]}{'...' if len(value) > 50 else ''}\""
        except Exception as e:
            return f"❌ 填入失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 填入失败: {type(e).__name__}: {e}"


@tool
def browser_select(selector: str, value: str, config: RunnableConfig) -> str:
    """选择下拉框中的选项。

    参数:
      selector: <select> 元素的 CSS 选择器
      value: 要选择的 option 的 value 或 label
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            await page.select_option(selector, value, timeout=10000)
            return f"✅ 已选择: {selector} = {value}"
        except Exception as e:
            return f"❌ 选择失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 选择失败: {type(e).__name__}: {e}"


@tool
def browser_get_text(selector: str, config: RunnableConfig) -> str:
    """获取页面中指定元素的文本内容。

    参数:
      selector: CSS 选择器
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            element = await page.wait_for_selector(selector, timeout=10000)
            if not element:
                return f"❌ 未找到元素: {selector}"
            text = await element.inner_text()
            return text[:5000] + ("\n...（内容较长，已截断）" if len(text) > 5000 else "")
        except Exception as e:
            return f"❌ 获取文本失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 获取文本失败: {type(e).__name__}: {e}"


@tool
def browser_screenshot(config: RunnableConfig, full_page: bool = True) -> str:
    """截取当前浏览器页面的截图。

    参数:
      full_page: 是否截取完整页面（包括滚动部分），默认 true
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
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
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 截图失败: {type(e).__name__}: {e}"


@tool
def browser_evaluate(script: str, config: RunnableConfig) -> str:
    """在浏览器中执行 JavaScript 并返回结果。

    用于获取页面数据、检查元素状态、调用前端函数等。

    参数:
      script: JavaScript 代码字符串（如 "document.title"、"JSON.stringify(window.__INITIAL_STATE__)"）
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            result = await page.evaluate(script)
            text = str(result)
            return text[:5000] + ("\n...（结果较长，已截断）" if len(text) > 5000 else "")
        except Exception as e:
            return f"❌ JS 执行失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ JS 执行失败: {type(e).__name__}: {e}"


@tool
def browser_wait(config: RunnableConfig, ms: int = 2000) -> str:
    """等待指定毫秒数，常用于等待页面渲染或动画完成。

    参数:
      ms: 等待毫秒数（默认 2000）
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        await asyncio.sleep(ms / 1000)
        info = await _page_info(page)
        return f"⏳ 已等待 {ms}ms\n{info}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 等待失败: {type(e).__name__}: {e}"


@tool
def browser_scroll_to(config: RunnableConfig, selector: str = "", x: int = -1, y: int = -1) -> str:
    """滚动页面到指定元素或坐标位置。

    用于页面上元素未在当前视口中可见时，先滚动到目标位置再操作。
    支持两种模式：
      1. CSS 选择器模式：传入 selector，自动滚动直到元素可见
      2. 坐标模式：传入 x, y 坐标直接滚动到该位置

    参数:
      selector: CSS 选择器（如 #captcha-box、.footer）
      x: 目标 X 坐标（配合 y 使用，selector 为空时生效）
      y: 目标 Y 坐标

    返回: 滚动后的页面位置信息。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            if selector:
                el = await page.wait_for_selector(selector, timeout=10000)
                if not el:
                    return f"❌ 未找到元素: {selector}"
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(0.3)
                box = await el.bounding_box()
                info = f"✅ 已滚动到元素: {selector}"
                if box:
                    info += f"\n元素位置: ({int(box['x'])}, {int(box['y'])}) 尺寸: {int(box['width'])}×{int(box['height'])}"
            elif x >= 0 or y >= 0:
                sx = max(0, x)
                sy = max(0, y)
                await page.evaluate(f"window.scrollTo({sx}, {sy})")
                await asyncio.sleep(0.3)
                info = f"✅ 已滚动到坐标: ({sx}, {sy})"
            else:
                return "❌ 请提供 selector 或 x/y 坐标"

            page_info = await _page_info(page)
            return f"{info}\n\n{page_info}"
        except Exception as e:
            return f"❌ 滚动失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 滚动失败: {type(e).__name__}: {e}"


@tool
def browser_wait_for_element(selector: str, config: RunnableConfig, timeout: int = 15000) -> str:
    """等待页面中指定元素出现并变为可见。

    用于页面是 SPA/动态加载时，等待某个元素（如登录按钮、验证码区域）
    加载完成后再进行后续操作。

    参数:
      selector: CSS 选择器
      timeout: 超时毫秒数，默认 15000（15 秒）

    返回: 元素状态信息。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            el = await page.wait_for_selector(selector, timeout=timeout, state="visible")
            if not el:
                return f"❌ 超时未找到元素: {selector}（{timeout}ms）"
            box = await el.bounding_box()
            tag = await page.evaluate("(el) => el.tagName.toLowerCase()", el)
            info = f"✅ 元素已可见: {selector}\n标签: <{tag}>"
            if box:
                info += f"\n位置: ({int(box['x'])}, {int(box['y'])}) 尺寸: {int(box['width'])}×{int(box['height'])}"
            return info
        except Exception as e:
            return f"❌ 等待元素失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 等待元素失败: {type(e).__name__}: {e}"


@tool
def browser_takeover(config: RunnableConfig) -> str:
    """获取当前浏览器的完整控制权，返回当前页面状态。"""
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
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
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 浏览器初始化失败: {type(e).__name__}: {e}"


@tool
def browser_drag(source: str, target: str, config: RunnableConfig) -> str:
    """将页面元素拖拽到目标元素上（Drag-and-Drop）。

    用于实现拖拽排序、滑块验证、文件拖放等交互。

    参数:
      source: 被拖拽元素的 CSS 选择器
      target: 目标元素的 CSS 选择器
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
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
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 拖拽失败: {type(e).__name__}: {e}"


@tool
def browser_slide(selector: str, offset_x: int, config: RunnableConfig, offset_y: int = 0) -> str:
    """水平或垂直滑动页面元素（模拟鼠标拖拽），常用于滑块验证码。

    通过模拟人类操作轨迹逐步移动鼠标，避免被反爬机制检测。
    支持任意方向的滑动（水平、垂直或斜向）。

    参数:
      selector: 滑块元素的 CSS 选择器
      offset_x: 水平滑动的像素距离（正数向右，负数向左）
      offset_y: 垂直滑动的像素距离（正数向下，负数向上），默认 0
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
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
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 滑动失败: {type(e).__name__}: {e}"


async def _try_scroll_into_view(page, selector: str):
    """尝试将元素滚动到视口内，captcha 识别前调用（忽略失败）"""
    try:
        el = await page.query_selector(selector)
        if el:
            await el.scroll_into_view_if_needed()
            await asyncio.sleep(0.2)
    except Exception:
        pass


# ── 验证码识别（多模态 LLM） ─────────────────────────────────

# 系统级 LLM 客户端缓存：避免每个验证码调用都重建连接
_captcha_llm_client: Optional[httpx.AsyncClient] = None
_captcha_llm_config: Optional[dict] = None


def _get_captcha_config() -> dict:
    """读取当前激活的模型配置（从 config.json）。"""
    try:
                # 动态查找 config 模块（兼容 agent_core/ 和 project_root/ 两种启动方式）
        import importlib
        try:
            cfg_mod = importlib.import_module("config")
        except ImportError:
            cfg_mod = importlib.import_module("agent_core.config")
        AgentConfig = cfg_mod.AgentConfig
        cfg = AgentConfig.load()
        return {
            "base_url": cfg.base_url or "https://api.openai.com/v1",
            "api_key": cfg.api_key or "",
            "model": cfg.model or "gpt-4o",
        }
    except Exception as e:
        logger.warning("读取验证码模型配置失败: %s", e)
        return {"base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-4o"}


def _build_captcha_prompt(is_page_level: bool = False) -> str:
    """构造验证码识别 prompt。
    
    is_page_level=True 时只做类型检测，不做精确坐标定位（页面全图图标太小）。
    """
    
    if is_page_level:
        return (
            "判断图片中是否包含验证码（CAPTCHA）元素并返回 JSON。\n\n"
            "类型：\n"
            "1. text：图片中有扭曲字母或数字验证码，输出 chars。\n"
            "2. click：点选验证码（汉字或图标点选），输出 clicks 数组。\n"
            "   注意：这是整页截图，图标很小。请尽可能给出每个目标的大致像素坐标。\n"
            "   如果不确定精确位置，可以设置 confidence<0.7。\n"
            "3. slider：滑块验证码。\n"
            "4. unknown：没有任何验证码。\n\n"
            "⚠️ 特别注意：\n"
            "- 很多网站的图标点选验证码看起来像装饰性图标面板（建筑剪影、动物、水果、"
            "交通标志等），但只要带有\"刷新/换一批\"按钮，就极可能是验证码。\n"
            "- 不要因为图标看起来像 UI 装饰元素就判断为 unknown。\n"
            "- 如果图片右侧或中间有一个带刷新按钮的图标区域，优先判断为 click。\n"
            "- ⭐ 只输出要求点击的图标，不要列出验证码区域中的所有图标。"
            "通常页面只要求点击 2~3 个。如果看到 6~9 个图标，只有其中几个是需要点的。\n\n"
            "返回 JSON 格式：\n"
            '{"type":"click|text|slider|unknown","chars":"","clicks":[{"char":"汉字或图标名","x":0,"y":0}],"w":宽度,"h":高度,"confidence":0.0,"explain":"说明"}\n'
            "只输出 JSON，不要 Markdown 包裹。"
        )
    return (
        "识别图片中的验证码元素并返回精确坐标 JSON。\n\n"
        "图片是验证码区域的特写截图（不是整页），请精确识别每个点击目标。\n\n"
        "类型：\n"
        "1. click：点选验证码，图片中散落着图标或汉字字符，"
        "需要按页面提示的顺序依次点击。输出 clicks 数组：\n"
        "   - 汉字点选：char 写实际汉字（如 发、送、验）\n"
        "   - 图标点选：char 写图标名称（如 皇冠、眼睛、建筑、铃铛）\n"
        "   - 目标通常分散在验证码区域的不同位置\n"
        "2. text：扭曲字母数字，输出 chars。\n"
        "3. slider：滑块缺口。\n"
        "4. unknown：不是验证码。\n\n"
        "⚠️ 特别注意：\n"
        "- 图标点选验证码的图标可能是建筑剪影、动物、水果、日常用品等，"
        "看起来像装饰元素，但它们是可点击的验证码目标。\n"
        "- 如果图片中有\"刷新/换一批\"按钮、\"请按顺序点击\"等提示文字，"
        "即使图标看起来像 UI 装饰，也一定是 click 类型验证码。\n"
        "- 不要因为图标风格简洁现代就判断为 unknown。\n"
        "- ⭐ 只输出要求点击的图标，不要列出验证码区域中的所有图标。"
        "验证码区域可能展示 6~9 个图标，但通常只要求点击其中 2~3 个。\n"
        "优先参考页面提示文字（instruction_hint）来确认点击目标。\n\n"
        "重要：坐标 (x,y) 相对于图片左上角，看不清就降低 confidence。\n\n"
        "返回 JSON 格式：\n"
        '{"type":"click|text|slider|unknown","chars":"","clicks":[{"char":"汉字或图标名","x":0,"y":0}],"w":宽度,"h":高度,"confidence":0.0,"explain":"说明"}\n'
        "只输出 JSON，不要 Markdown 包裹。"
    )


def _extract_json_from_text(text: str) -> Optional[dict]:
    """从模型输出中提取 JSON（兼容 Markdown 包裹和截断）。"""
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
        candidate = m.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 可能是截断的 JSON：尝试补全
            pass
    # 截断容错：找到第一个 {，然后按行尝试逐步关闭
    brace_start = text.find("{")
    if brace_start >= 0:
        partial = text[brace_start:]
        # 尝试用 parse 或修复器
        try:
            # 修复常见截断：末尾是 "explain": " 缺少闭合
            # 补全闭合引号和括号
            fixed = partial
            # 如果以未闭合的字符串结束，关闭它
            if fixed.count('"') % 2 == 1:
                fixed += '"'
            # 补全未闭合的 []
            open_brackets = fixed.count("[") - fixed.count("]")
            if open_brackets > 0:
                fixed += "]" * open_brackets
            # 补全未闭合的 {}
            open_braces = fixed.count("{") - fixed.count("}")
            if open_braces > 0:
                fixed += "}" * open_braces
            return json.loads(fixed)
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _normalize_captcha_result(parsed: dict, img_w: int, img_h: int) -> dict:
    """把模型返回收敛成工具能安全消费的形状。"""
    ctype = str(parsed.get("type") or "unknown").strip().lower()
    if ctype in {"文字", "ocr", "code"}:
        ctype = "text"
    elif ctype in {"点选", "icon", "icons"}:
        ctype = "click"
    elif ctype in {"slide", "drag"}:
        ctype = "slider"
    elif ctype not in {"text", "click", "slider", "unknown"}:
        ctype = "unknown"
    parsed["type"] = ctype

    try:
        parsed["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence", 0) or 0)))
    except (TypeError, ValueError):
        parsed["confidence"] = 0.0

    if ctype == "text":
        parsed["chars"] = re.sub(r"\s+", "", str(parsed.get("chars") or ""))

    if ctype == "click":
        clicks = []
        for c in parsed.get("clicks") or []:
            if not isinstance(c, dict):
                continue
            try:
                x = float(c.get("x"))
                y = float(c.get("y"))
            except (TypeError, ValueError):
                continue
            if img_w > 0:
                x = max(0, min(img_w - 1, x))
            if img_h > 0:
                y = max(0, min(img_h - 1, y))
            clicks.append({"char": str(c.get("char") or c.get("label") or "?"), "x": round(x), "y": round(y)})
        parsed["clicks"] = clicks

    return parsed


def _offset_clicks(parsed: dict, offset_x: float, offset_y: float) -> dict:
    """把裁剪图坐标换算回视口坐标。"""
    if parsed.get("type") != "click" or not (offset_x or offset_y):
        return parsed
    parsed = dict(parsed)
    parsed["clicks"] = [
        {**c, "x": round(float(c.get("x", 0)) + offset_x), "y": round(float(c.get("y", 0)) + offset_y)}
        for c in parsed.get("clicks") or []
        if isinstance(c, dict)
    ]
    return parsed


def _captcha_should_send_max_tokens(base_url: str) -> bool:
    return "stepfun.com" not in base_url


async def _captcha_instruction_hint(page) -> str:
    """扫描页面上的验证码指示文字（如“请依次点击XXX”）。"""
    return await page.evaluate("""
    (() => {
      const priorityKeywords = ['请依次点击', '依次点击', '按顺序点击', '请点击以下', '点击下图'];
      const secondaryKeywords = ['点选验证', '点击验证', '滑块验证', '验证码', 'captcha', 'verify'];
      const texts = [];
      const els = document.querySelectorAll('p, span, div, label, h1, h2, h3, i, b, strong, .captcha-tip, .verify-tip, .nc-lang-cnt, .slider-captcha');
      for (const el of els) {
        const t = (el.textContent || '').trim().replace(/\\s+/g, ' ');
        if (t.length > 2 && t.length < 160 && el.offsetParent !== null) {
          if (priorityKeywords.some(k => t.includes(k))) texts.unshift('[指令] ' + t);
          else if (secondaryKeywords.some(k => t.toLowerCase().includes(k))) texts.push('[相关] ' + t);
        }
      }
      return [...new Set(texts)].slice(0, 8).join(' | ');
    })()
    """)


async def _detect_captcha_clip(page) -> Optional[dict]:
    """在整页模式下尝试裁出验证码区域，失败就退回普通视口截图。"""
    # ponytail: DOM 关键词启发式，适合常见登录验证码；复杂自绘/跨域 iframe 以后再升级成可配置候选选择器。
    return await page.evaluate("""
    (() => {
      const kw = /captcha|verify|verification|validate|vcode|verify.?code|验证码|点选|滑块|刷新|换一批/i;
      const vw = window.innerWidth, vh = window.innerHeight;
      const visible = el => {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return r.width >= 40 && r.height >= 20 && r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw &&
               style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
      };
      const score = el => {
        const r = el.getBoundingClientRect();
        const hay = [el.id, el.className, el.getAttribute('src'), el.getAttribute('alt'), el.getAttribute('title'), el.textContent]
          .join(' ').slice(0, 500);
        let s = kw.test(hay) ? 80 : 0;
        if (['IMG', 'CANVAS'].includes(el.tagName)) s += 25;
        if (r.width >= 120 && r.width <= 520 && r.height >= 30 && r.height <= 420) s += 20;
        if (/刷新|换一批|captcha|verify|验证码/i.test(hay)) s += 15;
        return s;
      };
      const candidates = [...document.querySelectorAll('img, canvas, svg, [class], [id], input, button')]
        .filter(visible)
        .map(el => ({el, score: score(el)}))
        .filter(x => x.score > 0)
        .sort((a, b) => b.score - a.score);
      if (!candidates.length) return null;
      let el = candidates[0].el;
      for (let i = 0; i < 3 && el.parentElement; i++) {
        const r = el.getBoundingClientRect();
        const pr = el.parentElement.getBoundingClientRect();
        const parentText = (el.parentElement.textContent || '').slice(0, 300);
        if (kw.test(parentText) && pr.width <= 620 && pr.height <= 520 && pr.width * pr.height <= Math.max(r.width * r.height * 8, 60000)) {
          el = el.parentElement;
        }
      }
      const r = el.getBoundingClientRect();
      const pad = 12;
      const bottomPad = 64;
      const left = Math.max(0, r.left - pad), top = Math.max(0, r.top - pad);
      const right = Math.min(vw, r.right + pad), bottom = Math.min(vh, r.bottom + pad + bottomPad);
      if (right - left < 40 || bottom - top < 20) return null;
      return {
        x: left + window.scrollX,
        y: top + window.scrollY,
        width: right - left,
        height: bottom - top,
        viewportX: left,
        viewportY: top,
        selector: el.id ? '#' + CSS.escape(el.id) : ''
      };
    })()
    """)


async def _call_vision_llm(png_data: bytes, config: dict, instruction_hint: str = "", img_w: int = 0, img_h: int = 0, is_page_level: bool = False) -> str:
    """调用多模态 LLM 识别验证码。返回原始文本。

    参数:
      png_data: 截图 PNG 数据
      config: 模型配置
      instruction_hint: 页面上的验证码提示文字
      img_w: 截图实际宽度（用于提示 LLM 坐标范围）
      img_h: 截图实际高度
    """
    
    base_url = config["base_url"].rstrip("/")
    if not base_url.endswith("/v1"):
        if "/v1" not in base_url:
            base_url = base_url + "/v1"
    url = f"{base_url}/chat/completions"

    b64 = base64.b64encode(png_data).decode()
    prompt = _build_captcha_prompt(is_page_level)
    # 告知 LLM 实际图片尺寸，避免坐标偏离
    if img_w and img_h:
        prompt += f"\n\n图片实际尺寸: {img_w}x{img_h}"
    if instruction_hint:
        prompt += f"\n\n页面提示文字：{instruction_hint}\n注意：图中按此顺序点击。"

    payload = {
        "model": config["model"],
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    if _captcha_should_send_max_tokens(base_url):
        payload["max_tokens"] = 1024
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['api_key']}" if config["api_key"] else "Bearer none",
    }
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    content = data["choices"][0]["message"]["content"]
    finish_reason = data["choices"][0].get("finish_reason", "")
    if finish_reason == "length":
        logger.warning("视觉 LLM 响应因 max_tokens 达到上限被截断 (finish_reason=length)")
        if not content or len(content.strip()) < 10:
            logger.error("截断后内容为空: prompt_len=%d, response_empty=%s", len(prompt), not content)
            if "max_tokens" in payload:
                logger.info("视觉 LLM 空响应，去掉 max_tokens 后重试一次")
                payload.pop("max_tokens", None)
                async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                content = data["choices"][0]["message"]["content"]
                finish_reason = data["choices"][0].get("finish_reason", "")
                logger.info("视觉 LLM 重试结果: finish_reason=%s, content_len=%d", finish_reason, len(content or ""))
    return content


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


def _format_result(parsed: dict, img_w: int, img_h: int, source: str = "page") -> str:
    """把识别结果格式化为给 Agent 的可读文本。

    source 参数用于生成正确的工具调用指引：
      - "page": 坐标相对于视口 → 引导调用 browser_click_captcha
      - "selector:...": 坐标相对于元素 → 引导调用 browser_captcha_click_sequence
    """
    
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
            # 根据源模式给出正确的工具调用指引
            clicks_json = json.dumps(clicks, ensure_ascii=False)
            if source == "page":
                lines.append(
                    "→ 坐标相对于视口左上角，调用 browser_click_captcha 执行点击：\n"
                    f"  browser_click_captcha(clicks='{clicks_json}')"
                )
            elif source.startswith("selector:"):
                sel = source[len("selector:"):].strip()
                lines.append(
                    "→ 坐标相对于验证码元素左上角，调用 browser_captcha_click_sequence 执行点击：\n"
                    f"  browser_captcha_click_sequence(selector=\"{sel}\", "
                    f"clicks='{clicks_json}', "
                    f"image_w={img_w}, image_h={img_h})"
                )
            # 低置信度时建议刷新验证码重试
            if conf < 0.5:
                lines.append(
                    "⚠️ 置信度较低（坐标可能不可靠）。建议先点击验证码的刷新/换一批按钮"
                    "获取新验证码，再重新调用 browser_captcha_recognize 识别。\n"
                    "  刷新工具: browser_captcha_refresh(selector=\"验证码容器选择器\")"
                )
    elif ctype == "slider":
        lines.append("🧩 滑块验证码：先识别缺口位置，再用 browser_slide 拖动滑块")
    else:
        lines.append("❓ 无法识别验证码类型，请人工介入或刷新验证码重试")

    if explain:
        lines.append(f"💡 {explain}")
    return "\n".join(lines)


@tool
def browser_captcha_recognize(config: RunnableConfig, source: str = "page") -> str:
    """识别当前浏览器页面中的验证码图片（基于多模态大模型）。

    适用于以下场景的登录/注册流程：
      - 简单字母/数字验证码：直接返回要输入的字符
      - 文字点选验证码（"请依次点击 X Y Z"）：返回字符和点击坐标
      - 滑块验证码：标记为 slider 类型，告知需要拖动
      - 看图选物验证码：与点选相同处理

    参数:
      source: 识别图片来源，可选值
        - "page"（默认）：截取当前整页并识别
        - "selector:<css_selector>"：截取指定元素区域（如 .captcha-img、#captcha-box、img[src*="captcha"]）

        注意：截取整页时 LLM 可能被页面其他文字干扰，建议优先使用 selector 定位验证码图片。

    返回: JSON 字符串 + 友好说明，包含类型、置信度、字符或点击坐标。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            # 1. 截取验证码图片
            clip_info = None
            if source == "page":
                clip_info = await _detect_captcha_clip(page)
                if clip_info:
                    clip = {k: clip_info[k] for k in ("x", "y", "width", "height")}
                    png_data = await page.screenshot(clip=clip, timeout=30000)
                else:
                    png_data = await page.screenshot(full_page=False, timeout=30000)
            elif source.startswith("selector:"):
                sel = source[len("selector:"):].strip()
                await _try_scroll_into_view(page, sel)
                el = await page.wait_for_selector(sel, timeout=10000)
                if not el:
                    return f"❌ 未找到元素: {sel}"
                png_data = await el.screenshot(timeout=30000)
            else:
                return f"❌ 不支持的 source: {source}"

            img_w, img_h = _image_dimensions(png_data)
            logger.info("🔍 验证码图片尺寸: %dx%d (%d bytes)", img_w, img_h, len(png_data))

            # 2. 扫描页面上的验证码指示文字（如"请依次点击XXX"）
            instruction_hint = await _captcha_instruction_hint(page)
            if instruction_hint:
                logger.info("📝 捕获到验证码提示文字: %s", instruction_hint[:150])

            # 3. 保存截图到工作区，方便 Agent 查看识别的是什么图片
            img_token = ""
            _ws = _workspace_ctx.get()
            if _ws:
                screenshot_dir = _ws / ".browser_screenshots"
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                ts = int(time.time())
                cap_path = screenshot_dir / f"captcha_{ts}.png"
                cap_path.write_bytes(png_data)
                img_token = cap_path.stem

            # 4. 调用多模态 LLM 识别（附带页面提示文字）
            config = _get_captcha_config()
            if not config.get("api_key"):
                if img_token:
                    return (
                        "❌ 未配置 LLM API Key。\n"
                        "请在设置中配置模型 API Key 后重试。\n\n"
                        f"![截图](/api/screenshot?token={img_token})"
                    )
                return (
                    "❌ 未配置 LLM API Key。\n"
                    "请在设置中配置模型 API Key 后重试。\n"
                    "(Settings → 选择 Provider → 填入 API Key)"
                )

            try:
                text = await _call_vision_llm(
                    png_data,
                    config,
                    instruction_hint,
                    img_w,
                    img_h,
                    is_page_level=(source == "page" and not clip_info),
                )
            except httpx.HTTPStatusError as e:
                err = f"❌ 调用视觉 LLM 失败: HTTP {e.response.status_code} {e.response.text[:200]}"
                if img_token:
                    err += f"\n\n![截图](/api/screenshot?token={img_token})"
                return err
            except Exception as e:
                err = f"❌ 调用视觉 LLM 异常: {type(e).__name__}: {e}"
                if img_token:
                    err += f"\n\n![截图](/api/screenshot?token={img_token})"
                return err

            logger.info("🧠 视觉 LLM 原始响应: %s", text[:300])
            parsed = _extract_json_from_text(text)
            if not parsed:
                result = (
                    "⚠️ 视觉 LLM 返回无法解析的内容。\n"
                    f"原始输出:\n{text[:500]}\n\n"
                    "可重试或人工识别后用 browser_fill 填入。"
                )
                if img_token:
                    result += f"\n\n![截图](/api/screenshot?token={img_token})"
                return result

            parsed = _normalize_captcha_result(parsed, img_w, img_h)

            # Jev 二次置信度校验：识别结果可疑时提示刷新，减少"识别错→白点"无效动作。
            # ponytail: 同步调用（≤2s 超时）跑在浏览器线程会短暂阻塞该会话的页面锁，
            # 但验证码识别本身已是秒级操作，可接受；失败静默跳过不影响主流程。
            try:
                from tools.jev_tools import captcha_confidence
                _jev_conf = captcha_confidence(
                    f"captcha_type={parsed.get('type')} result={json.dumps(parsed, ensure_ascii=False)[:300]}",
                )
                if _jev_conf is not None and _jev_conf < 0.5:
                    parsed["confidence"] = min(float(parsed.get("confidence", 0) or 0), _jev_conf)
                    parsed["explain"] = (str(parsed.get("explain", "")) + " [Jev 交叉校验判定识别结果不可靠]").strip()
            except Exception:
                pass  # Jev 不可用/失败 → 静默跳过，保持原识别结果

            result_w, result_h = img_w, img_h
            if source == "page" and clip_info:
                parsed = _offset_clicks(parsed, clip_info.get("viewportX", 0), clip_info.get("viewportY", 0))
                viewport = await page.evaluate("({w: window.innerWidth, h: window.innerHeight})")
                result_w, result_h = int(viewport["w"]), int(viewport["h"])

            # 4. 将识别结果截图也附上，方便 Agent 确认
            fmt = _format_result(parsed, result_w, result_h, source)
            if source == "page" and clip_info:
                fmt += "\n📎 已自动裁剪疑似验证码区域识别，点击坐标已换算为当前视口坐标"
            if img_token:
                fmt += f"\n\n![识别来源](/api/screenshot?token={img_token})"
            return fmt
        except Exception as e:
            return f"❌ 验证码识别失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 验证码识别失败: {type(e).__name__}: {e}"


@tool
def browser_captcha_click_sequence(selector: str, clicks: str, config: RunnableConfig, image_w: int = 0, image_h: int = 0) -> str:
    """在验证码图片区域上按指定顺序模拟点击（用于点选型验证码）。

    参数:
      selector: 验证码图片元素的 CSS 选择器（用于计算缩放比例）
      clicks: JSON 字符串，格式 '[{"char":"字","x":100,"y":50}, ...]'
              x/y 是相对图片原始像素的坐标，工具会自动换算到视口坐标
      image_w: 验证码图片原始宽度（来自 browser_captcha_recognize 的输出）
      image_h: 验证码图片原始高度

    返回: 每次点击的结果
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
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
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 点击序列失败: {type(e).__name__}: {e}"


@tool
def browser_click_captcha(clicks: str, config: RunnableConfig) -> str:
    """在页面上按坐标序列依次点击（用于图标点选型验证码，无需 CSS 选择器）。

    和 browser_captcha_click_sequence 的区别：
      - 不需要 CSS 选择器，坐标直接相对于当前视口
      - browser_captcha_recognize 识别页面 captcha 后可直接使用返回的坐标

    参数:
      clicks: JSON 字符串，格式 '[{"char":"字","x":100,"y":50}, ...]'
              x/y 是相对于当前浏览器视口左上角的像素坐标

    使用流程：
      1. 调用 browser_captcha_recognize(source="page") 识别验证码
      2. 将返回的坐标传入此工具执行点击
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            data = json.loads(clicks) if isinstance(clicks, str) else clicks
            if not isinstance(data, list) or not data:
                return "❌ clicks 参数必须是 JSON 数组"

            log = [f"🎯 即将在视口上点击 {len(data)} 次（坐标相对于视口左上角）"]
            for i, c in enumerate(data, 1):
                if not isinstance(c, dict):
                    continue
                x = float(c.get("x", 0))
                y = float(c.get("y", 0))
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
            return f"❌ 点击验证码失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 点击验证码失败: {type(e).__name__}: {e}"


@tool
def browser_captcha_refresh(config: RunnableConfig, selector: str = "") -> str:
    """刷新验证码图片（点击验证码区域的刷新/换一批按钮），获取新的验证码重新识别。

    在 browser_captcha_recognize 返回的置信度较低（<0.5）时调用。
    会先尝试在指定容器内找刷新按钮，找不到则在整个页面中搜索。

    参数:
      selector: 验证码容器的 CSS 选择器（如 .captcha-box、#captcha），
                传入后优先在容器内查找刷新按钮

    返回: 刷新结果和新验证码截图。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            refresh_btn = None
            # 如果给了容器选择器，先在容器内找
            if selector:
                container = await page.query_selector(selector)
                if container:
                    # 在容器内查找刷新/换一批/retry 按钮
                    for btn_text in ["刷新", "换一批", "⟳", "↻", "🔄"]:
                        btn = await container.query_selector(
                            f"button, a, span, i, div, img"
                        )
                        # 遍历子元素查找包含刷新文本的
                        all_inner = await container.query_selector_all("*")
                        for el in all_inner:
                            text = (await el.inner_text()).strip()
                            alt = await el.get_attribute("alt") or ""
                            cls = await el.get_attribute("class") or ""
                            if any(k in text or k in alt or k in cls for k in ["刷新", "换一批", "refresh", "retry", "reload"]):
                                refresh_btn = el
                                break
                        if refresh_btn:
                            break

            # 没找到则在全局找
            if not refresh_btn:
                candidates = await page.query_selector_all(
                    'button:has-text("刷新"), button:has-text("换一批"), '
                    'a:has-text("刷新"), a:has-text("换一批"), '
                    '[class*="refresh"], [class*="reload"], [class*="retry"]'
                )
                for el in candidates:
                    if await el.is_visible():
                        refresh_btn = el
                        break

            if not refresh_btn:
                return "❌ 未找到刷新/换一批按钮，请尝试手动刷新页面或点击验证码区域"

            await refresh_btn.click()
            await asyncio.sleep(1.0)  # 等待新验证码加载

            info = await _page_info(page)
            screenshot = await _save_screenshot(page)
            result = f"✅ 验证码已刷新\n\n{info}\n\n"
            if screenshot.get("token"):
                result += f"![截图](/api/screenshot?token={screenshot['token']})\n\n"
            result += "💡 请重新调用 browser_captcha_recognize 识别新验证码"
            return result
        except Exception as e:
            return f"❌ 刷新验证码失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 刷新验证码失败: {type(e).__name__}: {e}"


@tool
def browser_captcha_scan_grid(config: RunnableConfig, grid_rows: int = 9, grid_cols: int = 16) -> str:
    """在页面上叠加网格参考线并截图，辅助视觉模型精确定位图标验证码中的点击坐标。

    用于「请按顺序点击图中指定图标」类型的验证码。流程：
      1. 在页面视口上临时绘制 A1~P9 网格线（默认 16×9）
      2. 截图（网格可见）
      3. 移除网格覆盖层
      4. 返回截图 + 网格坐标说明

    默认 16 列×9 行（列 A~P，行 1~9），每格约 80×80 像素，适合精确定位小图标。
    对于全页 1280×720 的截图，推荐 16×9 或 12×8，不建议低于 6×6。

    视觉 LLM 看到带网格的截图后，可以回答如：
      "皇冠在 B3 格，眼睛在 D1 格，手掌在 A4 格"
    然后你将这些网格引用转换为 {x,y} 坐标，再用 browser_click_captcha 执行点击。

    参数:
      grid_rows: 网格行数，默认 9（行标签 1~9）
      grid_cols: 网格列数，默认 16（列标签 A~P）

    返回: 带网格的截图和坐标映射说明。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        try:
            # 获取视口大小
            viewport = await page.evaluate("({w: window.innerWidth, h: window.innerHeight})")
            vw, vh = viewport["w"], viewport["h"]

            # 生成列标签 A,B,C,...
            col_labels = [chr(65 + i) for i in range(grid_cols)]
            row_labels = [str(i + 1) for i in range(grid_rows)]

            # 计算每个网格单元尺寸
            cell_w = vw / grid_cols
            cell_h = vh / grid_rows

            # 通过 JS 注入网格覆盖层
            overlay_js = """
            (() => {
              const existing = document.getElementById('__captcha_grid_overlay');
              if (existing) existing.remove();

              const overlay = document.createElement('div');
              overlay.id = '__captcha_grid_overlay';
              overlay.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;pointer-events:none;z-index:999999;';
              document.body.appendChild(overlay);

              const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
              svg.setAttribute('width', '100%%');
              svg.setAttribute('height', '100%%');
              svg.style.cssText = 'width:100vw;height:100vh;';
              overlay.appendChild(svg);

              const lines = %s;
              const rows = %d;
              const cols = %d;
              const cw = 100 / cols;
              const rh = 100 / rows;

              // 绘制网格线
              for (let r = 0; r <= rows; r++) {
                const y = (r / rows) * 100;
                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', '0%%');  line.setAttribute('y1', y + '%%');
                line.setAttribute('x2', '100%%'); line.setAttribute('y2', y + '%%');
                line.setAttribute('stroke', 'rgba(255,0,0,0.5)'); line.setAttribute('stroke-width', '1');
                svg.appendChild(line);
              }
              for (let c = 0; c <= cols; c++) {
                const x = (c / cols) * 100;
                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', x + '%%');  line.setAttribute('y1', '0%%');
                line.setAttribute('x2', x + '%%');  line.setAttribute('y2', '100%%');
                line.setAttribute('stroke', 'rgba(255,0,0,0.5)'); line.setAttribute('stroke-width', '1');
                svg.appendChild(line);
              }

              // 绘制标签（列 A B C...，行 1 2 3...）
              const ns = 'http://www.w3.org/2000/svg';
              const colLabels = %s;
              const rowLabels = %s;
              for (let c = 0; c < cols; c++) {
                const txt = document.createElementNS(ns, 'text');
                txt.setAttribute('x', ((c + 0.5) / cols) * 100 + '%%');
                txt.setAttribute('y', '16px');
                txt.setAttribute('text-anchor', 'middle');
                txt.setAttribute('fill', 'rgba(255,0,0,0.8)');
                txt.setAttribute('font-size', '14px');
                txt.setAttribute('font-weight', 'bold');
                txt.textContent = colLabels[c];
                svg.appendChild(txt);
              }
              for (let r = 0; r < rows; r++) {
                const txt = document.createElementNS(ns, 'text');
                txt.setAttribute('x', '12px');
                txt.setAttribute('y', ((r + 0.5) / rows) * 100 + '%%');
                txt.setAttribute('dominant-baseline', 'middle');
                txt.setAttribute('fill', 'rgba(255,0,0,0.8)');
                txt.setAttribute('font-size', '14px');
                txt.setAttribute('font-weight', 'bold');
                txt.textContent = rowLabels[r];
                svg.appendChild(txt);
              }

              return {vw: window.innerWidth, vh: window.innerHeight};
            })()
            """ % (json.dumps([]), grid_rows, grid_cols, json.dumps(col_labels), json.dumps(row_labels))

            result = await page.evaluate(overlay_js)
            await asyncio.sleep(0.3)  # 等待 SVG 渲染

            # 截图（带网格）
            png_data = await page.screenshot(full_page=False, timeout=30000)

            # 移除覆盖层
            await page.evaluate("""
              const el = document.getElementById('__captcha_grid_overlay');
              if (el) el.remove();
            """)

            # 保存截图
            timestamp = int(time.time())
            _ws = _workspace_ctx.get()
            if _ws:
                screenshot_dir = _ws / ".browser_screenshots"
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = screenshot_dir / f"screenshot_grid_{timestamp}.png"
                screenshot_path.write_bytes(png_data)
                token = screenshot_path.stem
            else:
                token = ""

            # 构建坐标映射说明
            lines = [
                f"✅ 已生成 {grid_rows}×{grid_cols} 网格截图",
                f"📐 视口尺寸: {vw}×{vh}",
                f"📏 每格: {cell_w:.0f}×{cell_h:.0f} 像素",
                "",
                "网格坐标（列 A~{}，行 1~{}）：".format(col_labels[-1], grid_rows),
                "",
                "格子坐标计算（像素，相对于视口左上角）：",
            ]
            for r in range(min(grid_rows, 3)):  # 只显示前3行的示例
                for c in range(min(grid_cols, 3)):
                    cell_label = f"{col_labels[c]}{row_labels[r]}"
                    cx = int(c * cell_w + cell_w / 2)
                    cy = int(r * cell_h + cell_h / 2)
                    lines.append(f"  {cell_label} → 中心点 ({cx}, {cy})")
                if grid_cols > 3:
                    lines.append(f"  ...")
            lines.append("")
            lines.append("💡 识别后如 '皇冠在 B3，眼睛在 D1，手掌在 A4'")
            lines.append("→ 调用 browser_click_captcha 传入坐标即可")

            if token:
                lines.append(f"![网格截图](/api/screenshot?token={token})")

            return "\n".join(lines)

        except Exception as e:
            return f"❌ 网格扫描失败: {type(e).__name__}: {e}"

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 网格扫描失败: {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════════════
# 表单决策（Jev 驱动）：枚举表单控件 → Jev 决策"填什么/选哪个" → 执行
# ══════════════════════════════════════════════════════════════

# 枚举可见表单控件与候选提交按钮。
# ponytail: 一段内联 JS 就够了，不引第三方 DOM 库。天花板：不覆盖 canvas/自定义
# 组件（无文本标签）与 shadow DOM 内部；需要时扩选择器即可，无需改架构。
_FORM_ENUM_JS = r"""
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
  const textOf = (el) => {
    // 去掉内嵌的表单控件再取文本，否则 <label>所在行业<select>…</select></label> 会把选项文字也算进标签
    const c = el.cloneNode(true);
    c.querySelectorAll('input,select,textarea,button,svg').forEach(n => n.remove());
    return (c.textContent || '').replace(/\s+/g, ' ').trim();
  };
  const labelOf = (el) => {
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map(textOf).filter(Boolean).join(' ').trim();
      if (t) return t;
    }
    if (el.id) {
      const l = document.querySelector('label[for="' + esc(el.id) + '"]');
      if (l) return textOf(l);
    }
    const up = el.closest('label');
    if (up) { const t = textOf(up); if (t) return t; }
    return (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim();
  };
  const selOf = (el) => {
    if (el.id && document.querySelectorAll('#' + esc(el.id)).length === 1) return '#' + esc(el.id);
    if (el.name) {
      const q = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
      if (document.querySelectorAll(q).length === 1) return q;
      // 单选/复选常无 id，但同 name 下 value 唯一 → input[name=x][value=y] 稳定
      if (el.value && el.value.indexOf('"') < 0) {
        const q2 = q + '[value="' + el.value + '"]';
        if (document.querySelectorAll(q2).length === 1) return q2;
      }
    }
    return null;   // 无稳定选择器：调用方跳过并提示，不瞎猜
  };
  const fields = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase(), type = (el.type || '').toLowerCase();
    if (['hidden','submit','button','reset','image','file'].includes(type)) continue;
    if (!vis(el)) continue;
    const it = {tag, type, name: el.name || '', id: el.id || '',
                label: labelOf(el), required: !!el.required, selector: selOf(el)};
    if (tag === 'select') {
      it.type = 'select';
      it.options = Array.from(el.options).filter(o => !o.disabled)
        .map(o => ({text: (o.text || '').trim(), value: o.value}))
        .filter(o => o.text);
      it.value = el.value;
    } else if (type === 'radio' || type === 'checkbox') {
      it.value = el.value; it.checked = !!el.checked;
    } else {
      it.value = el.value || '';
    }
    fields.push(it);
  }
  const buttons = [];
  for (const b of document.querySelectorAll('button, input[type=submit]')) {
    if (!vis(b)) continue;
    const tag = b.tagName.toLowerCase();
    const type = (b.getAttribute('type') || 'submit').toLowerCase();
    if (tag === 'button' && type !== 'submit') continue;
    buttons.push({selector: selOf(b), text: (b.innerText || b.value || '').trim().slice(0, 40)});
  }
  return {fields, buttons};
}
"""


def _field_key(f: dict) -> str:
    return f.get("name") or f.get("id") or f.get("label") or f.get("selector") or "?"


def _lookup_value(values: dict, f: dict):
    """按 label/name/id/selector 精确或包含匹配取值（文本框内容 Jev 生成不了，须由调用方给）。"""
    keys = [f.get("label"), f.get("name"), f.get("id"), f.get("selector")]
    norm = lambda s: str(s).strip().lower()
    for k in keys:
        if not k:
            continue
        for vk, vv in values.items():
            if norm(vk) == norm(k):
                return vv
    for k in keys:
        if not k or len(str(k)) < 2:
            continue
        kl = norm(k)
        for vk, vv in values.items():
            if norm(vk) in kl:
                return vv
    return None


def _plan_form_actions(form: dict, goal: str, values: dict, submit: bool = False,
                       client=None) -> tuple:
    """纯函数：表单枚举结果 + 目标 → 待执行动作列表（Jev client 可注入，便于单测）。

    返回 (actions, notes, state)：
      - actions: [{"action": fill|check|uncheck|select|click, "selector":..., "value":..., "label":...}]
      - notes:   跳过原因/无需改动等说明（给人看）
      - state:   ["字段=最终值", ...] 各字段的**结果状态**摘要，供提交前核对（含未改动的字段）
    Jev 不可用时（决策返回 None）跳过该字段并在 notes 里说明，不猜。
    """
    from tools.jev_tools import pick_option, option_matches, confirm_submit

    actions, notes, state = [], [], []
    fields = (form or {}).get("fields") or []

    # 分组：radio 按 name 成组（单选），checkbox 同名成组（多选），其余各自独立
    groups, order = {}, []
    for f in fields:
        t = f.get("type")
        if t in ("radio", "checkbox") and f.get("name"):
            gk = f"{t}:{f['name']}"
        else:
            gk = f"one:{f.get('selector') or _field_key(f)}"
        if gk not in groups:
            groups[gk] = []
            order.append(gk)
        groups[gk].append(f)

    for gk in order:
        items = groups[gk]
        head = items[0]
        t = head.get("type")
        label = head.get("label") or head.get("name") or head.get("selector") or "?"
        if t in ("radio", "checkbox"):
            # 单选/复选组的每项 label 是"选项文本"，组名用 name/id（别拿选项文本当组名）
            label = head.get("name") or head.get("id") or label
        sel = head.get("selector")

        if t in ("radio", "checkbox"):
            by_label = {}
            for it in items:
                name = (it.get("label") or it.get("value") or "").strip()
                if name and name not in by_label:
                    by_label[name] = it
            if not by_label:
                notes.append(f"跳过「{label}」：选项无可读文本，无法决策")
                state.append(f"{label}=(无法识别选项)")
                continue
            desc = f"{label}（{'单选' if t == 'radio' else '复选'}）"
            if t == "radio":
                picked = pick_option(goal, desc, list(by_label), client=client)
                if not picked:
                    notes.append(f"跳过「{label}」：Jev 未能决策（单选）")
                    state.append(f"{label}=(未决定)")
                    continue
                it = by_label[picked]
                state.append(f"{label}={picked}")
                if it.get("checked"):
                    notes.append(f"「{label}」已是 {picked}，无需改动")
                elif not it.get("selector"):
                    notes.append(f"跳过「{picked}」：无稳定选择器")
                else:
                    actions.append({"action": "check", "selector": it["selector"],
                                    "label": f"{label}={picked}"})
            else:
                chosen, undecided = [], []
                for name, it in by_label.items():
                    want = option_matches(goal, desc, name, client=client)
                    if want is None:
                        notes.append(f"跳过「{name}」：Jev 未能决策（复选）")
                        undecided.append(name)
                        continue
                    if want:
                        chosen.append(name)
                    if not it.get("selector"):
                        notes.append(f"跳过「{name}」：无稳定选择器")
                        continue
                    if want and not it.get("checked"):
                        actions.append({"action": "check", "selector": it["selector"],
                                        "label": f"{label}={name}"})
                    elif not want and it.get("checked"):
                        actions.append({"action": "uncheck", "selector": it["selector"],
                                        "label": f"{label}={name}（取消勾选）"})
                if undecided:
                    chosen.append("(未决定:" + ",".join(undecided) + ")")
                state.append(f"{label}=[{', '.join(chosen)}]")

        elif t == "select":
            opts = [o["text"] for o in (head.get("options") or [])]
            if not opts or not sel:
                notes.append(f"跳过「{label}」：无选项或无稳定选择器")
                state.append(f"{label}=(不可选)")
                continue
            picked = pick_option(goal, f"{label}（下拉框）", opts, client=client)
            if not picked:
                notes.append(f"跳过「{label}」：Jev 未能决策（下拉框）")
                state.append(f"{label}=(未决定)")
                continue
            val = next((o["value"] for o in head["options"] if o["text"] == picked), picked)
            state.append(f"{label}={picked}")
            if head.get("value") == val:
                notes.append(f"「{label}」已是 {picked}，无需改动")
            else:
                actions.append({"action": "select", "selector": sel, "value": val,
                                "label": f"{label}={picked}"})

        else:  # 文本类：Jev 不能生成内容，只能取调用方提供的值
            val = _lookup_value(values, head)
            if val is None:
                state.append(f"{label}=(空)")
                if head.get("required") or values:
                    notes.append(f"跳过「{label}」：未提供该文本字段的值（Jev 不生成文本）")
                continue
            state.append(f"{label}={str(val)[:40]}")
            if not sel:
                notes.append(f"跳过「{label}」：无稳定选择器")
                continue
            if str(head.get("value") or "") == str(val):
                notes.append(f"「{label}」已是该值，无需改动")
            else:
                actions.append({"action": "fill", "selector": sel, "value": str(val),
                                "label": f"{label}={str(val)[:40]}"})

    if submit:
        # 按钮可能既无 id 也无 name（selector=null）→ 保留 text，执行时用 role/text 定位
        btns = [b for b in ((form or {}).get("buttons") or []) if b.get("selector") or b.get("text")]
        if not btns:
            notes.append("未找到可用的提交按钮，未提交")
        else:
            texts = [b["text"] or f"按钮{i}" for i, b in enumerate(btns)]
            if len(btns) == 1:
                pick = texts[0]
            else:
                pick = pick_option(goal, "提交按钮（单选）", texts, client=client)
            if not pick:
                notes.append("Jev 未能选出提交按钮，未提交")
            else:
                btn = btns[texts.index(pick)]   # ponytail: 文案重复时取第一个，够用
                ok = confirm_submit(goal, "; ".join(state), client=client)
                if ok is False:
                    notes.append("Jev 判定表单状态与目标不符，已跳过提交（请人工确认）")
                else:
                    action = {"action": "click", "selector": btn.get("selector"), "text": pick,
                              "label": f"提交（{pick}）"}
                    actions.append(action)

    return actions, notes, state


@tool
def browser_form_inspect(config: RunnableConfig) -> str:
    """枚举当前页面的表单控件（输入框/单选/复选/下拉/提交按钮）。

    只读，不修改页面。用于在 browser_form_fill 前了解表单结构，或人工核对。
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    async def _run():
        page = await _ensure_browser(thread_id)
        form = await page.evaluate(_FORM_ENUM_JS)
        fields = form.get("fields") or []
        if not fields and not form.get("buttons"):
            return "未在当前页面发现可见表单控件。"
        lines = [f"发现 {len(fields)} 个表单控件："]
        for f in fields:
            kind = f.get("type") or f.get("tag")
            desc = f"- [{kind}] {f.get('label') or f.get('name') or '（无标签）'}"
            if f.get("required"):
                desc += " *必填"
            if kind == "select":
                desc += " 选项: " + " / ".join(o["text"] for o in (f.get("options") or []))
            elif kind in ("radio", "checkbox"):
                desc += f" 值={f.get('value')} 当前={'已选' if f.get('checked') else '未选'}"
            elif f.get("value"):
                desc += f" 当前值={str(f['value'])[:30]}"
            lines.append(desc)
        btns = [b.get("text") for b in (form.get("buttons") or [])]
        if btns:
            lines.append("提交按钮: " + " / ".join(t or "(无文本)" for t in btns))
        return "\n".join(lines)

    try:
        return _run_browser(_run(), thread_id)
    except Exception as e:
        return f"❌ 表单枚举失败: {type(e).__name__}: {e}"


@tool
def browser_form_fill(goal: str, config: RunnableConfig, values: str = "",
                      submit: bool = False, dry_run: bool = False) -> str:
    """用自然语言目标自动填写表单（单选/复选/下拉/文本），可选提交。

    内部流程：枚举表单控件 → Jev 逐字段决策（单选 Choice、复选 Noul、提交 Noul 校验）
    → 执行。Jev 只做"选择/判断"，不生成文本，所以文本字段的值必须由 values 提供。

    参数:
      goal: 自然语言目标（如"选技术类，勾选订阅周报，然后提交"）
      values: 文本字段的取值，JSON 对象字符串，键用 label/name/id 均可（如 '{"邮箱":"a@b.com"}'）
      submit: 是否在填完后点提交按钮（默认 false，先看清楚再提交更稳）。
              只有 Jev 明确判定"与目标不符"才会拦下提交；Jev 不可用时不拦（提交是你显式要求的）
      dry_run: true 时只返回"打算怎么做"，不真正改动页面（推荐先跑一次）
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    values_dict = {}
    if values:
        try:
            parsed = json.loads(values)
            if isinstance(parsed, dict):
                values_dict = parsed
            else:
                return f"❌ values 必须是 JSON 对象字符串，收到: {values[:80]}"
        except Exception as e:
            return f"❌ values 解析失败（需 JSON 对象）: {e}"

    async def _enumerate():
        page = await _ensure_browser(thread_id)
        return await page.evaluate(_FORM_ENUM_JS)

    try:
        form = _run_browser(_enumerate(), thread_id)
    except Exception as e:
        return f"❌ 表单枚举失败: {type(e).__name__}: {e}"

    # Jev 决策放在浏览器锁之外（同步 HTTP 不能占用浏览器事件循环）
    try:
        actions, notes, state = _plan_form_actions(form, goal, values_dict, submit=submit)
    except Exception as e:
        return f"❌ 表单决策失败: {type(e).__name__}: {e}"

    head = [f"🎯 目标: {goal}", f"表单控件 {len(form.get('fields') or [])} 个，计划动作 {len(actions)} 项"]
    if state:
        head.append("填后状态: " + "; ".join(state))
    if not actions:
        head.append("无需改动或无法决策（见下）")
    for a in actions:
        head.append(f"  · {a['action']}: {a.get('label') or a['selector']}")
    if notes:
        head.append("说明:")
        head.extend(f"  - {n}" for n in notes)

    if dry_run:
        head.append("\n（dry_run=true，未改动页面）")
        return "\n".join(head)

    async def _apply():
        page = await _ensure_browser(thread_id)
        done, failed = [], []
        for a in actions:
            try:
                if a["action"] == "fill":
                    await page.fill(a["selector"], a["value"], timeout=10000)
                elif a["action"] == "check":
                    await page.check(a["selector"], timeout=10000)
                elif a["action"] == "uncheck":
                    await page.uncheck(a["selector"], timeout=10000)
                elif a["action"] == "select":
                    await page.select_option(a["selector"], a["value"], timeout=10000)
                elif a["action"] == "click":
                    if a.get("selector"):
                        await page.click(a["selector"], timeout=10000)
                    else:
                        # 按钮无 id/name → 用可访问名定位（Playwright role 引擎）
                        await page.get_by_role("button", name=a.get("text") or "").first.click(timeout=10000)
                done.append(a)
            except Exception as e:
                failed.append(f"{a.get('label') or a.get('selector')}: {type(e).__name__}: {e}")
        await asyncio.sleep(0.5)  # 等页面响应
        info = await _page_info(page)
        return done, failed, info

    try:
        done, failed, info = _run_browser(_apply(), thread_id)
    except Exception as e:
        return "\n".join(head) + f"\n\n❌ 表单填写失败: {type(e).__name__}: {e}"

    out = head + [f"\n✅ 已执行 {len(done)} 项"]
    if failed:
        out.append(f"❌ 失败 {len(failed)} 项:")
        out.extend(f"  - {f}" for f in failed)
    out.append(f"\n{info}")
    return "\n".join(out)


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
    browser_scroll_to,
    browser_wait_for_element,
    browser_drag,
    browser_slide,
    browser_captcha_recognize,
    browser_captcha_click_sequence,
    browser_click_captcha,
    browser_captcha_refresh,
    browser_captcha_scan_grid,
    # 表单决策（Jev）
    browser_form_inspect,
    browser_form_fill,
]
