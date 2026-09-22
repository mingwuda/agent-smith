"""通用 Jev / TypeSafe System One 决策库。

Jev 是 TypeSafe AI 的「系统一模型」：不生成文本，只返回带校准概率的类型化决策。
本模块是对官方 `POST /v1/systemone` API 的极薄封装，提供三种决策原语：

  - noul  (是/否概率 0~1)
  - choice(多选一 + 各选项概率)
  - score (对有序档位的打分 0~N)

同时提供两个业务层封装（内部用三原语组合）：
  - risk_gate            ：shell 命令语义风险门控（本项目最契合场景）
  - captcha_confidence   ：浏览器验证码识别结果的二次置信度校验

## 设计原则
1. **零第三方依赖**：仅用标准库 urllib.request（httpx 虽已装，但本库保持零依赖，
   便于无框架环境独立测试/复用）。
2. **失败静默降级**：任何调用（网络/超时/鉴权/解析）失败都返回 None，绝不抛异常。
   调用方必须在拿到 None 时回退到自己的既有逻辑。这是"叠加一层决策"而非"取代"。
3. **可注入 client**：提供 FakeJevClient 便于测试；生产用 JevClient。
4. **配置来源**：优先 env `TYPESAFE_API_KEY`，否则回退 ~/.desktop_agent/config.json
   的 typesafe_api_key（与 agent_core/config.py 的 env 映射机制一致）。

依赖：urllib 标准库。运行需 Python >= 3.8。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import time
from typing import Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# API 端点与默认模型
API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 15          # 单次调用超时（秒）
DEFAULT_RETRIES = 1           # 瞬时失败重试次数

# 从环境读取 key（与 agent_core/config.py 的 env 映射保持一致）
_ENV_KEY = "TYPESAFE_API_KEY"
# config.json 回退路径（相对 home）
_CONFIG_REL = ".desktop_agent/config.json"


class JevClient:
    """裸 HTTP 封装 /v1/systemone。失败返回 None（不抛异常）。"""

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL,
                 timeout: float = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES,
                 base_url: str = API_URL):
        self.api_key = api_key if api_key is not None else _load_key()
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.base_url = base_url

    def _post(self, payload: dict) -> Optional[dict]:
        """发送请求并解析响应。失败返回 None。"""
        if not self.api_key:
            logger.debug("Jev: 未配置 TYPESAFE_API_KEY，跳过")
            return None
        body = json.dumps(payload).encode("utf-8")

        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base_url, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        last_err: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                # 鉴权/参数错误等确定性失败不重试；瞬时（5xx/限流）重试
                if e.code < 500:
                    logger.warning("Jev: HTTP %s %s（第 %d/%d 次，不重试）",
                                   e.code, e.reason, attempt + 1, self.retries + 1)
                    return None
            except Exception as e:
                last_err = e
                # 超时/连接类瞬时错误，可重试
            if attempt < self.retries:
                time.sleep(0.3 * (attempt + 1))
        logger.debug("Jev: 调用失败（重试 %d 次后）: %r", self.retries, last_err)
        return None

    # ── 三种原语 ────────────────────────────────────────────────
    def noul(self, state: str, instructions: str) -> Optional[float]:
        """是/否概率 0~1。如"这条命令是否危险？" 返回 None=失败/未配置。"""
        resp = self._post({
            "state": state, "model": self.model,
            "questions": {"q": {"type": "noul", "instructions": instructions}},
        })
        if not resp:
            return None
        return resp.get("answers", {}).get("q", {}).get("noul")

    def choice(self, state: str, instructions: str, criteria: Dict[str, str],
               ) -> Optional[Dict[str, Union[str, Dict[str, float]]]]:
        """多选一。criteria: {选项: 说明}。返回 {"choice":..., "probabilities":{...}}"""
        resp = self._post({
            "state": state, "model": self.model,
            "questions": {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}},
        })
        if not resp:
            return None
        a = resp.get("answers", {}).get("q", {})
        if "choice" not in a:
            return None
        return {"choice": a["choice"], "probabilities": a.get("probabilities", {})}

    def score(self, state: str, instructions: str, criteria: List[str],
              ) -> Optional[float]:
        """对有序档位打分。criteria: 从低到高的档位描述列表。返回档位序号(0起)或 None"""
        resp = self._post({
            "state": state, "model": self.model,
            "questions": {"q": {"type": "score", "instructions": instructions, "criteria": criteria}},
        })
        if not resp:
            return None
        return resp.get("answers", {}).get("q", {}).get("score")


def _load_key() -> str:
    """优先 env，其次 config.json。返回 '' 表示未配置。"""
    k = os.environ.get(_ENV_KEY, "")
    if k:
        return k
    try:
        home = os.path.expanduser("~")
        cfg_path = os.path.join(home, _CONFIG_REL)
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f).get("typesafe_api_key", "") or ""
    except Exception:
        return ""


def _normalize_bool(v) -> bool:
    """把 Jev 返回的 noul 转成布尔（阈值可配）。非数字回退 False。"""
    try:
        return float(v) >= 0.7
    except (TypeError, ValueError):
        return False


def _client(api_key: Optional[str] = None) -> JevClient:
    """构造 client（允许注入 key，缺省走配置）。"""
    return JevClient(api_key=api_key)


# ════════════════════════════════════════════════════════════════
# 业务层封装
# ════════════════════════════════════════════════════════════════

def risk_gate(command: str, api_key: Optional[str] = None, client=None) -> Dict[str, object]:
    """shell 命令语义风险门控（Noul）。

    判断一条命令是否需要人工确认。这是对 shell_tools 既有正则高危闸的**补充**，
    覆盖正则覆盖不到的语义危险（混淆、下载即执行、编码绕过、多条叠加等）。

    参数:
      command: 待评估命令
      api_key: 显式指定 key（缺省走配置）
      client: 可注入决策 client（测试用，缺省自动构造）

    返回 dict：
      - needs_confirmation: bool  是否建议确认（阈值 0.7）
      - probability: float|None   Jev 给出的危险概率(0~1)，拿到前为 None
      - reason: str               命中/未命中的说明
      - available: bool           Jev 是否可用（配了 key 且调用成功）
    """
    clf = client or _client(api_key)
    noul = clf.noul(command, (
        "This is a shell command that may be destructive, irreversible or a security "
        "risk. Return 1.0 if it SHOULD require human confirmation before execution; "
        "return 0.0 if it is safe to auto-run."
    ))
    if noul is None:
        # 失败/未配置 → 不追加确认，交回现有逻辑
        return {"needs_confirmation": False, "probability": None, "reason": "Jev 不可用（未配置/调用失败）自动放行", "available": False}
    risky = _normalize_bool(noul)
    return {
        "needs_confirmation": risky,
        "probability": noul,
        "reason": "Jev 语义风险门控命中" if risky else "Jev 语义风险门控通过",
        "available": True,
    }


def captcha_confidence(state_text: str, api_key: Optional[str] = None) -> Optional[float]:
    """验证码识别结果的二次置信度校验（Noul）。

    对 LLM 识别验证码后的结果做交叉校验，减少"识别错→白点/白刷"的无效动作。
    state_text: 待判断文本（如验证码描述 + 识别结果 + 页面提示）。返回置信度 0~1，失败 None。
    """
    clf = _client(api_key)
    return clf.noul(state_text, (
        "The following is a description of an image and the result of an automated captcha "
        "recognition. Return 1.0 if the recognition result is highly likely CORRECT and "
        "confident; return 0.0 if it is likely wrong, incomplete, or should be refreshed/re-done."
    ))


# ════════════════════════════════════════════════════════════════
# Fake client（无 key / 演示 / 测试用，结构与 JevClient 一致）
# ════════════════════════════════════════════════════════════════

class FakeJevClient:
    """确定性 mock：同一接口，供无 key / 测试时演示行为契约。"""

    def noul(self, state: str, instructions: str) -> Optional[float]:
        s = state.lower()
        risk_kw = ["rm -rf", "rm -r", "--force", "-f ", "force", "reset --hard",
                   "curl", "wget", "chmod", "taskkill", "format ", "mkfs",
                   "shutdown", "reboot", "drop table", "del /s", "| sh", "| bash"]
        hits = sum(1 for k in risk_kw if k in s)
        if hits >= 2:
            return 0.95
        if hits == 1:
            return 0.8
        return 0.2

    def choice(self, state: str, instructions: str, criteria: Dict[str, str],
               ) -> Optional[Dict]:
        # 预置一个稳定选择（演示），真实实现由模型语义判断
        keys = list(criteria.keys())
        first = keys[0] if keys else "default"
        return {"choice": first, "probabilities": {first: 0.9}}

    def score(self, state: str, instructions: str, criteria: List[str]) -> Optional[float]:
        return 0.8  # 演示固定值


def _make_client(api_key: Optional[str] = None, fake: bool = False) -> Union[JevClient, FakeJevClient]:
    """根据参数返回真实或 fake client。fake=True 或未配 key 时返回 Fake。"""
    if fake:
        return FakeJevClient()
    if os.getenv(_ENV_KEY):
        return JevClient(api_key=api_key)
    # 未配 key：也回退 config.json；仍无则 fake（保证演示可跑）
    if _load_key():
        return JevClient(api_key=api_key)
    return FakeJevClient()