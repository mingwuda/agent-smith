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