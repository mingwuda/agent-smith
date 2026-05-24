"""配置管理"""
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


CONFIG_FILE = Path.home() / ".desktop_agent" / "config.json"


@dataclass
class AgentConfig:
    """Agent 配置"""
    
    # 模型配置
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    
    # 工作区
    workspace: str = str(Path.home() / "agent_workspace")
    
    # Skills 目录
    skills_dir: str = ""
    
    # 服务器
    host: str = "127.0.0.1"
    port: int = 8899
    
    # 用量限制
    max_cost_per_day: float = 5.0
    
    system_prompt: str = (
        "你是一个桌面 AI 智能体，可以自主完成用户交给你的任务。\n\n"
        "## 核心能力\n"
        "- 读写文件、管理目录\n"
        "- 执行 Python 代码\n"
        "- 搜索网页和获取网页内容\n"
        "- 获取系统信息\n\n"
        "## 工作规范\n"
        "1. 先理解用户需求，拆解为步骤\n"
        "2. 选择最合适的工具执行\n"
        "3. 每一步完成后检查结果\n"
        "4. 最终给用户清晰的结果总结\n"
        "5. 如果遇到错误，尝试修复或告知用户\n\n"
        "## 工作区\n"
        f"你的工作区目录是：{Path.home() / 'agent_workspace'}\n"
        "读写文件时使用相对于工作区的路径。"
    )
    
    @classmethod
    def load(cls) -> "AgentConfig":
        """从文件 + 环境变量加载配置（环境变量优先级更高）"""
        config = cls()
        
        # 1. 从文件加载
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for key, value in data.items():
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
            "AGENT_WORKSPACE": ("workspace", str),
            "AGENT_SKILLS_DIR": ("skills_dir", str),
            "AGENT_HOST": ("host", str),
            "AGENT_PORT": ("port", int),
            "MAX_COST_PER_DAY": ("max_cost_per_day", float),
        }
        for env_key, (attr_name, cast_fn) in env_map.items():
            val = os.getenv(env_key)
            if val is not None:
                try:
                    setattr(config, attr_name, cast_fn(val))
                except (ValueError, TypeError):
                    pass
        
        # 3. 填充默认值
        if not config.skills_dir:
            config.skills_dir = str(Path(__file__).parent / "samples")
        
        # 4. 初始化目录
        Path(config.workspace).mkdir(parents=True, exist_ok=True)
        Path(config.skills_dir).mkdir(parents=True, exist_ok=True)
        
        return config
    
    def save(self):
        """保存配置到文件"""
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "api_key": self.api_key,
            "model": self.model,
            "base_url": self.base_url,
            "workspace": self.workspace,
            "skills_dir": self.skills_dir,
            "host": self.host,
            "port": self.port,
            "max_cost_per_day": self.max_cost_per_day,
        }
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    def to_api_dict(self) -> dict:
        """返回给前端展示的配置（脱敏 API Key）"""
        return {
            "model": self.model,
            "api_key_configured": bool(self.api_key),
            "api_key_preview": self.api_key[:8] + "..." if len(self.api_key) > 8 else ("已设置" if self.api_key else "未设置"),
            "base_url": self.base_url,
            "workspace": self.workspace,
            "skills_dir": self.skills_dir,
            "host": self.host,
            "port": self.port,
            "max_cost_per_day": self.max_cost_per_day,
        }
