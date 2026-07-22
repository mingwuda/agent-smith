"""桌面 AI 智能体核心（拆分后入口：组合各 Mixin）。

为降低单文件体积，按职责拆分为多个 Mixin：
- AgentInitMixin：构造 / 配置 / 模型与图构建
- AgentRunMixin：run / 流式输出 / 检查点修复
- AgentChatMixin：chat_sync / 反思 / 技能 / 用量统计
模块级辅助函数集中在 agent_helpers。对外仍导出 DesktopAgent。
"""
from agent_init import AgentInitMixin
from agent_run import AgentRunMixin
from agent_chat import AgentChatMixin


class DesktopAgent(AgentInitMixin, AgentRunMixin, AgentChatMixin):
    """桌面 AI 智能体核心。"""
