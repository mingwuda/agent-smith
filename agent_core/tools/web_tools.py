"""网页搜索与抓取工具"""
import re
import urllib.parse
from langchain_core.tools import tool

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _search_bing(query: str, max_results: int) -> list[dict]:
    """通过 Bing 搜索（国内可访问，无需 API Key）"""
    import requests
    from bs4 import BeautifulSoup

    encoded = urllib.parse.quote(query)
    url = f"https://www.bing.com/search?q={encoded}&count={max_results}&setlang=zh-Hans"
    
    resp = requests.get(url, timeout=15, headers=HEADERS)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    
    soup = BeautifulSoup(resp.text, "lxml")
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
    """搜索互联网，返回相关网页结果列表。使用 Bing 搜索引擎，国内可直接访问，无需 API Key。
    
    参数:
        query: 搜索关键词
        max_results: 返回结果数量（默认 5，最大 10）
    """
    max_results = min(max(max_results, 1), 10)
    
    try:
        results = _search_bing(query, max_results)
        
        if not results:
            return f"未找到与「{query}」相关的搜索结果"
        
        lines = [f"🔍 搜索「{query}」共找到 {len(results)} 条结果：\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "").strip()
            url = r.get("href", "").strip()
            snippet = r.get("body", "").strip()
            lines.append(f"{i}. **{title}**")
            lines.append(f"   链接: {url}")
            if snippet:
                snippet = snippet[:300] + "..." if len(snippet) > 300 else snippet
                lines.append(f"   摘要: {snippet}")
            lines.append("")
        
        return "\n".join(lines)
    
    except Exception as e:
        return f"❌ 搜索失败: {type(e).__name__}: {e}。建议换个关键词试试。"


@tool
def web_fetch(url: str, max_chars: int = 5000) -> str:
    """获取网页内容并提取可读文本。适合查看文章、文档等内容。
    
    参数:
        url: 要获取的网页完整 URL（需包含 http:// 或 https://）
        max_chars: 最大返回字符数（默认 5000）
    """
    import requests
    from bs4 import BeautifulSoup
    
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    
    try:
        resp = requests.get(url, timeout=15, headers=HEADERS, allow_redirects=True)
        resp.raise_for_status()
        
        resp.encoding = resp.apparent_encoding or "utf-8"
        
        soup = BeautifulSoup(resp.text, "lxml")
        
        # 移除无用标签
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", 
                         "noscript", "iframe", "svg", "form", "button", "input"]):
            tag.decompose()
        
        # 提取纯文本
        text = soup.get_text(separator="\n", strip=True)
        
        # 清理空行
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)
        
        # 截断
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n...（内容过长，已截断至 {max_chars} 字符）"
        
        return text if text else "（页面无可见文本内容）"
    
    except requests.exceptions.Timeout:
        return f"❌ 请求超时: {url}"
    except requests.exceptions.HTTPError as e:
        return f"❌ HTTP 错误: {e}"
    except requests.exceptions.ConnectionError:
        return f"❌ 无法连接: {url}"
    except Exception as e:
        return f"❌ 获取失败: {type(e).__name__}: {e}"


TOOLS = [web_search, web_fetch]
