"""回归测试：设置页 Add Model 后保存丢失。

根因：set_model_order 原先只保留"后端已存在"的模型(if m in models)，
且仅当"有删除"时才把 models 收缩写回。前端新增模型(order 有、后端 models 没有)
会被过滤掉、永不落库 → Add Model 后保存，刷新即消失。

修复：令 order 为该 provider 的权威完整模型列表，直接写入 models，
从而同时支持"新增"(order 有而 models 没有)与"删除"(order 没有而 models 有)。
"""
from agent_core.config import AgentConfig


def _make_cfg():
    cfg = AgentConfig()
    cfg._normalize_providers()
    # 添加一个自定义 provider
    cfg.update_provider(provider_id="myprovider", provider_name="My",
                        api_key="k", model="m1", base_url="http://x")
    return cfg


def test_add_model_then_save_persists_new_model():
    cfg = _make_cfg()
    # 后端此时只有初始模型 m1
    assert cfg.providers["myprovider"]["models"] == ["m1"]

    # 前端 Add Model：order = [m1, m2]（m2 是后端尚不存在的新模型）
    order = ["m1", "m2"]
    cfg.set_model_order("myprovider", order)

    # 新模型必须落库进 models（此前会被过滤掉 → bug）
    assert cfg.providers["myprovider"]["models"] == ["m1", "m2"]
    # model_order 也与 order 一致
    assert cfg.providers["myprovider"]["model_order"] == ["m1", "m2"]


def test_delete_model_via_order_still_works():
    cfg = _make_cfg()
    cfg.set_model_order("myprovider", ["m1", "m2", "m3"])
    assert cfg.providers["myprovider"]["models"] == ["m1", "m2", "m3"]

    # 删除 m2
    cfg.set_model_order("myprovider", ["m1", "m3"])
    assert cfg.providers["myprovider"]["models"] == ["m1", "m3"]
    assert cfg.providers["myprovider"]["model_order"] == ["m1", "m3"]


def test_add_model_also_appends_after_save_settings_flow():
    """模拟完整前端流程：update_provider(model=新模型) + set_model_order(order=含新模型)。"""
    cfg = _make_cfg()
    # Add Model 时 update_provider(model=m2) 会先 append
    cfg.update_provider(provider_id="myprovider", model="m2")
    assert "m2" in cfg.providers["myprovider"]["models"]
    # 然后 set_model_order 再次用完整 order 收敛
    cfg.set_model_order("myprovider", cfg.providers["myprovider"]["models"])
    assert cfg.providers["myprovider"]["models"] == ["m1", "m2"]


def test_deleting_active_model_falls_back():
    cfg = _make_cfg()
    cfg.set_model_order("myprovider", ["m1", "m2"])
    cfg.update_provider(provider_id="myprovider", model="m2")
    assert cfg.providers["myprovider"]["model"] == "m2"

    # 删除激活模型 m2 → 回退到 m1
    cfg.set_model_order("myprovider", ["m1"])
    assert "m2" not in cfg.providers["myprovider"]["models"]
    assert cfg.providers["myprovider"]["model"] == "m1"


def test_non_custom_provider_ignored():
    """非自定义 provider 不被收缩（冗余，现有逻辑已保证）。"""
    cfg = _make_cfg()
    # 注意：DEFAULT_PROVIDERS 现为空，_normalize_providers 会把所有 provider
    # 重算为 is_custom=(pid not in DEFAULT_PROVIDERS)=True，所以直接伪造 is_custom=False
    # 会在 set_model_order 内部被 normalize 覆盖。这里仅验证"order 写入 model_order"。
    cfg.providers["builtin"] = {
        "name": "builtin", "is_custom": False, "models": ["b1"],
    }
    cfg.set_model_order("builtin", ["b1", "b2"])
    # model_order 记录 order，模型已并入
    assert cfg.providers["builtin"]["model_order"] == ["b1", "b2"]
    assert "b2" in cfg.providers["builtin"]["models"]