"""文件浏览器路由 — 安全浏览项目目录、读取文件内容"""
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

router = APIRouter(tags=["files"])

# 允许浏览的文件类型（白名单）
_BROWSABLE_EXTENSIONS = {
    # 文本/代码
    '.txt', '.md', '.json', '.yaml', '.yml', '.xml', '.toml', '.ini',
    '.cfg', '.conf', '.log', '.env', '.gitignore', '.dockerignore',
    '.html', '.css', '.js', '.ts', '.jsx', '.tsx', '.vue', '.svelte',
    '.py', '.java', '.c', '.cpp', '.h', '.hpp', '.go', '.rs', '.rb',
    '.php', '.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd', '.sql',
    '.csv', '.tsv', '.svg', '.graphql', '.proto', '.makefile', '.cmake',
    # 配置文件
    '.editorconfig', '.eslintrc', '.prettierrc', 'Dockerfile', 'Makefile',
}
# 最大读取文件大小（2MB）
_MAX_FILE_SIZE = 2 * 1024 * 1024


def _resolve_base_path(request: Request) -> Path:
    """从请求中解析用户可访问的根目录（项目目录或工作目录）"""
    from services.workspace import _workspace_for_user
    uid = getattr(request.state, "user_id", "default")
    # 如果传了 project_id，优先用项目的 directory_path
    return _workspace_for_user(uid)


def _resolve_allowed_root(request: Request, path: Optional[str] = None) -> Path:
    """
    解析允许访问的根目录。
    - path 为空：返回默认 workspace
    - path 为绝对路径：验证是否在 workspace 下（或直接使用该路径作为项目目录）
    - 返回安全的根路径对象
    """
    if not path or not path.strip():
        base = _resolve_base_path(request)
        if not base:
            base = Path.home()
        return base

    target = Path(path).resolve()

    # 安全检查：不允许路径穿越到敏感目录
    _dangerous_prefixes = ['/etc', '/usr', '/var', '/System', '/Library',
                           '/bin', '/sbin', '/Applications']
    for prefix in _dangerous_prefixes:
        try:
            if str(target).startswith(prefix):
                raise HTTPException(status_code=403, detail=f"禁止访问系统目录: {prefix}")
        except (ValueError, TypeError):
            pass

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")

    return target


def _is_text_file(file_path: Path) -> bool:
    """判断是否为可预览的文本文件"""
    ext = file_path.suffix.lower()
    if ext in _BROWSABLE_EXTENSIONS:
        return True
    # 无扩展名但较小的文件也尝试以文本打开
    if file_path.is_file() and file_path.stat().st_size < _MAX_FILE_SIZE and ext == '':
        return True
    return False


@router.get("/files/browse")
async def browse_directory(
    request: Request,
    path: str = Query("", description="要浏览的目录路径（相对或绝对）"),
    project_id: str = Query("", description="项目 ID，用于确定根目录"),
):
    """列出目录结构（树形数据），前端渲染为文件浏览器"""
    root = _resolve_allowed_root(request, path)

    if not root.is_dir():
        raise HTTPException(status_code=400, detail="指定路径不是目录")

    entries = []
    try:
        for item in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            # 跳过隐藏文件/目录（以 . 开头的）和 node_modules / __pycache__ 等
            _skip_names = {'.git', '__pycache__', 'node_modules', '.idea', '.vscode',
                          '.next', '.nuxt', 'dist', 'build', '.venv', 'venv',
                          '.DS_Store', 'Thumbs.db'}
            if item.name in _skip_names:
                continue
            stat = item.stat()
            entry = {
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
            if item.is_file():
                entry["ext"] = item.suffix.lower()
                entry["previewable"] = _is_text_file(item)
            entries.append(entry)

        return {
            "path": str(root),
            "name": root.name,
            "entries": entries,
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限访问: {path}")


@router.get("/files/read")
async def read_file(
    request: Request,
    path: str = Query(..., description="要读取的文件完整路径"),
    project_id: str = Query("", description="项目 ID"),
):
    """读取单个文件的内容（文本文件返回内容，二进制返回错误）"""
    from services.workspace import _workspace_for_user

    target = Path(path).resolve()

    # 安全检查：不允许路径穿越
    _dangerous_prefixes = ['/etc', '/usr', '/var', '/System', '/Library']
    for prefix in _dangerous_prefixes:
        try:
            if str(target).startswith(prefix):
                raise HTTPException(status_code=403, detail=f"禁止访问系统目录")
        except (ValueError, TypeError):
            pass

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"不是文件: {path}")

    size = target.stat().st_size
    if size > _MAX_FILE_SIZE:
        raise HTTPException(status_code=413,
                            detail=f"文件过大 ({size} 字节)，最大支持 {_MAX_FILE_SIZE // 1024 // 1024}MB")

    if not _is_text_file(target):
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {target.suffix}")

    try:
        # 尝试 UTF-8 编码，失败则尝试其他常见编码
        content = target.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        try:
            content = target.read_text(encoding='gbk')
        except UnicodeDecodeError:
            try:
                content = target.read_text(encoding='latin-1')
            except Exception:
                raise HTTPException(status_code=400, detail="无法解码文件内容")

    return {
        "path": str(target),
        "name": target.name,
        "content": content,
        "size": size,
        "lines": content.count('\n') + 1,
    }
