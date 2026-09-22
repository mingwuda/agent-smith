"""jev_tools 通用决策库测试。

覆盖：
  - risk_gate：Noul 风险门控的阈值/降级行为（注入 Fake 与"降级"client）
  - JevClient.noul/choice/score：失败返回 None（不抛异常）
  - shell_tools 集成：Jev 可在禁用时放行、可用时追加确认闸
"""
import importlib
import os
import subprocess
import sys
from unittest import mock

import pytest


# 让 agent_core.tools 可被导入（依赖顶层包名 agent_core）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 生产代码里工具间用绝对包名互引（from tools.jev_tools import ...），
# 因此 agent_core 目录也要在 sys.path 上，否则这些惰性导入在测试中会失败。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_core"))

from agent_core.tools import jev_tools


class _FakeRisky:
    """fake client：所有命令都判高危。"""
    def noul(self, state, instructions):
        return 0.95


class _FakeSafe:
    """fake client：所有命令都判安全。"""
    def noul(self, state, instructions):
        return 0.2


class _FakeNone:
    """fake client：模拟失败（返回 None = 降级）。"""
    def noul(self, state, instructions):
        return None


# ── risk_gate ────────────────────────────────────────────────
def test_risk_gate_high_probability():
    r = jev_tools.risk_gate("rm -rf /", client=_FakeRisky())
    assert r["needs_confirmation"] is True
    assert r["available"] is True
    assert r["probability"] == 0.95
    assert "风险" in r["reason"]


def test_risk_gate_safe():
    r = jev_tools.risk_gate("ls -la /tmp", client=_FakeSafe())
    assert r["needs_confirmation"] is False
    assert r["available"] is True
    assert "通过" in r["reason"]


def test_risk_gate_degrades_to_pass_when_unavailable():
    # 失败/未配置 → available=False 且 needs_confirmation=False（静默放行）
    r = jev_tools.risk_gate("rm -rf /", client=_FakeNone())
    assert r["needs_confirmation"] is False
    assert r["available"] is False


def test_risk_gate_threshold_boundary():
    """noul=0.7 恰好命中（>=阈值），0.69 不命中。"""
    class _Fake:
        def __init__(self, v):
            self.v = v
        def noul(self, state, instructions):
            return self.v
    assert jev_tools.risk_gate("x", client=_Fake(0.7))["needs_confirmation"] is True
    assert jev_tools.risk_gate("x", client=_Fake(0.69))["needs_confirmation"] is False


# ── JevClient 失败不抛异常 ──────────────────────────────────
def test_client_noul_unconfigured_returns_none():
    # 无 key 时 noul 返回 None，不抛异常
    c = jev_tools.JevClient(api_key="")
    assert c.noul("any", "instructions") is None


# ── shell_tools 集成 ─────────────────────────────────────────
def test_shell_jev_gate_can_be_disabled():
    """JEV_RISK_GATE=0 时 _jev_risk_gate_enabled 为 False。"""
    with mock.patch.dict(os.environ, {"JEV_RISK_GATE": "0"}):
        from agent_core.tools import shell_tools
        assert shell_tools._jev_risk_gate_enabled() is False


def test_shell_jev_gate_enabled_by_default():
    from agent_core.tools import shell_tools
    with mock.patch.dict(os.environ, {}, clear=True):
        assert shell_tools._jev_risk_gate_enabled() is True


@mock.patch("agent_core.tools.jev_tools._load_key", return_value="")
def test_shell_jev_gate_returns_none_when_no_key(_):
    """未配置 key 时 Jev 门控静默放行（返回 None）。"""
    from agent_core.tools import shell_tools
    with mock.patch.dict(os.environ, {"JEV_RISK_GATE": "1"}):
        assert shell_tools._jev_risk_gate("rm -rf /") is None

# ── loop_should_stop（loop_guard 接入）──────────────────────
class _FakeStop:
    def noul(self, state, instructions):
        return 0.95


class _FakeContinue:
    def noul(self, state, instructions):
        return 0.2


class _FakePick:
    def choice(self, state, instructions, criteria):
        return {"choice": "m2", "probabilities": {"m2": 0.9}}


class _FakeNoneChoice(_FakeNone):
    """_FakeNone + choice 原语（模拟 choice 调用失败）。"""
    def choice(self, state, instructions, criteria):
        return None


def test_loop_should_stop_true():
    assert jev_tools.loop_should_stop("read_file", "sig", 25, client=_FakeStop()) is True


def test_loop_should_continue_overrides_guard():
    assert jev_tools.loop_should_stop("read_file", "sig", 25, client=_FakeContinue()) is False


def test_loop_should_stop_degrades_to_none():
    """Jev 不可用 → None，调用方维持原判定。"""
    assert jev_tools.loop_should_stop("read_file", "sig", 25, client=_FakeNone()) is None


# ── vision_pick_model（vision_router 接入）──────────────────
def test_vision_pick_model_returns_candidate():
    assert jev_tools.vision_pick_model("state", ["m1", "m2"], client=_FakePick()) == "m2"


def test_vision_pick_model_rejects_unknown_choice():
    """Jev 返回候选之外的模型名 → 视为无效，返回 None（回退取第一个）。"""
    class _FakeBad:
        def choice(self, state, instructions, criteria):
            return {"choice": "not-in-candidates", "probabilities": {}}
    assert jev_tools.vision_pick_model("state", ["m1", "m2"], client=_FakeBad()) is None


def test_vision_pick_model_empty_candidates():
    assert jev_tools.vision_pick_model("state", [], client=_FakePick()) is None


def test_vision_pick_model_degrades_to_none():
    assert jev_tools.vision_pick_model("state", ["m1"], client=_FakeNoneChoice()) is None


# ── captcha_confidence（browser_tools 接入）─────────────────
def test_captcha_confidence_low_flags_unreliable():
    class _FakeLow:
        def noul(self, state, instructions):
            return 0.3
    assert jev_tools.captcha_confidence("state", client=_FakeLow()) == 0.3


def test_captcha_confidence_degrades_to_none():
    assert jev_tools.captcha_confidence("state", client=_FakeNone()) is None


# ── loop_guard 集成：Jev 判定"可继续"时跳过中断 ─────────────
def test_loop_guard_jev_continue_overrides():
    """检测1命中但 Jev 判定重复可能有效 → 不中断。"""
    import loop_guard as lg
    calls = [{"tool": "read_file", "signature": "read_file:/tmp/x"} for _ in range(25)]
    with mock.patch("tools.jev_tools.loop_should_stop", return_value=False):
        assert lg._detect_tool_loop(calls, 60) == ""


def test_loop_guard_jev_unavailable_keeps_original_verdict():
    """Jev 不可用（None/异常）→ 维持原判定中断。"""
    import loop_guard as lg
    calls = [{"tool": "read_file", "signature": "read_file:/tmp/x"} for _ in range(25)]
    with mock.patch("tools.jev_tools.loop_should_stop", return_value=None):
        assert "严格重复" in lg._detect_tool_loop(calls, 60)
    with mock.patch("tools.jev_tools.loop_should_stop", side_effect=RuntimeError("boom")):
        assert "严格重复" in lg._detect_tool_loop(calls, 60)


# ══════════════════════════════════════════════════════════════
# 表单决策：pick_option / option_matches / confirm_submit
# ══════════════════════════════════════════════════════════════
class _FakeGoalPick:
    """按关键词命中做决策：state/选项里含 want 词 → noul 高 / choice 选它。"""

    def __init__(self, *want):
        self.want = list(want)

    def _hit(self, s):
        # state 里若有 option= 行，只看该行，避免 goal 文案里的词污染"选项是否应勾选"的判定
        line = next((l for l in str(s).splitlines() if l.startswith("option=")), None)
        return any(w in (line or s) for w in self.want)

    def noul(self, state, instructions):
        return 0.9 if self._hit(state) else 0.1

    def choice(self, state, instructions, criteria):
        for k in criteria:
            if self._hit(k):
                return {"choice": k, "probabilities": {k: 0.9}}
        return {"choice": list(criteria)[0], "probabilities": {}}


def test_pick_option_returns_candidate_in_list():
    c = _FakeGoalPick("专业")
    assert jev_tools.pick_option("升级套餐", "套餐（下拉框）", ["基础", "专业"], client=c) == "专业"


def test_pick_option_rejects_unknown_choice():
    class _FakeBad:
        def choice(self, state, instructions, criteria):
            return {"choice": "不在候选里", "probabilities": {}}
    assert jev_tools.pick_option("g", "f", ["a", "b"], client=_FakeBad()) is None


def test_pick_option_empty_options():
    assert jev_tools.pick_option("g", "f", [], client=_FakeGoalPick("a")) is None


def test_pick_option_degrades_to_none():
    assert jev_tools.pick_option("g", "f", ["a"], client=_FakeNoneChoice()) is None


def test_option_matches_true_false():
    c = _FakeGoalPick("周报")
    assert jev_tools.option_matches("订阅周报", "sub", "订阅周报", client=c) is True
    assert jev_tools.option_matches("订阅周报", "sub", "订阅广告", client=c) is False


def test_option_matches_degrades_to_none():
    assert jev_tools.option_matches("g", "f", "o", client=_FakeNone()) is None


def test_confirm_submit_true_false_none():
    assert jev_tools.confirm_submit("g", "s", client=_FakeGoalPick("g")) is True
    assert jev_tools.confirm_submit("g", "s", client=_FakeGoalPick("别的")) is False
    assert jev_tools.confirm_submit("g", "s", client=_FakeNone()) is None


# ══════════════════════════════════════════════════════════════
# 表单规划纯函数（browser_form_fill 的核心，可脱离浏览器单测）
# ══════════════════════════════════════════════════════════════
def _plan(form, goal, values=None, submit=False, client=None):
    from agent_core.tools.browser_tools import _plan_form_actions
    actions, notes, _state = _plan_form_actions(form, goal, values or {}, submit=submit, client=client)
    return actions, notes


def _f(**kw):
    base = {"tag": "input", "type": "text", "name": "", "id": "", "label": "",
            "required": False, "selector": None, "value": ""}
    base.update(kw)
    return base


def test_plan_radio_group_picks_one():
    form = {"fields": [
        _f(type="radio", name="cat", label="技术", selector="#c1"),
        _f(type="radio", name="cat", label="生活", selector="#c2"),
    ], "buttons": []}
    actions, notes = _plan(form, "关注技术方向", client=_FakeGoalPick("技术"))
    assert len(actions) == 1
    a = actions[0]
    assert (a["action"], a["selector"], a["value"] if "value" in a else None) == ("check", "#c1", None)
    assert "技术" in a["label"]


def test_plan_radio_already_checked_is_noop():
    form = {"fields": [
        _f(type="radio", name="cat", label="技术", selector="#c1", checked=True),
        _f(type="radio", name="cat", label="生活", selector="#c2"),
    ], "buttons": []}
    actions, notes = _plan(form, "关注技术方向", client=_FakeGoalPick("技术"))
    assert actions == []
    assert any("无需改动" in n for n in notes)


def test_plan_checkbox_group_checks_and_unchecks():
    form = {"fields": [
        _f(type="checkbox", name="sub", label="订阅周报", selector="#s1", checked=False),
        _f(type="checkbox", name="sub", label="订阅广告", selector="#s2", checked=True),
    ], "buttons": []}
    actions, _ = _plan(form, "只订阅周报", client=_FakeGoalPick("周报"))
    got = {(a["action"], a["selector"]) for a in actions}
    assert got == {("check", "#s1"), ("uncheck", "#s2")}


def test_plan_select_uses_option_value():
    form = {"fields": [
        _f(tag="select", type="select", name="plan", label="套餐", selector="#plan",
           value="basic", options=[{"text": "基础", "value": "basic"},
                                   {"text": "专业", "value": "pro"}]),
    ], "buttons": []}
    actions, _ = _plan(form, "升级到专业版", client=_FakeGoalPick("专业"))
    assert actions == [{"action": "select", "selector": "#plan", "value": "pro",
                        "label": "套餐=专业"}]


def test_plan_text_field_needs_explicit_value():
    form = {"fields": [
        _f(type="email", name="email", label="邮箱", selector="#email", required=True),
    ], "buttons": []}
    # 未给值 → 不猜，跳过并说明
    actions, notes = _plan(form, "填表单", client=_FakeGoalPick("x"))
    assert actions == []
    assert any("不生成文本" in n for n in notes)
    # 给了值（label 命中）→ fill
    actions, _ = _plan(form, "填表单", {"邮箱": "a@b.com"}, client=_FakeGoalPick("x"))
    assert actions == [{"action": "fill", "selector": "#email", "value": "a@b.com",
                        "label": "邮箱=a@b.com"}]


def test_plan_submit_click_when_confirmed():
    form = {"fields": [_f(type="radio", name="cat", label="技术", selector="#c1")],
            "buttons": [{"selector": "#go", "text": "提交"}]}
    actions, _ = _plan(form, "选技术并提交", submit=True, client=_FakeGoalPick("技术"))
    assert any(a["action"] == "click" and a["selector"] == "#go" for a in actions)


def test_plan_submit_skipped_when_jev_says_no():
    """confirm_submit 判 False → 不提交，仅填表。"""
    class _NoSubmit(_FakeGoalPick):
        def noul(self, state, instructions):
            # 选项判定要给高分，提交确认要给低分
            return 0.1 if "form=" in state else 0.9
    form = {"fields": [_f(type="radio", name="cat", label="技术", selector="#c1")],
            "buttons": [{"selector": "#go", "text": "提交"}]}
    actions, notes = _plan(form, "选技术并提交", submit=True, client=_NoSubmit("技术"))
    assert not any(a["action"] == "click" for a in actions)
    assert any("跳过提交" in n for n in notes)


def test_plan_no_jev_no_actions():
    """Jev 全程不可用（None）→ 一个字段动作都不做，且逐项说明原因。"""
    form = {"fields": [
        _f(type="radio", name="cat", label="技术", selector="#c1"),
        _f(type="checkbox", name="sub", label="订阅周报", selector="#s1"),
        _f(tag="select", type="select", name="plan", label="套餐", selector="#plan",
           options=[{"text": "基础", "value": "basic"}]),
    ], "buttons": [{"selector": "#go", "text": "提交"}]}
    actions, notes = _plan(form, "随便填", client=_FakeNoneChoice())
    assert actions == []
    assert len(notes) >= 3


def test_plan_submit_rule_only_explicit_false_blocks():
    """提交规则：只有 Jev 明确判 False 才拦；Jev 不可用（None）不拦（submit 是用户显式要求）。"""
    form = {"fields": [], "buttons": [{"selector": "#go", "text": "提交"}]}
    actions, _ = _plan(form, "提交", submit=True, client=_FakeNoneChoice())
    assert [a["action"] for a in actions] == ["click"]


def test_plan_submit_button_without_selector_uses_text():
    """按钮无 id/name（selector=null）也要能提交：靠 text 用 role 定位。"""
    from agent_core.tools.browser_tools import _plan_form_actions
    form = {"fields": [_f(type="radio", name="cat", label="技术", selector="#c1")],
            "buttons": [{"selector": None, "text": "注册"}]}
    actions, notes, _ = _plan_form_actions(form, "选技术并提交", {}, submit=True,
                                           client=_FakeGoalPick("技术"))
    click = [a for a in actions if a["action"] == "click"]
    assert click and click[0]["selector"] is None and click[0]["text"] == "注册"
    assert not any("未找到可用" in n for n in notes)


def test_plan_group_label_uses_name_not_option_text():
    """单选/复选组的组名用 name，不能拿第一个选项文本当组名（否则输出/说明会误导）。"""
    from agent_core.tools.browser_tools import _plan_form_actions
    form = {"fields": [
        _f(type="radio", name="acct", label="个人", selector="#c1"),
        _f(type="radio", name="acct", label="企业", selector="#c2"),
    ], "buttons": []}
    actions, notes, state = _plan_form_actions(form, "选企业", {}, client=_FakeGoalPick("企业"))
    assert actions[0]["label"] == "acct=企业"
    assert state == ["acct=企业"]


def test_plan_state_summary_covers_unchanged_and_empty():
    """state 摘要要覆盖"未改动"和"空"的字段，提交前核对才看得见缺失项。"""
    from agent_core.tools.browser_tools import _plan_form_actions
    form = {"fields": [
        _f(type="checkbox", name="sub", label="订阅周报", selector="#s1", checked=True),
        _f(type="checkbox", name="sub", label="订阅广告", selector="#s2", checked=False),
        _f(type="email", name="email", label="邮箱", selector="#email", required=True),
    ], "buttons": []}
    actions, notes, state = _plan_form_actions(form, "只订阅周报", {}, client=_FakeGoalPick("周报"))
    assert actions == []
    assert "sub=[订阅周报]" in state
    assert "邮箱=(空)" in state
