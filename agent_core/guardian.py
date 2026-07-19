"""系统守护层 —— 启动自愈（第 0 层基础设施）。

设计铁律（ponytail）：
- 本模块**只依赖标准库**，绝不 import 任何业务模块、绝不调用 LLM。
  一旦守护层自身成为启动失败源，就本末倒置了。
- 任何异常都被内部吞掉：宁可少做一次回退，也绝不向外抛。
- 可用性优先于正确性：启动只要怀疑就回退到 last-known-good，"先让它活过来"。

职责：
1. self_heal_on_boot：包住 init_agent()。失败则按 LIFO 回退最近一次进化产物
   （移入隔离区、保留待查、不删除）并重试；全部回退仍失败则整体隔离
   generated_dir 再试；最终失败返回 False（服务存活但 agent=None，等人工）。
2. record_evolution：登记一次进化产物（P3 自改写时调用，供自愈 LIFO 回退）。
3. validate_artifact：apply 闸门静态校验（P3 启用；P1 提供可用基础版，不接调用方）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("guardian")

MANIFEST_NAME = "manifest.json"
BOOT_OK_NAME = ".boot_ok"


# ---------- manifest 读写（全部容错，失败即视为空） ----------

def _read_manifest(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[守护] manifest 读取失败，按空处理: %s", path)
    return {"entries": []}


def _write_manifest(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("[守护] manifest 写入失败")


# ---------- 隔离区（保留待查，不删除） ----------

def _quarantine_target(quarantine_dir: Path, src: Path) -> Path:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    target = quarantine_dir / f"{ts}_{src.name}"
    n = 1
    while target.exists():
        target = quarantine_dir / f"{ts}_{n}_{src.name}"
        n += 1
    return target


def _move_to_quarantine(quarantine_dir: Path, src: Path) -> bool:
    """把进化产物移入隔离区（保留待查，不删除）。成功返回 True。"""
    if not src.exists():
        return False
    try:
        target = _quarantine_target(quarantine_dir, src)
        shutil.move(str(src), str(target))
        logger.info("[守护] 产物已隔离: %s -> %s", src, target)
        return True
    except Exception:
        logger.exception("[守护] 移动产物到隔离区失败: %s", src)
        return False


# ---------- 启动自愈 ----------

def _hash_manifest(manifest_path: Path) -> str:
    try:
        if manifest_path.exists():
            return hashlib.md5(manifest_path.read_bytes()).hexdigest()[:12]
    except Exception:
        pass
    return ""


def _try_boot(boot_fn: Callable[[], None], boot_ok_marker: Path, manifest_path: Path) -> bool:
    """执行一次 boot_fn。成功写 .boot_ok 标记并返回 True；抛异常返回 False。"""
    try:
        boot_fn()
    except Exception:
        logger.exception("[守护] boot_fn 执行抛异常")
        return False
    try:
        boot_ok_marker.parent.mkdir(parents=True, exist_ok=True)
        boot_ok_marker.write_text(
            json.dumps({"ts": int(time.time()), "manifest_hash": _hash_manifest(manifest_path)},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return True


def self_heal_on_boot(
    boot_fn: Callable[[], None],
    *,
    generated_dir: Path,
    manifest_path: Path,
    quarantine_dir: Path,
    boot_ok_marker: Path,
    full_reset_times: int = 1,
) -> bool:
    """启动自愈：尝试 boot_fn()，失败时按 LIFO 回退最近进化产物并重试。

    返回 True 表示 agent 已就绪；False 表示自愈失败（服务存活但 agent=None）。

    回退顺序（可用性优先）：
      1) 首次尝试
      2) 逐条回退 manifest 中登记的进化产物（最近先回退）
      3) 整体隔离 generated_dir 再试（full_reset_times 次）
      4) 全失败 → 返回 False，不写 boot_ok
    """
    # 1) 首次尝试（最常见路径：没有任何进化产物出问题）
    if _try_boot(boot_fn, boot_ok_marker, manifest_path):
        return True

    # 2) 逐条回退进化产物（LIFO）
    manifest = _read_manifest(manifest_path)
    entries = manifest.get("entries", [])
    while entries:
        entry = entries.pop()
        src = Path(entry.get("path") or "")
        logger.error("[守护] 启动失败，回退最近进化产物: kind=%s path=%s note=%s",
                     entry.get("kind"), src, entry.get("note"))
        _move_to_quarantine(quarantine_dir, src)
        _write_manifest(manifest_path, {"entries": entries})
        if _try_boot(boot_fn, boot_ok_marker, manifest_path):
            logger.warning("[守护] 已通过回退 %s 恢复启动", src.name)
            return True

    # 3) 整体隔离 generated_dir（兜底：manifest 之外的脏产物）
    for _ in range(max(0, full_reset_times)):
        if generated_dir.exists():
            logger.error("[守护] 仍失败，整体隔离 generated_dir: %s", generated_dir)
            # 隔离前清空 manifest，避免回退逻辑重复处理已隔离内容
            _write_manifest(manifest_path, {"entries": []})
            _move_to_quarantine(quarantine_dir, generated_dir)
            if _try_boot(boot_fn, boot_ok_marker, manifest_path):
                logger.warning("[守护] 已通过清空 generated_dir 恢复启动")
                return True

    # 4) 全部失败
    logger.critical(
        "[守护] 自愈失败：多次回退仍无法启动 Agent。"
        "服务存活但 agent=None，需人工介入（检查 %s 与 %s）。",
        quarantine_dir, boot_ok_marker.parent,
    )
    return False


# ---------- 进化产物登记（P3 自改写时调用） ----------

def record_evolution(*, kind: str, path: str, note: str = "", manifest_path: Path) -> None:
    """登记一次进化产物，供自愈时按 LIFO 回退。"""
    manifest = _read_manifest(manifest_path)
    entries = manifest.get("entries", [])
    entries.append({
        "kind": kind,
        "path": path,
        "note": note,
        "created_at": int(time.time()),
    })
    _write_manifest(manifest_path, {"entries": entries})


def roll_back_latest(manifest_path: Path, quarantine_dir: Path) -> Optional[Path]:
    """回退 manifest 中登记的**最近一条**进化产物（LIFO）。

    弹出最后一条 entry，把对应文件移入隔离区（保留待查，不删除），
    并写回 manifest。返回被隔离的文件路径；manifest 为空时返回 None。
    供守护进程（控制面）在进程外执行 boot 恢复编排时复用。
    """
    manifest = _read_manifest(manifest_path)
    entries = manifest.get("entries", [])
    if not entries:
        return None
    entry = entries.pop()
    src = Path(entry.get("path") or "")
    moved = None
    if src.exists():
        if _move_to_quarantine(quarantine_dir, src):
            moved = quarantine_dir / f"{int(time.time())}_{src.name}"
    _write_manifest(manifest_path, {"entries": entries})
    logger.warning("[守护] 回退进化产物: kind=%s path=%s note=%s",
                   entry.get("kind"), src, entry.get("note"))
    return moved


# ---------- apply 闸门（P3 自改写时启用；P1 提供可用基础版，不接调用方） ----------

def validate_artifact(kind: str, content: str, *, allowed_config_keys: Optional[set] = None) -> bool:
    """静态校验进化产物，不调用 LLM。返回 True 表示可放行。

    - skill:        frontmatter 必须存在且能解析出 name/description 至少一个非空
    - config_patch: 必须是 JSON 对象且所有 key 都在 allowed_config_keys 内
    - 其他 kind:    默认拒绝，要求显式声明
    """
    if kind == "skill":
        return _validate_skill(content)
    if kind == "config_patch":
        return _validate_config_patch(content, allowed_config_keys)
    return False


def _validate_skill(content: str) -> bool:
    if not content or "---" not in content:
        return False
    parts = content.split("---", 2)
    if len(parts) < 3:
        return False
    block = parts[1]
    has_name = bool(_frontmatter_field(block, "name"))
    has_desc = bool(_frontmatter_field(block, "description"))
    return has_name or has_desc


def _frontmatter_field(block: str, key: str) -> str:
    norm = key.lower().replace("_", "-")
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k.strip().lower().replace("_", "-") == norm:
            return v.strip().strip("'\"")
    return ""


def _validate_config_patch(content: str, allowed_keys: Optional[set]) -> bool:
    try:
        patch = json.loads(content)
    except Exception:
        return False
    if not isinstance(patch, dict):
        return False
    # 未声明允许集合时默认拒绝，避免误放行未知字段
    if allowed_keys is None:
        return False
    return all(k in allowed_keys for k in patch.keys())
