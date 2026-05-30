"""配置管理"""
import json
import os
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONFIG_FILE = Path.home() / ".desktop_agent" / "config.json"


def _bundled_samples_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "agent_core" / "samples"
    return Path(__file__).parent / "samples"


def _split_path_list(raw: str) -> list[Path]:
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]

DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "is_custom": False,
        "api_key": "",
        "model": "gpt-4o",
        "base_url": "",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    },
    "deepseek": {
        "name": "DeepSeek",
        "is_custom": False,
        "api_key": "",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "qwen": {
        "name": "通义千问",
        "is_custom": False,
        "api_key": "",
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"],
    },
    "custom": {
        "name": "自定义",
        "is_custom": True,
        "api_key": "",
        "model": "",
        "base_url": "",
        "models": [],
    },
}


@dataclass
class AgentConfig:
    """Agent 配置"""
    
    # 模型配置
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    active_provider: str = "openai"
    providers: dict[str, dict[str, Any]] = field(default_factory=lambda: deepcopy(DEFAULT_PROVIDERS))
    
    # 工作区
    workspace: str = str(Path.home() / "agent_workspace")
    
    # Skills 目录
    skills_dir: str = ""
    
    # 服务器
    host: str = "127.0.0.1"
    port: int = 8899
    
    # 用量限制
    max_cost_per_day: float = 5.0
    recursion_limit: int = 60
    api_max_retries: int = 3
    api_timeout_seconds: float = 30.0
    api_host_ips: str = ""
    context_window_tokens: int = 0
    
    system_prompt: str = (
        "你是一个桌面 AI 智能体，可以自主完成用户交给你的任务。\n\n"
        "## 核心能力\n"
        "- 读写文件、管理目录\n"
        "- 执行 Python 代码\n"
        "- 查看 Git 仓库状态、diff、日志和提交内容，并在用户明确要求时暂存、提交、推送和 revert 回退提交\n"
        "- 搜索网页和获取网页内容\n"
        "- 获取系统信息\n"
        "- 将独立子任务委派给 coder、reviewer、debugger 子代理\n\n"
        "## 工作规范\n"
        "1. 先理解用户需求，拆解为步骤\n"
        "2. 选择最合适的工具执行\n"
        "3. 每一步完成后检查结果\n"
        "4. 最终给用户清晰的结果总结\n"
        "5. 如果遇到错误，尝试修复或告知用户\n"
        "6. 需要了解代码变更时，优先使用 git_status、git_diff、git_log、git_show 等 Git 查看工具\n"
        "7. 只有用户明确要求提交、推送或回退提交时，才使用 git_add、git_commit、git_commit_all、git_push、git_revert；不要主动 pull、reset 或 restore\n"
        "8. 对独立编码、审查、排障任务，可以使用 delegate_task 子代理工具；当前版本同步等待结果，接口已为后续并行执行预留 task id/status\n\n"
        "## 工作区\n"
        f"你的工作区目录是：{Path.home() / 'agent_workspace'}\n"
        "读写文件时使用相对于工作区的路径。"
    )
    
    @classmethod
    def load(cls) -> "AgentConfig":
        """从文件 + 环境变量加载配置（环境变量优先级更高）"""
        config = cls()
        file_data: dict[str, Any] = {}
        
        # 1. 从文件加载
        if CONFIG_FILE.exists():
            try:
                file_data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for key, value in file_data.items():
                    if hasattr(config, key) and value is not None:
                        setattr(config, key, value)
            except (json.JSONDecodeError, OSError):
                pass
        
        # 2. 环境变量覆盖（优先级最高）
        env_map = {
            "LLM_MODEL": ("model", str),
            "LLM_API_KEY": ("api_key", str),
            "OPENAI_API_KEY": ("api_key", str),  # 兼容
            "LLM_BASE_URL": ("base_url", str),
            "OPENAI_BASE_URL": ("base_url", str),  # 兼容
            "LLM_PROVIDER": ("active_provider", str),
            "AGENT_WORKSPACE": ("workspace", str),
            "AGENT_SKILLS_DIR": ("skills_dir", str),
            "AGENT_HOST": ("host", str),
            "AGENT_PORT": ("port", int),
            "MAX_COST_PER_DAY": ("max_cost_per_day", float),
            "AGENT_RECURSION_LIMIT": ("recursion_limit", int),
            "AGENT_API_MAX_RETRIES": ("api_max_retries", int),
            "AGENT_API_TIMEOUT_SECONDS": ("api_timeout_seconds", float),
            "AGENT_API_HOST_IPS": ("api_host_ips", str),
            "AGENT_CONTEXT_WINDOW_TOKENS": ("context_window_tokens", int),
        }
        env_overrides = set()
        for env_key, (attr_name, cast_fn) in env_map.items():
            val = os.getenv(env_key)
            if val is not None:
                try:
                    setattr(config, attr_name, cast_fn(val))
                    env_overrides.add(attr_name)
                except (ValueError, TypeError):
                    pass

        legacy_keys = {"api_key", "model", "base_url"}
        apply_legacy = bool(legacy_keys.intersection(file_data) or legacy_keys.intersection(env_overrides))
        config._normalize_providers(apply_legacy=apply_legacy)
        config.recursion_limit = max(1, int(config.recursion_limit or 60))
        config.api_max_retries = max(0, int(config.api_max_retries or 0))
        config.api_timeout_seconds = max(1.0, float(config.api_timeout_seconds or 30.0))
        config.context_window_tokens = max(0, int(config.context_window_tokens or 0))
        
        # 3. 填充默认值
        if not config.skills_dir:
            config.skills_dir = str(_bundled_samples_dir())
        
        # 4. 初始化目录
        Path(config.workspace).mkdir(parents=True, exist_ok=True)
        for skills_dir in _split_path_list(config.skills_dir):
            skills_dir.mkdir(parents=True, exist_ok=True)
        
        return config

    def _normalize_providers(self, apply_legacy: bool = False):
        providers = deepcopy(DEFAULT_PROVIDERS)
        if isinstance(self.providers, dict):
            for provider_id, values in self.providers.items():
                if not isinstance(values, dict):
                    continue
                current = providers.setdefault(provider_id, {
                    "name": provider_id,
                    "api_key": "",
                    "model": "",
                    "base_url": "",
                    "models": [],
                    "is_custom": True,
                })
                current.update({k: v for k, v in values.items() if v is not None})
                current.setdefault("models", [])
                current.setdefault("name", provider_id)
                current.setdefault("api_key", "")
                current.setdefault("model", "")
                current.setdefault("base_url", "")
                current.setdefault("is_custom", provider_id not in DEFAULT_PROVIDERS or provider_id == "custom")
        self.providers = providers

        if self.active_provider not in self.providers:
            self.active_provider = "openai"

        # 兼容旧配置和环境变量：只有显式提供顶层字段时，才写入当前厂商。
        if apply_legacy:
            active = self.providers[self.active_provider]
            if self.api_key:
                active["api_key"] = self.api_key
            if self.model:
                active["model"] = self.model
            if self.base_url:
                active["base_url"] = self.base_url

        self._sync_effective_model()

    def _sync_effective_model(self):
        active = self.providers.get(self.active_provider, {})
        self.api_key = str(active.get("api_key", "") or "")
        self.model = str(active.get("model", "") or DEFAULT_PROVIDERS["openai"]["model"])
        self.base_url = str(active.get("base_url", "") or "")

    def update_provider(
        self,
        provider_id: str,
        api_key: str = "",
        model: str = "",
        base_url: str = "",
        provider_name: str = "",
    ):
        self._normalize_providers()
        if provider_id not in self.providers:
            self.providers[provider_id] = {
                "name": provider_name or provider_id,
                "is_custom": True,
                "api_key": "",
                "model": "",
                "base_url": "",
                "models": [],
            }
        self.active_provider = provider_id
        provider = self.providers[provider_id]
        if provider.get("is_custom") and provider_name:
            provider["name"] = provider_name
        if api_key:
            provider["api_key"] = api_key
        if model:
            provider["model"] = model
            models = provider.setdefault("models", [])
            if provider.get("is_custom") and model not in models:
                models.append(model)
        provider["base_url"] = base_url
        self._sync_effective_model()
    
    def save(self):
        """保存配置到文件"""
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active_provider": self.active_provider,
            "providers": self.providers,
            "api_key": self.api_key,
            "model": self.model,
            "base_url": self.base_url,
            "workspace": self.workspace,
            "skills_dir": self.skills_dir,
            "host": self.host,
            "port": self.port,
            "max_cost_per_day": self.max_cost_per_day,
            "recursion_limit": self.recursion_limit,
            "api_max_retries": self.api_max_retries,
            "api_timeout_seconds": self.api_timeout_seconds,
            "api_host_ips": self.api_host_ips,
            "context_window_tokens": self.context_window_tokens,
        }
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    def to_api_dict(self) -> dict:
        """返回给前端展示的配置（脱敏 API Key）"""
        self._normalize_providers()
        providers = {}
        for provider_id, provider in self.providers.items():
            api_key = str(provider.get("api_key", "") or "")
            providers[provider_id] = {
                "name": provider.get("name", provider_id),
                "is_custom": bool(provider.get("is_custom", False)),
                "model": provider.get("model", ""),
                "base_url": provider.get("base_url", ""),
                "models": provider.get("models", []),
                "api_key_configured": bool(api_key),
                "api_key_preview": api_key[:8] + "..." if len(api_key) > 8 else ("已设置" if api_key else "未设置"),
            }
        return {
            "active_provider": self.active_provider,
            "provider_name": self.providers[self.active_provider].get("name", self.active_provider),
            "providers": providers,
            "model": self.model,
            "api_key_configured": bool(self.api_key),
            "api_key_preview": self.api_key[:8] + "..." if len(self.api_key) > 8 else ("已设置" if self.api_key else "未设置"),
            "base_url": self.base_url,
            "workspace": self.workspace,
            "skills_dir": self.skills_dir,
            "host": self.host,
            "port": self.port,
            "max_cost_per_day": self.max_cost_per_day,
            "recursion_limit": self.recursion_limit,
            "api_max_retries": self.api_max_retries,
            "api_timeout_seconds": self.api_timeout_seconds,
            "api_host_ips": self.api_host_ips,
            "context_window_tokens": self.context_window_tokens,
        }
