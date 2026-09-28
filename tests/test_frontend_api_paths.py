"""前端 API 路径与后端路由前缀的一致性守卫。

背景（线上故障）：前端把 cancel / inject / ask resolve 三个端点写成了 `/agent/sessions/...`，
而后端路由挂在根路径（**没有** `/agent` 前缀）→ 请求命中 404 Not Found →
「征询提交失败」、以及「停止」按钮静默失效。

journal 实证：
    POST /agent/sessions/72607289/ask/ask_1790594102265_d574a2/resolve  → 404 Not Found
    POST /agent/sessions/72607289/cancel                                → 404 Not Found
对照：GET /sessions/72607289/stream/active → 200 OK（无前缀才对）

校验方式：取前端所有 `fetch(` 字面量路径的**首段**，断言它必须能在「后端路由首段集合」里找到。
足够粗（不受 path 参数 / 查询串 / 字符串拼接影响），又能准确抓住「前缀写错」这一类 bug。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_ROUTE_DECORATOR = re.compile(r'@(?:router|app)\.(?:get|post|put|delete|patch)\("([^"]*)"')
_ROUTER_PREFIX = re.compile(r'APIRouter\([^)]*prefix\s*=\s*"([^"]*)"')
_FE_FETCH = re.compile(r"""fetch\(\s*[`'"]/([A-Za-z0-9_-]*)""")


def _backend_roots() -> set[str]:
    """后端所有路由的「首段」集合（含 APIRouter 自身 prefix）。"""
    files = list((ROOT / "agent_core" / "api" / "routes").glob("*.py"))
    files += list((ROOT / "agent_core" / "api").glob("*.py"))
    files.append(ROOT / "agent_core" / "main.py")
    roots = set()
    for f in files:
        src = f.read_text(encoding="utf-8")
        m = _ROUTER_PREFIX.search(src)
        prefix = m.group(1) if m else ""
        for dm in _ROUTE_DECORATOR.finditer(src):
            path = (prefix + dm.group(1)).strip("/")
            roots.add(path.split("/")[0] if path else "")
    return roots


def _frontend_roots() -> set[str]:
    """前端所有 fetch 字面量路径的「首段」集合。"""
    roots = set()
    for f in (ROOT / "desktop" / "js").rglob("*.js"):
        for m in _FE_FETCH.finditer(f.read_text(encoding="utf-8")):
            roots.add(m.group(1))
    return roots


def test_frontend_fetch_roots_exist_in_backend_routes():
    missing = sorted(_frontend_roots() - _backend_roots())
    assert not missing, (
        "前端 fetch 的首段在后端路由里找不到（路径前缀写错会 404）：" + repr(missing)
    )


def test_no_agent_prefixed_calls():
    """显式回归守卫：后端从无 /agent 前缀，前端不得再写 /agent/... 。"""
    offenders = []
    for f in (ROOT / "desktop").rglob("*"):
        if f.suffix not in (".js", ".html"):
            continue
        if re.search(r"""fetch\(\s*[`'"]/agent/""", f.read_text(encoding="utf-8")):
            offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, "/agent 前缀在后端不存在，必然 404：" + repr(offenders)


def test_guard_would_catch_the_agent_prefix_bug():
    """自检：确认 'agent' 确实不在后端首段集合里（否则上面的守卫是失效的）。"""
    assert "agent" not in _backend_roots()
    assert "sessions" in _backend_roots()
