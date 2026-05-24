"""SKILL.md 解析器 —— 将技能描述文件解析为结构化数据"""
import re
from pathlib import Path
from typing import Optional


class SkillDefinition:
    """解析后的技能定义"""
    
    def __init__(self, name: str, root: Path):
        self.name = name
        self.root = root
        self.description: str = ""
        self.triggers: list[str] = []
        self.instructions: str = ""
        self.tools_required: list[str] = []
        self.env_vars: dict[str, str] = {}
        self.metadata: dict[str, str] = {}
        self._valid = False
    
    @property
    def is_valid(self) -> bool:
        return self._valid
    
    def to_tool_description(self) -> str:
        """生成嵌入到 system prompt 中的技能描述块"""
        lines = [
            f"### 技能：{self.name}",
            f"描述：{self.description}",
        ]
        if self.triggers:
            lines.append(f"触发词：{'、'.join(self.triggers)}")
        lines.append("")
        lines.append(self.instructions)
        return "\n".join(lines)


class SkillLoader:
    """扫描并加载 SKILL.md 文件"""
    
    SECTION_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)
    
    @classmethod
    def load(cls, skill_dir: Path) -> Optional[SkillDefinition]:
        """加载单个目录下的 SKILL.md"""
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None
        
        name = skill_dir.name
        content = skill_file.read_text(encoding="utf-8")
        return cls._parse(name, skill_dir, content)
    
    @classmethod
    def load_all(cls, base_dir: Path) -> list[SkillDefinition]:
        """扫描 base_dir 下所有子目录，加载其中的 SKILL.md"""
        skills = []
        if not base_dir.exists():
            return skills
        for child in sorted(base_dir.iterdir()):
            if child.is_dir():
                skill = cls.load(child)
                if skill:
                    skills.append(skill)
        return skills
    
    @classmethod
    def _parse(cls, name: str, root: Path, content: str) -> SkillDefinition:
        skill = SkillDefinition(name, root)
        
        # 解析前置元数据（# 标题之后的 Key: Value 行）
        lines = content.split("\n")
        in_header = True
        header_lines: list[str] = []
        body_lines: list[str] = []
        for line in lines:
            if in_header and line.startswith("#"):
                continue  # 跳过标题
            if in_header and ": " in line and not line.startswith(" "):
                header_lines.append(line)
            else:
                in_header = False
                body_lines.append(line)
        
        # 解析头部键值对
        for hl in header_lines:
            if ": " in hl:
                k, v = hl.split(": ", 1)
                k = k.strip().lower()
                v = v.strip()
                if k == "description":
                    skill.description = v
                elif k == "trigger":
                    skill.triggers = [t.strip() for t in v.split("、")]
                elif k == "tools required":
                    skill.tools_required = [t.strip() for t in v.split("、")]
                elif k == "environment variables":
                    for pair in v.split(";"):
                        if "=" in pair:
                            ek, ev = pair.split("=", 1)
                            skill.env_vars[ek.strip()] = ev.strip()
                else:
                    skill.metadata[k] = v
        
        # 按 ## 分段解析
        body = "\n".join(body_lines)
        sections = cls.SECTION_PATTERN.split(body)
        
        current_section = ""
        for i, part in enumerate(sections):
            part = part.strip()
            if not part:
                continue
            if i % 2 == 0:
                # 当前段落在上一轮中已被记录为 section 标题
                pass
            
            if part in ("Description", "描述"):
                current_section = "description"
            elif part in ("Trigger", "触发词", "触发"):
                current_section = "trigger"
            elif part in ("Instructions", "指令", "说明"):
                current_section = "instructions"
            elif part.startswith("Environment"):
                current_section = "env"
            elif part.startswith("Tools"):
                current_section = "tools"
            else:
                # 内容
                if current_section == "description" and not skill.description:
                    skill.description = part
                elif current_section == "trigger":
                    parts = [t.strip().strip("「」""''") for t in part.replace("、", ",").split(",")]
                    skill.triggers.extend(p for p in parts if p)
                elif current_section == "instructions":
                    skill.instructions = part
                elif current_section == "tools":
                    skill.tools_required = [
                        t.strip() for t in part.replace("、", ",").split(",") if t.strip()
                    ]
        
        skill._valid = bool(skill.instructions)
        return skill
