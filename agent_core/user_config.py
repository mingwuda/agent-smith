"""用户级配置 —— 每个用户可独立保存模型/Provider 等设置，默认继承全局配置。"""
import json
from pathlib import Path
from typing import Any

from config import AgentConfig

USERS_DIR = Path.home() / ".desktop_agent" / "users"
USER_CONFIG_NAME = "user_config.json"


def _user_config_path(user_id: str) -> Path:
    p = USERS_DIR / user_id / USER_CONFIG_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_user_config(user_id: str) -> dict[str, Any]:
    path = _user_config_path(user_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_user_config(user_id: str, data: dict[str, Any]) -> None:
    path = _user_config_path(user_id)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user_effective_config(user_id: str) -> dict[str, Any]:
    """返回用户生效配置：全局配置 + 用户覆盖字段。"""
    global_cfg = AgentConfig.load()
    user_overrides = _load_user_config(user_id)
    providers = user_overrides.get("providers", global_cfg.providers)
    # 为每个 provider 补充 api_key_configured 标记，供前端过滤使用
    enriched_providers = {}
    for pid, p in providers.items():
        if not isinstance(p, dict):
            # 跳过 __provider_order__ 等非 provider 条目（它们可能是 list）
            continue
        enriched_providers[pid] = dict(p)
        enriched_providers[pid]["api_key_configured"] = bool(str(p.get("api_key", "") or ""))
    merged = {
        "active_provider": user_overrides.get("active_provider", global_cfg.active_provider),
        "model": user_overrides.get("model", global_cfg.model),
        "api_key": user_overrides.get("api_key", global_cfg.api_key),
        "base_url": user_overrides.get("base_url", global_cfg.base_url),
        "providers": enriched_providers,
        # 展示顺序是全局偏好（非用户级覆盖），必须一并透传：否则前端拿不到
        # provider_order，排序退化为字典序，与设置页（/settings）顺序不一致。
        "provider_order": [
            pid for pid in (global_cfg.providers.get("__provider_order__") or [])
            if isinstance(pid, str) and pid in providers
        ],
        "review_provider_id": user_overrides.get("review_provider_id", global_cfg.review_provider_id),
        "review_model": user_overrides.get("review_model", global_cfg.review_model),
        # 透传超时配置，供前端联动前端 fetch 超时
        "llm_hard_timeout_seconds": global_cfg.llm_hard_timeout_seconds,
        "llm_idle_timeout_seconds": global_cfg.llm_idle_timeout_seconds,
        "api_timeout_seconds": global_cfg.api_timeout_seconds,
    }
    return merged


def save_user_config(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """保存用户级配置，仅持久化用户提交的字段。"""
    allowed_keys = {
        "active_provider",
        "model",
        "api_key",
        "base_url",
        "providers",
        "review_provider_id",
        "review_model",
    }
    overrides = {k: v for k, v in payload.items() if k in allowed_keys and v is not None}
    existing = _load_user_config(user_id)
    existing.update(overrides)
    _write_user_config(user_id, existing)
    return get_user_effective_config(user_id)
