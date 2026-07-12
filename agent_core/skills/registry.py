"""技能注册表 —— 管理所有已加载的技能"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from .loader import SkillDefinition, SkillLoader


class SkillRegistry:
    """技能注册表，提供技能的注册、查找、刷新"""
    
    def __init__(self):
        self._skills: dict[str, SkillDefinition] = {}
        self._base_dirs: Optional[list[Path]] = None
    
    def load_from(self, base_dir: Path | list[Path]) -> int:
        """从目录加载所有技能，返回加载数量。兼容 oh-my-openagent 常见技能目录。"""
        base_dirs = base_dir if isinstance(base_dir, list) else [base_dir]
        self._base_dirs = base_dirs
        skills = SkillLoader.load_from_dirs(base_dirs)
        self._skills.clear()
        for skill in skills:
            self._skills[skill.name] = skill
        return len(skills)
    
    def reload(self) -> int:
        """重新加载所有技能（热加载）"""
        if self._base_dirs:
            return self.load_from(self._base_dirs)
        return 0
    
    def register(self, skill: SkillDefinition) -> str:
        """手动注册一个技能"""
        self._skills[skill.name] = skill
        return f"✅ 已注册技能: {skill.name}"
    
    def unregister(self, name: str) -> bool:
        """卸载技能"""
        if name in self._skills:
            del self._skills[name]
            return True
        return False
    
    def get(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)
    
    def list_all(self) -> list[SkillDefinition]:
        return list(self._skills.values())
    
    def find_by_prompt(self, prompt: str) -> list[SkillDefinition]:
        """根据用户输入查找匹配的技能（按触发词或技能名子串匹配，去重保序）"""
        if not prompt:
            return []
        prompt_lower = prompt.lower()
        matched: dict[str, SkillDefinition] = {}
        for skill in self._skills.values():
            hit = False
            for trigger in skill.triggers:
                if trigger and trigger.lower() in prompt_lower:
                    hit = True
                    break
            if not hit and skill.name and skill.name.lower() in prompt_lower:
                hit = True
            if hit:
                matched[skill.name] = skill
        return list(matched.values())

    def generate_prompt_block(self) -> str:
        """生成 system prompt 中的精简技能目录（仅 name + 描述 + 触发词）。

        完整指令不再常驻 system prompt，改为在用户请求命中触发词时由
        render_injection_block() 按需注入到当轮对话，显著降低每次 LLM 调用的 token 成本。
        """
        if not self._skills:
            return ""

        blocks = [
            "",
            "## 已加载的技能",
            "以下是当前已加载到系统中的 Skills 目录。当你判断用户需求匹配某个技能的触发词/描述时，"
            "该技能的完整工作流会在本轮对话中自动注入，请按注入的内容执行；如果技能要求的专属工具不可用，需要明确说明限制。",
            "用户询问“有哪些技能”“已加载哪些 Skills”“你会哪些技能”时，必须优先列出这些 Skills 名称与描述，而不是只列底层工具。",
            "",
        ]
        for skill in self._skills.values():
            blocks.append(skill.to_prompt_summary())
            blocks.append("")
        return "\n".join(blocks)

    def render_injection_block(self, skills: list[SkillDefinition]) -> str:
        """把命中的技能完整指令渲染成注入块（追加到当轮用户消息末尾，仅本轮生效）。"""
        if not skills:
            return ""
        parts = [
            "## 已为你激活的技能（按本次请求触发，请在本轮对话中严格遵循其工作流）",
            "",
        ]
        for skill in skills:
            parts.append(skill.to_tool_description())
            parts.append("")
        return "\n".join(parts)


# 全局单例
_registry: Optional[SkillRegistry] = None

def get_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry
