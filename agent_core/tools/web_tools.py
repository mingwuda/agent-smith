"""网页搜索与抓取工具"""
from datetime import date, timedelta
import time
import re
import socket
import subprocess
import urllib.parse
from typing import Optional
import requests
import urllib3.util.connection

from langchain_core.tools import tool
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}
REQUEST_TIMEOUT = (3, 10)
RETRY_TOTAL = 1
_TAVILY_SEARCH_ENABLED = False
_TAVILY_API_KEY = ""
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_search_attempt_start = 0.0

_RELATIVE_DATE_RE = re.compile(
    r"(今天|今日|昨天|昨日|今年|本年|最新|近期|最近|当前|现在|today|yesterday|latest|recent|current|this year|now)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _current_date_context() -> str:
    today = date.today()
    return f"当前日期: {today.isoformat()}，当前年份: {today.year}"


def _looks_time_sensitive(query: str) -> bool:
    return bool(_RELATIVE_DATE_RE.search(query or ""))


def _normalize_search_query(query: str, recency_days: int = 0) -> tuple[str, str]:
    raw = (query or "").strip()
    today = date.today()
    recency_days = max(0, min(int(recency_days or 0), 365))
    normalized = raw
    notes: list[str] = []

    years = {int(item) for item in _YEAR_RE.findall(raw)}
    if _looks_time_sensitive(raw):
        if today.year not in years:
            normalized = f"{normalized} {today.year}"
            notes.append(f"检测到相对时间词，已补充当前年份 {today.year}")
        old_years = sorted(year for year in years if year != today.year)
        if old_years:
            notes.append(f"查询中包含非当前年份 {', '.join(map(str, old_years))}；如用户要最新信息，请优先以 {today.year} 为准")

    if recency_days > 0:
        since = today - timedelta(days=recency_days)
        normalized = f"{normalized} after:{since.isoformat()}"
        notes.append(f"已追加近 {recency_days} 天约束 after:{since.isoformat()}")

    return normalized, "；".join(notes)


def configure_search(
    tavily_search_enabled: bool = False,
    tavily_api_key: str = "",
    tavily_search_url: str = "https://api.tavily.com/search",
):
    global _TAVILY_SEARCH_ENABLED, _TAVILY_API_KEY, _TAVILY_SEARCH_URL
    _TAVILY_SEARCH_ENABLED = bool(tavily_search_enabled)
    _TAVILY_API_KEY = str(tavily_api_key or "").strip()
    _TAVILY_SEARCH_URL = str(tavily_search_url or "https://api.tavily.com/search").strip()


class _SimpleResponse:
    def __init__(self, text: str, status_code: int, url: str):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = {}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"{self.status_code} Error: {self.url}")
            error.response = self
            raise error


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        status=RETRY_TOTAL,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


_SESSION = _build_session()


def _prepare_url(url: str, params: Optional[dict] = None) -> str:
    if not params:
        return url
    request = requests.Request("GET", url, params=params).prepare()
    return request.url or url


def _curl_get(url: str, params: Optional[dict] = None) -> _SimpleResponse:
    full_url = _prepare_url(url, params)
    cmd = [
        "curl",
        "-sSL",
        "--compressed",
        "--max-time",
        "35",
        "-A",
        HEADERS["User-Agent"],
        "-H",
        f"Accept: {HEADERS['Accept']}",
        "-H",
        f"Accept-Language: {HEADERS['Accept-Language']}",
        "-w",
        "\n__DESKTOP_AGENT_HTTP_STATUS__:%{http_code}",
        full_url,
    ]
    completed = subprocess.run(cmd, text=True, capture_output=True, timeout=40, check=False)
    if completed.returncode != 0:
        raise requests.exceptions.ConnectionError(completed.stderr.strip() or "curl 请求失败")
    marker = "\n__DESKTOP_AGENT_HTTP_STATUS__:"
    body, _, status_raw = completed.stdout.rpartition(marker)
    try:
        status_code = int(status_raw.strip() or "0")
    except ValueError:
        status_code = 0
    return _SimpleResponse(body, status_code, full_url)


def _http_get(url: str, params: Optional[dict] = None):
    return _SESSION.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT,
        headers=HEADERS,
        allow_redirects=True,
    )

# 预解析 DNS（解决 urllib3 在部分网络下解析 cn.bing.com 慢的问题）
_BING_IPS: dict[str, str] = {}
for _host in ("www.bing.com", "cn.bing.com"):
    try:
        _BING_IPS[_host] = socket.gethostbyname(_host)
    except OSError:
        pass

if _BING_IPS:
    _orig_create_conn = urllib3.util.connection.create_connection

    def _patched_create_conn(address, *args, **kwargs):
        host, port = address
        if host in _BING_IPS:
            address = (_BING_IPS[host], port)
        return _orig_create_conn(address, *args, **kwargs)

    urllib3.util.connection.create_connection = _patched_create_conn


def _search_bing(query: str, max_results: int) -> list[dict]:
    """通过 Bing 搜索，简单的 HTML 提取"""
    from bs4 import BeautifulSoup

    encoded = urllib.parse.quote(query)
    url = f"https://www.bing.com/search?q={encoded}&count={max_results}&setlang=zh-Hans"

    resp = _http_get(url)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for li in soup.select("li.b_algo"):
        title_el = li.select_one("h2 a")
        snippet_el = li.select_one(".b_caption p")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        if title and href:
            results.append({"title": title, "href": href, "body": snippet})
        if len(results) >= max_results:
            break

    return results


def _search_sogou(query: str, max_results: int) -> list[dict]:
    """通过搜狗搜索，作为国内可用的备用源。"""
    from bs4 import BeautifulSoup

    encoded = urllib.parse.quote(query)
    url = f"https://www.sogou.com/web?query={encoded}&num={max_results}"

    resp = _http_get(url)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    
    for item in soup.select(".vrwrap, .rb"):
        title_el = item.select_one(".vr-title a, .vr-title, h3 a, h3")
        snippet_el = item.select_one(".star-wiki, .str-text, .str_info_div, .space-txt")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        if title and href:
            results.append({"title": title, "href": href, "body": snippet})
        if len(results) >= max_results:
            break

    return results


def _search_duckduckgo(query: str, max_results: int) -> list[dict]:
    """通过 DuckDuckGo HTML 结果页搜索，作为 Bing 失败时的备用源。"""
    from bs4 import BeautifulSoup

    url = "https://html.duckduckgo.com/html/"
    resp = _http_get(url, params={"q": query})
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for item in soup.select(".result"):
        title_el = item.select_one(".result__title a")
        snippet_el = item.select_one(".result__snippet")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        href = title_el.get("href", "")
        parsed = urllib.parse.urlparse(href)
        if parsed.path.startswith("/l/") or "uddg=" in parsed.query:
            query_params = urllib.parse.parse_qs(parsed.query)
            href = query_params.get("uddg", [href])[0]
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if title and href:
            results.append({"title": title, "href": href, "body": snippet})
        if len(results) >= max_results:
            break
    return results


def _search_tavily(query: str, max_results: int, recency_days: int = 0) -> list[dict]:
    if not _TAVILY_API_KEY:
        raise ValueError("Tavily API Key 未配置")

    payload = {
        "api_key": _TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    if recency_days > 0:
        payload["days"] = max(1, min(int(recency_days), 365))

    resp = _SESSION.post(
        _TAVILY_SEARCH_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT,
        headers={"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]},
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("results") or []:
        title = str(item.get("title") or "").strip()
        href = str(item.get("url") or "").strip()
        body = str(item.get("content") or item.get("snippet") or "").strip()
        if title and href:
            results.append({"title": title, "href": href, "body": body})
        if len(results) >= max_results:
            break
    return results


def _format_results(query: str, results: list[dict], source: str, *, original_query: str = "", note: str = "") -> str:
    lines = [f"🔍 搜索「{query}」找到 {len(results)} 条结果（来源: {source}）："]
    lines.append(_current_date_context())
    if original_query and original_query != query:
        lines.append(f"原始查询: {original_query}")
    if note:
        lines.append(f"搜索提示: {note}")
    lines.append("")
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        url = r.get("href", "").strip()
        snippet = r.get("body", "").strip()
        lines.append(f"{i}. **{title}**")
        lines.append(f"   链接: {url}")
        if snippet:
            snippet = (snippet[:240] + "...") if len(snippet) > 240 else snippet
            lines.append(f"   摘要: {snippet}")
        lines.append("")
    lines.append("搜索建议: 优先从上述结果选择 1-3 个权威/高相关链接调用 web_fetch，然后综合回答；不要仅为微调关键词反复 web_search。")
    return "\n".join(lines)


@tool
def web_search(query: str, max_results: int = 5, recency_days: int = 0) -> str:
    """搜索互联网，返回相关网页标题、链接和摘要。优先使用 Bing，失败时自动切换 DuckDuckGo，无需 API Key。

    参数:
        query: 搜索关键词。查询“今天/最新/近期/current/latest”等实时信息时必须包含正确年份；工具会按当前日期辅助补全年份。
        max_results: 返回结果数量（默认 5，建议 5-8）。先用一次高质量搜索，再 fetch 1-3 个最相关链接，避免多轮改写搜索。
        recency_days: 可选，限制近 N 天结果（1-365）。例如今天/最新新闻用 7，近期事件用 30；不需要近期约束时传 0。
    """
    global _search_attempt_start
    _search_attempt_start = time.time()
    max_results = max(1, min(max_results, 10))
    normalized_query, note = _normalize_search_query(query, recency_days)
    errors = []

    search_backends = []
    if _TAVILY_SEARCH_ENABLED:
        search_backends.append(("Tavily", lambda q, n: _search_tavily(q, n, recency_days)))
    search_backends.extend((("Bing", _search_bing), ("搜狗", _search_sogou), ("DuckDuckGo", _search_duckduckgo)))

    for source, search_fn in search_backends:
        if time.time() - _search_attempt_start > 18:
            errors.append(f"{source}: 总搜索超时已到（跳过）")
            break
        try:
            results = search_fn(normalized_query, max_results)
            if results:
                return _format_results(normalized_query, results, source, original_query=query, note=note)
            errors.append(f"{source}: 未找到结果")
        except requests.exceptions.Timeout:
            errors.append(f"{source}: 搜索超时")
        except requests.exceptions.ConnectionError as e:
            errors.append(f"{source}: 网络连接失败（{str(e)[:160]}）")
        except requests.exceptions.HTTPError as e:
            errors.append(f"{source}: HTTP {e.response.status_code}")
        except Exception as e:
            errors.append(f"{source}: {type(e).__name__}: {e}")

    return (
        f"❌ 未能获取「{normalized_query}」的搜索结果。已尝试 Bing、搜狗和 DuckDuckGo。\n"
        + _current_date_context()
        + (f"\n搜索提示: {note}" if note else "")
        + "\n"
        + "\n".join(f"- {item}" for item in errors)
    )


def _fetch_text(url: str, max_chars: int) -> str:
    """获取网页纯文本"""
    from bs4 import BeautifulSoup

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    resp = _http_get(url)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"

    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "noscript", "iframe", "svg", "form", "button", "input"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = "\n".join(lines)

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n...（内容较长，截断至 {max_chars} 字符）"

    return text or "（页面无可见文本内容）"


@tool
def web_fetch(url: str, max_chars: int = 5000) -> str:
    """获取指定网页的正文内容。适合查看文章、文档、API 文档等。

    参数:
        url: 网页链接（如 https://example.com/article）
        max_chars: 最大返回字符数（默认 5000）
    """
    try:
        return _fetch_text(url, max_chars)
    except requests.exceptions.Timeout:
        return f"❌ 请求超时: {url}"
    except requests.exceptions.HTTPError as e:
        return f"❌ HTTP {e.response.status_code}: {url}"
    except requests.exceptions.ConnectionError as e:
        return f"❌ 无法连接: {url}\n原因: {str(e)[:240]}"
    except Exception as e:
        return f"❌ 获取失败: {e}"


TOOLS = [web_search, web_fetch]
