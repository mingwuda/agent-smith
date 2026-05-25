"""网页搜索与抓取工具"""
import re
import socket
import urllib.parse
import urllib3.util.connection

from langchain_core.tools import tool

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

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
    import requests
    from bs4 import BeautifulSoup

    encoded = urllib.parse.quote(query)
    url = f"https://www.bing.com/search?q={encoded}&count={max_results}&setlang=zh-Hans"

    resp = requests.get(url, timeout=12, headers=HEADERS, allow_redirects=True)
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


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """搜索互联网，返回相关网页标题、链接和摘要。使用 Bing 搜索引擎，无需 API Key，国内可直接访问。

    参数:
        query: 搜索关键词（如"今天热点新闻""Python教程""2025诺贝尔奖"）
        max_results: 返回结果数量（默认 5，建议 3-8）
    """
    max_results = max(1, min(max_results, 10))

    try:
        import requests
        results = _search_bing(query, max_results)

        if not results:
            return f"未找到与「{query}」相关的搜索结果，建议换个关键词试试"

        lines = [f"🔍 搜索「{query}」找到 {len(results)} 条结果：\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "").strip()
            url = r.get("href", "").strip()
            snippet = r.get("body", "").strip()
            lines.append(f"{i}. **{title}**")
            lines.append(f"   链接: {url}")
            if snippet:
                snippet = (snippet[:200] + "...") if len(snippet) > 200 else snippet
                lines.append(f"   摘要: {snippet}")
            lines.append("")

        return "\n".join(lines)

    except requests.exceptions.Timeout:
        return "❌ 搜索超时，网络可能不稳定，请稍后重试"
    except requests.exceptions.ConnectionError:
        return "❌ 网络连接失败，请检查网络"
    except Exception as e:
        return f"❌ 搜索失败: {type(e).__name__}: {e}"


def _fetch_text(url: str, max_chars: int) -> str:
    """获取网页纯文本"""
    import requests
    from bs4 import BeautifulSoup

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    resp = requests.get(url, timeout=12, headers=HEADERS, allow_redirects=True)
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
    except requests.exceptions.ConnectionError:
        return f"❌ 无法连接: {url}"
    except Exception as e:
        return f"❌ 获取失败: {e}"


TOOLS = [web_search, web_fetch]
