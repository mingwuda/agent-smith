"""技能注册表 —— 管理所有已加载的技能"""
from pathlib import Path
from typing import Optional

from .loader import SkillDefinition, SkillLoader


class SkillRegistry:
    """技能注册表，提供技能的注册、查找、刷新"""
    
    def __init__(self):
        self._skills: dict[str, SkillDefinition] = {}
        self._base_dir: Optional[Path] = None
    
    def load_from(self, base_dir: Path) -> int:
        """从目录加载所有技能，返回加载数量"""
        self._base_dir = base_dir
        skills = SkillLoader.load_all(base_dir)
        count = 0
        for skill in skills:
            self._skills[skill.name] = skill
            count += 1
        return count
    
    def reload(self) -> int:
        """重新加载所有技能（热加载）"""
        if self._base_dir:
            self._skills.clear()
            return self.load_from(self._base_dir)
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
        """根据用户输入查找匹配的技能（按触发词匹配）"""
        prompt_lower = prompt.lower()
        matched = []
        for skill in self._skills.values():
            for trigger in skill.triggers:
                if trigger.lower() in prompt_lower:
                    matched.append(skill)
                    break
        return matched
    
    def generate_prompt_block(self) -> str:
        """生成本prompt块嵌入到 system prompt 中"""
        if not self._skills:
            return ""
        
        blocks = [
            "",
            "## 已加载的技能",
            "以下技能你可以根据用户需求调用。当用户提到触发词时，优先使用对应技能。",
            "",
        ]
        for skill in self._skills.values():
            blocks.append(skill.to_tool_description())
            blocks.append("")
        return "\n".join(blocks)


# 全局单例
_registry: Optional[SkillRegistry] = None

def get_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry
