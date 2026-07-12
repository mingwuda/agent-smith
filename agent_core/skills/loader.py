"""SKILL.md 解析器 —— 将技能描述文件解析为结构化数据"""
import os
import re
from pathlib import Path
from typing import Any, Optional


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
        self.metadata: dict[str, Any] = {}
        self.format: str = "desktop-agent"
        self._valid = False
    
    @property
    def is_valid(self) -> bool:
        return self._valid
    
    def to_tool_description(self) -> str:
        """生成嵌入到 system prompt 中的技能描述块（完整指令，体积大）"""
        lines = [
            f"### 技能：{self.name}",
            f"描述：{self.description}",
        ]
        if self.triggers:
            lines.append(f"触发词：{'、'.join(self.triggers)}")
        lines.append("")
        lines.append(self.instructions)
        return "\n".join(lines)

    def to_prompt_summary(self) -> str:
        """生成 system prompt 精简目录条目（仅 name + 描述 + 触发词，约 150 字节/个）。

        完整指令不再常驻 system prompt，而是在用户请求命中触发词时按需注入，
        避免 19 个技能的完整 SKILL.md 每次 LLM 调用都被重发（原本占 ~109KB）。
        """
        lines = [f"- **{self.name}**"]
        if self.description:
            lines.append(f"  {self.description}")
        if self.triggers:
            lines.append(f"  触发词：{'、'.join(self.triggers)}")
        return "\n".join(lines)


class SkillLoader:
    """扫描并加载 SKILL.md 文件"""
    
    SECTION_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)
    FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
    HOME_SKILL_DIRS = (
        Path.home() / ".config" / "opencode" / "skills",
    )
    
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
    def load_from_dirs(cls, base_dirs: list[Path]) -> list[SkillDefinition]:
        """按优先级从多个目录加载技能；后加载的同名技能覆盖先加载的。"""
        by_name: dict[str, SkillDefinition] = {}
        for base_dir in cls.expand_skill_dirs(base_dirs):
            for skill in cls.load_all(base_dir):
                by_name[skill.name] = skill
        return list(by_name.values())

    @classmethod
    def expand_skill_dirs(cls, base_dirs: list[Path]) -> list[Path]:
        """展开配置目录与 oh-my-openagent 常见技能目录。"""
        dirs: list[Path] = []
        for base_dir in base_dirs:
            dirs.extend(cls._split_path_list(base_dir))
        dirs.extend(cls.HOME_SKILL_DIRS)

        seen: set[str] = set()
        expanded: list[Path] = []
        for item in dirs:
            path = item.expanduser()
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            expanded.append(path)
        return expanded

    @classmethod
    def _split_path_list(cls, base_dir: Path) -> list[Path]:
        raw = str(base_dir)
        if os.pathsep in raw:
            return [Path(p) for p in raw.split(os.pathsep) if p.strip()]
        return [base_dir]
    
    @classmethod
    def _parse(cls, name: str, root: Path, content: str) -> SkillDefinition:
        skill = SkillDefinition(name, root)
        content = cls._parse_frontmatter(skill, content)
        
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
                elif k == "name":
                    skill.name = v or skill.name
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
        # SECTION_PATTERN.split 的结果结构为：[前言, 标题1, 正文1, 标题2, 正文2, ...]
        # 即奇数下标是 ## 段标题，偶数下标是段正文（下标 0 为首个标题前的前言）。
        # 必须据此区分「标题」与「正文」：未知标题要清空 current_section，
        # 否则其后的正文会被错误归入上一个已知段落（典型表现为整篇 SKILL.md
        # 被塞进 triggers，即技能目录串字问题）。标题文本本身绝不计入正文。
        current_section = ""
        for i, part in enumerate(sections):
            part = part.strip()
            if not part:
                continue
            if i % 2 == 1:
                # 这是一个 ## 段标题
                title = part
                if title in ("Description", "描述"):
                    current_section = "description"
                elif title in ("Trigger", "触发词", "触发"):
                    current_section = "trigger"
                elif title in ("Instructions", "指令", "说明"):
                    current_section = "instructions"
                elif title.startswith("Environment"):
                    current_section = "env"
                elif title.startswith("Tools"):
                    current_section = "tools"
                else:
                    # 未知段落标题：停止向下归属内容
                    current_section = ""
                continue
            # i 为偶数：段正文（或首个标题前的前言）
            if current_section == "description" and not skill.description:
                skill.description = part
            elif current_section == "trigger":
                # 触发词可能以顿号/逗号/换行混排，统一切分并清理项目符号
                raw = part.replace("、", ",").replace("\n", ",")
                parts = [t.strip().lstrip("- ").strip("「」""''").strip() for t in raw.split(",")]
                skill.triggers.extend(p for p in parts if p)
            elif current_section == "instructions":
                skill.instructions = part
            elif current_section == "tools":
                skill.tools_required = [
                    t.strip() for t in part.replace("、", ",").split(",") if t.strip()
                ]
        
        if not skill.instructions:
            skill.instructions = cls._body_without_title(body).strip()
        if not skill.triggers and skill.description:
            skill.triggers = cls._extract_embedded_triggers(skill.description)
        skill._valid = bool(skill.instructions)
        return skill

    @classmethod
    def _parse_frontmatter(cls, skill: SkillDefinition, content: str) -> str:
        match = cls.FRONTMATTER_PATTERN.match(content)
        if not match:
            return content

        skill.format = "oh-my-openagent"
        frontmatter = match.group(1)
        for key, value in cls._parse_simple_yaml(frontmatter).items():
            normalized = key.lower().replace("_", "-")
            if normalized == "name" and isinstance(value, str):
                skill.name = value.strip() or skill.name
            elif normalized == "description" and isinstance(value, str):
                skill.description = value.strip()
            elif normalized in {"trigger", "triggers"}:
                skill.triggers = cls._as_list(value)
            elif normalized in {"tools-required", "tools"}:
                skill.tools_required = cls._as_list(value)
            elif normalized in {"environment-variables", "env", "env-vars"} and isinstance(value, dict):
                skill.env_vars.update({str(k): str(v) for k, v in value.items()})
            else:
                skill.metadata[normalized] = value
        return content[match.end():]

    @classmethod
    def _parse_simple_yaml(cls, text: str) -> dict[str, Any]:
        """解析 Skill frontmatter 的常见 YAML 子集；复杂结构保留为原始文本。"""
        data: dict[str, Any] = {}
        current_key = ""
        block_lines: list[str] = []

        def flush_block():
            nonlocal current_key, block_lines
            if current_key:
                data[current_key] = "\n".join(block_lines).rstrip()
                current_key = ""
                block_lines = []

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if current_key and (raw_line.startswith(" ") or raw_line.startswith("\t") or line.startswith("- ")):
                block_lines.append(line)
                continue
            flush_block()
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if not value:
                current_key = key
                block_lines = []
            else:
                data[key] = cls._parse_scalar(value)
        flush_block()
        return data

    @classmethod
    def _parse_scalar(cls, value: str) -> Any:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                return []
            return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
        return value

    @classmethod
    def _as_list(cls, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if not isinstance(value, str):
            return []
        lines = []
        for line in value.splitlines():
            item = line.strip()
            if item.startswith("- "):
                lines.append(item[2:].strip())
            elif item:
                lines.extend(part.strip() for part in item.replace("、", ",").split(","))
        return [item.strip().strip("「」""''") for item in lines if item.strip()]

    @classmethod
    def _extract_embedded_triggers(cls, text: str) -> list[str]:
        match = re.search(r"\bTriggers?\s*:\s*(.+)$", text, flags=re.IGNORECASE)
        if not match:
            return []
        raw = match.group(1).strip().rstrip(".")
        return [
            item.strip().strip("`'\"「」")
            for item in raw.replace("、", ",").split(",")
            if item.strip().strip("`'\"「」")
        ]

    @classmethod
    def _body_without_title(cls, body: str) -> str:
        lines = body.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and lines[0].startswith("#"):
            lines.pop(0)
        return "\n".join(lines)
