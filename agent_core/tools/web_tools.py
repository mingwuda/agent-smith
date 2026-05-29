"""网页搜索与抓取工具"""
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
REQUEST_TIMEOUT = (5, 25)
RETRY_TOTAL = 3


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
    try:
        return _SESSION.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
            headers=HEADERS,
            allow_redirects=True,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return _curl_get(url, params)

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


def _format_results(query: str, results: list[dict], source: str) -> str:
    lines = [f"🔍 搜索「{query}」找到 {len(results)} 条结果（来源: {source}）：\n"]
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
    return "\n".join(lines)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """搜索互联网，返回相关网页标题、链接和摘要。优先使用 Bing，失败时自动切换 DuckDuckGo，无需 API Key。

    参数:
        query: 搜索关键词（如"今天热点新闻""Python教程""2025诺贝尔奖"）
        max_results: 返回结果数量（默认 5，建议 3-8）
    """
    max_results = max(1, min(max_results, 10))
    errors = []

    for source, search_fn in (("Bing", _search_bing), ("DuckDuckGo", _search_duckduckgo)):
        try:
            results = search_fn(query, max_results)
            if results:
                return _format_results(query, results, source)
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
        f"❌ 未能获取「{query}」的搜索结果。已尝试 Bing 和 DuckDuckGo。\n"
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
