"""技能路由"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...config import AgentConfig
from ...skills.registry import get_registry
from ..deps import _require_admin, _get_current_user
from logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["skills"])


class SkillInfo(BaseModel):
    name: str
    description: str
    triggers: list[str] = []
    has_instructions: bool = False
    format: str = "desktop-agent"
    source: str = ""
    mcp_declared: bool = False


class SkillFileEntry(BaseModel):
    path: str
    size: int = 0
    kind: str = "file"   # file | dir


class SkillDetail(BaseModel):
    name: str
    description: str
    triggers: list[str] = []
    instructions: str = ""
    format: str = "desktop-agent"
    source: str = ""
    mcp_declared: bool = False
    tools_required: list[str] = []
    files: list[SkillFileEntry] = []


class SkillFileContent(BaseModel):
    name: str
    file_path: str
    content: str
    size: int
    truncated: bool = False


class ReloadResponse(BaseModel):
    message: str
    count: int


# 技能详情读取相关常量
SKILL_FILE_PREVIEW_MAX_BYTES = 256 * 1024  # 单文件预览上限 256KB


def _list_skill_files(skill_root: Path) -> list[SkillFileEntry]:
    """列出技能目录下的所有文件（不含子目录展开），过滤隐藏/常见临时文件。"""
    entries: list[SkillFileEntry] = []
    if not skill_root or not skill_root.exists():
        return entries
    try:
        for item in sorted(skill_root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name.startswith("."):
                continue
            rel = item.relative_to(skill_root).as_posix()
            if item.is_dir():
                entries.append(SkillFileEntry(path=rel + "/", size=0, kind="dir"))
                # 展开一层子目录（常用 scripts/、references/）
                try:
                    for sub in sorted(item.iterdir(), key=lambda p: p.name.lower()):
                        if sub.name.startswith("."):
                            continue
                        sub_rel = sub.relative_to(skill_root).as_posix()
                        if sub.is_file():
                            try:
                                entries.append(SkillFileEntry(
                                    path=sub_rel,
                                    size=min(sub.stat().st_size, 1024 * 1024 * 50),
                                    kind="file",
                                ))
                            except OSError:
                                entries.append(SkillFileEntry(path=sub_rel, size=0, kind="file"))
                except OSError:
                    pass
            else:
                try:
                    entries.append(SkillFileEntry(
                        path=rel,
                        size=min(item.stat().st_size, 1024 * 1024 * 50),
                        kind="file",
                    ))
                except OSError:
                    entries.append(SkillFileEntry(path=rel, size=0, kind="file"))
    except OSError:
        pass
    return entries


@router.get("/skills", response_model=list[SkillInfo])
def list_skills():
    """列出所有已加载的技能"""
    from ...main import _app_base_dir
    registry = get_registry()
    # 如果尚未加载技能，尝试加载
    if not registry.list_all():
        app_base = _app_base_dir()
        registry.load_from([
            Path(AgentConfig.load().skills_dir),
            app_base / "skills",
            app_base / ".claude" / "skills",
            app_base / ".agents" / "skills",
        ])
    return [
        SkillInfo(
            name=s.name,
            description=s.description,
            triggers=s.triggers,
            has_instructions=bool(s.instructions),
            format=s.format,
            source=str(s.root),
            mcp_declared="mcp" in s.metadata,
        )
        for s in registry.list_all()
    ]


@router.post("/skills/reload", response_model=ReloadResponse)
def reload_skills():
    """热加载所有技能"""
    from ...main import agent
    if not agent:
        raise HTTPException(503, "Agent 尚未初始化")
    count = agent.reload_skills()
    return ReloadResponse(message=f"已重新加载 {count} 个技能", count=count)


@router.get("/skills/{name}", response_model=SkillDetail)
def get_skill_detail(name: str):
    """获取技能详情（含 SKILL.md 正文和文件清单）"""
    registry = get_registry()
    skill = registry.get(name)
    if not skill:
        raise HTTPException(404, f"技能 {name} 不存在")
    return SkillDetail(
        name=skill.name,
        description=skill.description,
        triggers=skill.triggers,
        instructions=skill.instructions,
        format=skill.format,
        source=str(skill.root),
        mcp_declared="mcp" in skill.metadata,
        tools_required=skill.tools_required,
        files=_list_skill_files(skill.root),
    )


@router.get("/skills/{name}/files", response_model=SkillFileContent)
def get_skill_file(name: str, path: str = ""):
    """读取技能目录下的指定文件内容（仅在工作区/技能根目录内可读）。"""
    registry = get_registry()
    skill = registry.get(name)
    if not skill:
        raise HTTPException(404, f"技能 {name} 不存在")

    skill_root: Path = skill.root
    if not path:
        raise HTTPException(400, "path 不能为空")

    # 防止路径穿越
    target = (skill_root / path).resolve()
    try:
        target.relative_to(skill_root.resolve())
    except ValueError:
        raise HTTPException(403, f"路径超出技能目录: {path}")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"文件不存在: {path}")

    try:
        raw = target.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"读取失败: {e}")

    truncated = False
    if len(raw) > SKILL_FILE_PREVIEW_MAX_BYTES:
        raw = raw[:SKILL_FILE_PREVIEW_MAX_BYTES]
        truncated = True

    # 文本/二进制区分
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        return SkillFileContent(
            name=skill.name, file_path=path, content=text, size=len(raw), truncated=truncated
        )

    try:
        file_size = target.stat().st_size
    except OSError:
        file_size = len(raw)

    return SkillFileContent(
        name=skill.name,
        file_path=path,
        content=text,
        size=file_size,
        truncated=truncated,
    )
