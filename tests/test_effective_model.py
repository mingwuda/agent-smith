"""测试回放消息记录的模型名解析逻辑（_request_effective_model）。

验证：与 LLM 构建同一套规则——model_override > provider.model > 全局 model，
且异常时回退到当前全局模型名，而不是返回空串/抛错。
"""
import pytest

# 触发 agent_core 的 sys.path 注入，使 `from services.agent_service import` 顶层导入可用
from agent_core.main import app  # noqa: F401
from agent_core.services.agent_service import _request_effective_model  # noqa: F401


class _FakeAgent:
    def __init__(self, providers, active_provider, default_model):
        self.config = type("C", (), {"providers": providers, "active_provider": active_provider, "model": default_model})()

    def _resolve_provider_config(self, model_override="", provider_override=""):
        pid = provider_override or self.config.active_provider
        prov = self.config.providers.get(pid, {})
        model = model_override or prov.get("model") or self.config.model
        return pid, model, "k", "", False


@pytest.fixture
def fake_agent(monkeypatch):
    def _install(agent):
        import app_state
        monkeypatch.setattr(app_state, "get_agent", lambda: agent)
        import agent_core.services.agent_service as mod
        monkeypatch.setattr(mod, "_current_model_name", lambda: "fallback-model")
    return _install


def test_provider_model_used_when_no_override(fake_agent):
    fake_agent(_FakeAgent({"pA": {"model": "model-A"}, "pB": {"model": "model-B"}}, "pA", "default-X"))
    assert _request_effective_model() == "model-A"


def test_model_override_wins(fake_agent):
    fake_agent(_FakeAgent({"pA": {"model": "model-A"}}, "pA", "default-X"))
    assert _request_effective_model("my-override-model", "") == "my-override-model"


def test_provider_override_wins_over_active(fake_agent):
    fake_agent(_FakeAgent({"pA": {"model": "model-A"}, "pB": {"model": "model-B"}}, "pA", "default-X"))
    assert _request_effective_model("", "pB") == "model-B"


def test_provider_missing_model_falls_to_default(fake_agent):
    fake_agent(_FakeAgent({"pA": {}}, "pA", "default-X"))
    assert _request_effective_model() == "default-X"


def test_exception_falls_back_to_current(monkeypatch):
    import app_state
    monkeypatch.setattr(app_state, "get_agent", lambda: object())  # 无 _resolve_provider_config
    import agent_core.services.agent_service as mod
    monkeypatch.setattr(mod, "_current_model_name", lambda: "fallback-model")
    assert _request_effective_model("x", "y") == "fallback-model"