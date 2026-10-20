"""浏览器实时视图 / 接管路由

方案 A：`/api/browser/live` 返回 agent 当前浏览器页面快照（实时视图）。
方案 B：`/api/browser/takeover-url` 返回用户可用真实浏览器接管的入口
        （当前页 URL 直接打开；Obscura CDP 场景可经 DevTools 接管）。
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tools import browser_tools
from api.deps import _get_current_user

router = APIRouter(tags=["browser"])


class LiveViewResponse(BaseModel):
    available: bool
    backend: str = ""
    url: str = ""
    title: str = ""
    screenshot: str = ""   # data-url（base64 PNG）
    dom: str = ""
    note: str = ""


class TakeoverResponse(BaseModel):
    available: bool
    backend: str = ""
    url: str = ""          # 用户可在真实浏览器直接打开并继续操作
    cdp_url: str = ""      # 若后端是远程 CDP（Obscura）且经鉴权暴露，给出 DevTools 接管入口
    note: str = ""


@router.get("/api/browser/live", response_model=LiveViewResponse)
def browser_live(thread_id: str = "default", request: Request = None):
    """获取某会话当前浏览器页面的实时快照（方案 A：看得见）。

    轮询调用即可获得 agent 正在浏览器里操作的画面。
    """
    _get_current_user(request)  # 鉴权（无有效用户会 401）
    return browser_tools.get_live_view(thread_id or "default")


@router.get("/api/browser/takeover-url", response_model=TakeoverResponse)
def browser_takeover_url(thread_id: str = "default", request: Request = None):
    """返回用户可用真实浏览器接管当前页的入口（方案 B：真浏览器接着弄）。

    - backend == "obscura" 且配置了 OBSCURA_CDP_URL → 用户可用 Chrome DevTools
      (chrome://inspect) 连 cdp_url 接管同一个实例；url 是当前页（也可直接打开）。
    - 其余（本地 Chromium persistent/普通）→ 给出 url，用户直接打开继续操作，
      登录态/表单状态即 agent 留下的现场。
    """
    _get_current_user(request)
    view = browser_tools.get_live_view(thread_id or "default")

    cdp_url = ""
    if view.get("backend") == "obscura":
        cdp_url = browser_tools._obscura_cdp_url() or ""

    return TakeoverResponse(
        available=bool(view.get("available")),
        backend=view.get("backend") or "",
        url=view.get("url") or "",
        cdp_url=cdp_url,
        note=(
            f"后端={view.get('backend') or '未初始化'}。直接打开 url 可接管 agent 现场；"
            if view.get("available")
            else "当前无浏览器页面可接管。"
        ),
    )