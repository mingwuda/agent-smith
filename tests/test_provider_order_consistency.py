"""回归：底部发送框与设置页的 Provider 顺序必须一致。

复现 bug：两个数据源不同——
  - 设置页读 GET /settings      → AgentConfig.to_api_dict()（含 provider_order）
  - 发送框读 GET /users/me/settings → get_user_effective_config()（曾缺失该字段）

发送框拿不到 provider_order 时，indexOf 全为 -1，排序退化为 providers 字典序，
于是两处顺序不一致（用户可见）。

运行：python -m pytest tests/test_provider_order_consistency.py -q
"""
from functools import cmp_to_key

# 先导入 main 以触发 agent_core/main.py 顶部的 sys.path 注入（沿用既有测试约定）
from agent_core.main import app  # noqa: F401
from config import AgentConfig
from user_config import get_user_effective_config, _write_user_config


def _seed_global(order):
    cfg = AgentConfig()
    cfg.providers = {
        "p_a": {"name": "A", "model": "m-a", "api_key": "sk-a", "base_url": "http://a"},
        "p_b": {"name": "B", "model": "m-b", "api_key": "sk-b", "base_url": "http://b"},
        "p_c": {"name": "C", "model": "m-c", "api_key": "sk-c", "base_url": "http://c"},
    }
    cfg.active_provider = "p_b"
    cfg.set_provider_order(order)
    cfg.save()
    return cfg


def _frontend_sort(ids, order):
    """复刻 settings.js 中 populateProviderSelect / refreshProviderSelects 的排序逻辑。"""
    def cmp(a, b):
        ia = order.index(a) if a in order else -1
        ib = order.index(b) if b in order else -1
        if ia == -1 and ib == -1:
            return 0
        if ia == -1:
            return 1
        if ib == -1:
            return -1
        return ia - ib
    return sorted(ids, key=cmp_to_key(cmp))


def test_user_settings_exposes_provider_order():
    """核心回归：/users/me/settings 必须返回 provider_order（修复前该键缺失）。"""
    _seed_global(["p_c", "p_a", "p_b"])
    view = get_user_effective_config("order_uid")
    assert "provider_order" in view
    assert view["provider_order"] == ["p_c", "p_a", "p_b"]


def test_composer_order_matches_settings_page_order():
    """两处数据源经前端同一排序逻辑后，结果必须完全一致。"""
    order = ["p_c", "p_a", "p_b"]
    _seed_global(order)

    admin_view = AgentConfig.load().to_api_dict()        # 设置页
    user_view = get_user_effective_config("order_uid")   # 发送框

    assert _frontend_sort(admin_view["providers"], admin_view["provider_order"]) == order
    assert _frontend_sort(user_view["providers"], user_view["provider_order"]) == order


def test_order_drops_ids_missing_from_user_providers():
    """用户级 providers 覆盖不含某个 id 时，不得把脏 id 透传给前端。"""
    _seed_global(["p_c", "p_a", "p_b"])
    _write_user_config("order_uid2", {"providers": {
        "p_a": {"name": "A", "model": "m-a", "api_key": "sk-a"},
        "p_b": {"name": "B", "model": "m-b", "api_key": "sk-b"},
    }})
    view = get_user_effective_config("order_uid2")
    assert view["provider_order"] == ["p_a", "p_b"]
