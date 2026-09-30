"""Case → Skill 离线蒸馏（对应 EverOS 的 procedural memory 自进化闭环）。

设计（半自动模式，ponytail: 用户要求"生成后待用户确认再生效"）：
1. 采集：agent 每完成一次带工具调用的任务，reflect_on_task 已产出一条 technique 反思
   （{t,v}）。本模块把「同类 technique」累积成结构化 Case（_case_<topic>），
   记录背景(用户需求)、动作序列(工具调用轨迹)、结果、适用条件、出现次数。
2. 蒸馏：同一 Case 的 occurrences 达到阈值（_CASE_PROMOTE_THRESHOLD）后，
   自动起草候选 SKILL.md 写入「待审批目录」pending/，并落一条 _skill_<name> 指针标记候选。
3. 半自动生效：用户确认 → approve_skill() 把 SKILL.md 从 pending 移入技能目录
   (config.skills_dir) 并触发 registry.reload() → 技能在下次请求命中触发词时生效。
   用户拒绝 → reject_skill() 丢弃候选。

不修改既有 KV 存储机制（仍用 LocalMemory），只在它之上叠加 case 聚合与技能起草。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from memory.local_memory import get_memory

logger = logging.getLogger(__name__)

# 同一 topic 的 technique 出现多少次才晋升为候选技能（防单次偶发即生成低质技能）
CASE_PROMOTE_THRESHOLD = 3
# 单个 Case 最多保留的动作序列长度（防止无限膨胀）
MAX_CASE_ACTIONS = 12
# 待审批技能目录名（相对 skills_dir）
PENDING_DIR = "pending"


# ---------- Case 聚合 ----------

def _topic_key(technique_text: str) -> str:
    """从 technique 反思文本提炼稳定话题键（md5），用于聚合同类 Case。

    用完整反思文本做指纹（P1-5）：避免 v[:20] 只取前 20 字造成的碰撞——
    前 20 字相同但方法不同的反思（如「git 冲突|逐个手动合并」vs
    「git 冲突|逐个自动合并」）会被误合并，导致后一种方法丢失。
    完整 hash 让不同方法各自成 Case，主题相关但路径不同的可复用 skill 分开记录。
    """
    text = technique_text.strip()
    return hashlib.md5(("case:" + text).encode("utf-8")).hexdigest()[:12]


def _case_key(topic_hash: str) -> str:
    return f"_case_{topic_hash}"


def accumulate_case(uid: str, user_message: str, reflection: dict,
                    actions: Optional[list[dict]] = None) -> dict:
    """累积一条 technique 反思为结构化 Case。

    返回当前 Case 的 value（含 occurrences）。若 reflection 不是 technique 类型，
    则不改写（preference/pitfall 仍走原有 _learned_/_avoid_ 逻辑）。
    """
    if not reflection or reflection.get("t") != "technique":
        return reflection
    v = str(reflection.get("v", "")).strip()
    if not v:
        return reflection

    mem = get_memory(uid)
    topic = _topic_key(v)
    key = _case_key(topic)
    existing = mem.get(key)
    now = time.time()

    if existing and isinstance(existing, dict):
        existing = dict(existing)
        existing["occurrences"] = int(existing.get("occurrences", 1)) + 1
        existing["updated_at"] = now
        existing["result"] = "多次成功"  # 多次重复出现说明路径可靠
        # 追加本次动作序列（P1-4：去除连续重复的相同工具——同一工具反复调用
        # 是噪音，让工作流步骤可读；保留跨步骤的顺序去重前的整体轨迹）
        seq = existing.get("actions", []) or []
        if actions:
            new_acts = [a.get("tool", "?") for a in actions if isinstance(a, dict)]
            existing["actions"] = _dedupe_tool_seq((seq + new_acts)[-MAX_CASE_ACTIONS:])
        # 追加背景（去重，最多保留 5 条）
        ctx = existing.get("contexts", []) or []
        if user_message and user_message[:40] not in ctx:
            ctx.insert(0, user_message[:100])
            existing["contexts"] = ctx[:5]
    else:
        existing = {
            "t": "technique",
            "v": v,
            "topic": v[:20],
            "occurrences": 1,
            "contexts": [user_message[:100]] if user_message else [],
            "actions": _dedupe_tool_seq(
                [a.get("tool", "?") for a in actions if isinstance(a, dict)][-MAX_CASE_ACTIONS:]
            ) if actions else [],
            "result": "成功",
            "created_at": now,
            "updated_at": now,
        }
    mem.set(key, existing)
    return existing


def _dedupe_tool_seq(tools: list[str]) -> list[str]:
    """P1-4：去除连续重复的相同工具名（git_status,git_status → git_status）。

    同一工具紧邻反复调用是噪音，会让 SKILL.md 指令显得重复冗长；合并后工作流可读。
    仅合并「连续相同」，不改变不同工具间的真实顺序。
    """
    out = []
    for t in tools:
        if not t:
            continue
        if not out or out[-1] != t:
            out.append(t)
    return out


def find_promotable_cases(uid: str, threshold: Optional[int] = None) -> list[dict]:
    """找出已达到晋升阈值的 Case（occurrences >= threshold），返回带 key 的列表。"""
    threshold = threshold or CASE_PROMOTE_THRESHOLD
    mem = get_memory(uid)
    out = []
    for item in mem.list_items():
        key = item["key"]
        if not key.startswith("_case_"):
            continue
        val = item["value"]
        if isinstance(val, dict) and int(val.get("occurrences", 0) or 0) >= threshold:
            out.append({"key": key, "value": val})
    return out


# ---------- Skill 起草（半自动） ----------

def _safe_skill_name(v: str) -> str:
    """把反思话题转成合法技能名（ASCII 化 + 清理非法字符）。"""
    name = re.sub(r"[^a-zA-Z0-9]+", "-", v[:40]).strip("-").lower()
    return name or "case-skill"


# 合法技能名：由 _safe_skill_name 产出，恒为小写字母数字与 '-'（开头非 '-'）。
# 审批/拒绝接口的 skill_name 直接来自请求体，**必须**先过这里再拼路径。
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _checked_skill_name(skill_name: str) -> Optional[str]:
    """校验来自请求体的技能名；非法返回 None。

    安全背景（2026-09-29 实证复现）：approve_skill / reject_skill 会把 skill_name
    直接拼成 `Path(skills_dir)/.../skill_name` 后 rmtree / 写文件：
      - `".."`   → 命中 `skills_dir/pending/..`（即 skills_dir 本身）→ **递归清空整个技能目录**
      - `"../.."`→ 生产环境（skills_dir=/opt/desktop-agent/agent_core/samples）
                   → **清空 /opt/desktop-agent/agent_core 整棵源码树**
    注意顶层目录名会因 rmdir 对末段为 `..` 返回 EINVAL 而残留，容易误判成"没事"。
    因此这里拒绝：空名、含路径分隔符、`.` / `..`、隐藏名、非字母数字开头、超长。
    """
    name = (skill_name or "").strip()
    if not name or not _SKILL_NAME_RE.match(name):
        return None
    # 双保险：规范化后必须仍是单层名字（防未知平台的路径语义差异）
    if name in (".", "..") or Path(name).name != name:
        return None
    return name


def _within(target: Path, root: Path) -> bool:
    """target 解析后是否仍在 root 之内（防符号链接等绕过）。

    注意 rmdir/rmtree 的最终解析目标：必须先 resolve 再比较。
    """
    try:
        Path(target).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def draft_skill(uid: str, case_key: str, skills_dir: Path,
                supersede: bool = True) -> Optional[dict]:
    """为已达阈值的 Case 起草候选 SKILL.md 到待审批目录。

    skills_dir: config.skills_dir（技能加载根目录），pending 子目录存放候选。
    返回 {skill_name, pending_path}；已存在同名候选 / 已生效同名技能则跳过。
    """
    mem = get_memory(uid)
    case = mem.get(case_key)
    if not isinstance(case, dict):
        return None
    name = _safe_skill_name(str(case.get("v", "")))
    pending_dir = Path(skills_dir) / PENDING_DIR
    pending_dir.mkdir(parents=True, exist_ok=True)

    target = pending_dir / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return {"skill_name": name, "pending_path": str(target), "skipped": True}

    # 若已存在同名生效技能且 supersede=True，则跳过（避免重复占位）
    active = Path(skills_dir) / name / "SKILL.md"
    if supersede and active.exists():
        return None

    contexts = case.get("contexts") or []
    actions = case.get("actions") or []
    skills_md = _render_skill_md(name, case.get("v", ""), contexts, actions)
    target.write_text(skills_md, encoding="utf-8")

    # 落 _skill_ 指针标记候选
    mem.set(f"_skill_{name}", {
        "status": "pending",
        "skill_name": name,
        "case_key": case_key,
        "occurrences": case.get("occurrences", 0),
        "pending_path": str(target),
        "created_at": time.time(),
    })
    return {"skill_name": name, "pending_path": str(target), "skipped": False}


def _render_skill_md(name: str, technique: str, contexts: list[str],
                     actions: list[str]) -> str:
    """把 Case 内容渲染成 SKILL.md（复用已有 SKILL.md 格式，供 registry 解析）。"""
    triggers = [technique[:20]] if technique else []
    ctx_block = "\n".join(f"- {c}" for c in contexts) if contexts else "- 通用"
    action_block = "\n".join(f"- {a}" for a in actions) if actions else "- 待补充"
    return (
        "## 技能：{name}\n"
        "description: {tech}\n"
        "trigger: {trig}\n"
        "\n"
        "## 指令\n"
        "从过往成功案例蒸馏的可复用工作流，遇到相同问题时可参考以下步骤：\n"
        "{actions}\n"
        "\n"
        "## 适用场景\n"
        "{ctx}\n"
        "（本技能由 Case → Skill 离线蒸馏自动生成，待用户确认后生效）\n"
    ).format(name=name, tech=technique, trig="、".join(triggers),
             actions=action_block, ctx=ctx_block)


def approve_skill(uid: str, skill_name: str, skills_dir: Path) -> str:
    """用户确认后：把候选 SKILL.md 从 pending 移入技能目录并标记已生效。

    返回操作文案。
    """
    # 安全：skill_name 来自请求体，必须先严格校验（否则可写到 skills_dir 之外）
    name = _checked_skill_name(skill_name)
    if name is None:
        logger.warning("[case_forge] 拒绝非法技能名: %r", skill_name)
        return f"❌ 非法技能名 '{skill_name}'"
    root = Path(skills_dir).resolve()
    pending = root / PENDING_DIR / name / "SKILL.md"
    dest = root / name / "SKILL.md"
    if not (_within(pending, root) and _within(dest, root)):
        logger.warning("[case_forge] 技能名解析后越出技能目录: %r", skill_name)
        return f"❌ 非法技能名 '{skill_name}'"
    if not pending.exists():
        return f"❌ 未找到待审批技能 '{name}'"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        pending.unlink()
    else:
        pending.rename(dest)
    # 清掉 pndding 空壳目录（文件名已移走或删除）
    try:
        pending.parent.rmdir()
    except OSError:
        pass
    mem = get_memory(uid)
    mem.set(f"_skill_{name}", {
        "status": "active",
        "skill_name": name,
        "created_at": time.time(),
    })
    # 通知外层 reload 技能（由调用方在业务层执行 registry.reload()）
    return f"✅ 技能 '{name}' 已生效"


def reject_skill(uid: str, skill_name: str, skills_dir: Path) -> str:
    """用户拒绝：丢弃候选 SKILL.md 与 _skill_ 指针。"""
    # 安全：这里是 rmtree，skill_name 绝不可直接拼接（`..` 可清空任意祖先目录内容）
    name = _checked_skill_name(skill_name)
    if name is None:
        logger.warning("[case_forge] 拒绝非法技能名（reject）: %r", skill_name)
        return f"❌ 非法技能名 '{skill_name}'"
    root = Path(skills_dir).resolve()
    pending = root / PENDING_DIR / name
    # 双保险：必须严格位于 skills_dir 之内，且绝不能等于 skills_dir 本身
    if pending == root or not _within(pending, root):
        logger.warning("[case_forge] 技能名解析后越出技能目录（reject）: %r", skill_name)
        return f"❌ 非法技能名 '{skill_name}'"
    if pending.exists():
        import shutil
        shutil.rmtree(pending, ignore_errors=True)
    key = f"_skill_{name}"
    mem = get_memory(uid)
    if mem.get(key) is not None:
        mem.delete(key)
    return f"🗑 已舍弃技能候选 '{name}'"


def list_pending_skills(uid: str, skills_dir: Path) -> list[dict]:
    """列出待确认的技能候选（供前端/用户审阅）。"""
    mem = get_memory(uid)
    out = []
    for item in mem.list_items():
        key = item["key"]
        if not key.startswith("_skill_"):
            continue
        val = item["value"]
        if not isinstance(val, dict) or val.get("status") != "pending":
            continue
        pending_path = val.get("pending_path")
        exists = Path(pending_path).exists() if pending_path else False
        if exists:
            out.append({"skill_name": val.get("skill_name"), "pending_path": pending_path,
                        "occurrences": val.get("occurrences", 0)})
    return out