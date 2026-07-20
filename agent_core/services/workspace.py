"""工作区 / 文件 / 制品辅助函数。提取自 main.py"""
import base64
import hashlib
import json
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from fastapi import Request
from urllib.parse import quote

import user_manager
from session_store import add_message, get_session, create_session, rename_session, get_session_workspace


# ── 从 main.py 导出的函数 ──


def _workspace_for_user(uid: str) -> Path:
    # 微信 Bot 用户默认使用全局工作区，不按 uid 分子目录
    if uid.startswith("wechat_"):
        return user_manager.WORKSPACE_BASE
    return Path(user_manager.user_workspace(uid)).expanduser().resolve()


def _resolve_artifact_path(uid: str, path: str) -> Path:
    workspace = _workspace_for_user(uid)
    try:
        raw = Path(path or "").expanduser()
    except (RuntimeError, OSError):
        # expanduser 可能因 HOME 未设置而失败，此时按绝对路径处理
        raw = Path(path or "")
    target = raw if raw.is_absolute() else workspace / raw
    target = target.resolve(strict=False)
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise HTTPException(403, "只能下载当前用户工作区内的文件") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return target


def _relative_artifact_path(uid: str, path: str) -> str:
    target = _resolve_artifact_path(uid, path)
    return target.relative_to(_workspace_for_user(uid)).as_posix()


def _artifact_link(path: str) -> str:
    name = Path(path).name or path
    encoded = quote(path)
    download = f"[下载](/artifacts/download?path={encoded})"
    if Path(path).suffix.lower() in {".md", ".markdown"}:
        preview = f"[预览](#artifact-preview:{encoded})"
        return f"- {name}: {preview} / {download} (`{path}`)"
    return f"- {name}: {download} (`{path}`)"


def _append_artifact_links(content: str, uid: str, paths: Optional[list[str]] = None) -> str:
    cleaned_content = _strip_existing_artifact_section(content)
    found: list[str] = []
    seen: set[str] = set()
    # ponytail: 只信任来自工具调用的显式路径（write_file/append_to_file），
    # 不再从回复文本里扫描文件名——文本里出现的 README.md、ARCHITECTURE.md
    # 等只是被引用/阅读过的文件，误当成制品会误导用户下载错文件。
    for candidate in (paths or []):
        try:
            rel = _relative_artifact_path(uid, candidate)
        except Exception:
            continue
        if rel not in seen:
            seen.add(rel)
            found.append(rel)
    if not found:
        return content
    links = "\n".join(_artifact_link(path) for path in found)
    return f"{cleaned_content.rstrip()}\n\n---\n\n可下载文件：\n{links}"


def _strip_existing_artifact_section(content: str) -> str:
    """Remove model-generated download sections so the normalized one appears once."""
    lines = (content or "").splitlines()
    result: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if re.fullmatch(r"-{3,}", line):
            next_idx = idx + 1
            while next_idx < len(lines) and not lines[next_idx].strip():
                next_idx += 1
            next_line = lines[next_idx].strip() if next_idx < len(lines) else ""
            if re.match(r"^可下载文件[:：]?$", next_line):
                idx = next_idx + 1
                while idx < len(lines):
                    current = lines[idx].strip()
                    if re.fullmatch(r"-{3,}", current):
                        break
                    if current and not current.startswith(("-", "*")) and not re.match(r"^可下载文件[:：]?$", current):
                        break
                    idx += 1
                continue
        if re.match(r"^可下载文件[:：]?$", line):
            idx += 1
            while idx < len(lines):
                current = lines[idx].strip()
                if current and not current.startswith(("-", "*")):
                    break
                idx += 1
            continue
        result.append(lines[idx])
        idx += 1
    return "\n".join(result).rstrip()


def _extract_zip(raw: bytes, zip_name: str) -> tuple[str, str]:
    """解压 ZIP 字节到工作区 .agent_zip/{name}/ 目录，返回 (目录绝对路径, 文件清单文本)"""
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', Path(zip_name).stem)[:32]
    dest = Path.home() / "agent_workspace" / ".agent_zip" / f"{safe_name}_{hashlib.md5(raw[:1024]).hexdigest()[:8]}"
    dest.mkdir(parents=True, exist_ok=True)

    tree: list[str] = []
    try:
        with zipfile.ZipFile(BytesIO(raw)) as zf:
            for info in zf.infolist():
                fname = info.filename
                if fname.startswith("/") or ".." in fname:
                    continue  # 路径穿越防护
                target = dest / fname
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    tree.append(f"[DIR]  {fname}")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    zf.extract(info, dest)
                    size = info.file_size
                    size_str = f"{size/1024:.1f}KB" if size < 1024*1024 else f"{size/1024/1024:.1f}MB"
                    tree.append(f"[FILE] {fname}  ({size_str})")
        # 限制文件列表长度
        if len(tree) > 500:
            tree = tree[:500] + [f"...（共 {len(tree)} 个文件，仅展示前 500 个）"]
    except zipfile.BadZipFile:
        return "", "❌ ZIP 文件损坏，无法解压。"
    except Exception as e:
        return "", f"❌ 解压失败: {type(e).__name__}: {e}"

    manifest = (
        f"用户上传了 ZIP 文件「{zip_name}」，已解压到工作区 .agent_zip/ 目录。\n"
        f"解压路径: {dest}\n\n"
        f"文件清单（共 {len(tree)} 个条目）:\n" + "\n".join(tree)
    )
    return str(dest), manifest


def _display_user_message(uid: str, message: str, attachments: list[dict]) -> str:
    """生成用户消息的存储文本。有图片/压缩包/文本文件时保存到磁盘，返回摘要信息。"""
    if not attachments:
        return message

    # ── 先处理 ZIP ──
    zip_notes = ""
    for item in attachments:
        if item.get("mime_type") == "application/zip" and "raw" in item:
            name = item.get("name", "project.zip")
            raw = item.pop("raw")
            dest, manifest = _extract_zip(raw, name)
            if dest:
                zip_notes = manifest
            break

    if zip_notes:
        return json.dumps({"text": message or "请分析这个项目。", "zip_manifest": zip_notes}, ensure_ascii=False)

    # ── 文本文件处理 ──
    text_files: list[dict] = []
    for item in attachments:
        if "content" in item:
            text_files.append({
                "name": item.get("name", "file.txt"),
                "content": item.pop("content"),
            })
    if text_files:
        parts = [message or ""]
        for tf in text_files:
            parts.append(f"\n── 文件: {tf['name']} ──\n{tf['content']}")
        combined = "\n".join(parts).strip()
        return json.dumps({"text": combined or "请分析这些文件。", "text_files": [tf["name"] for tf in text_files]}, ensure_ascii=False)

    # ── 图片处理 ──
    image_paths: list[str] = []
    img_dir = Path.home() / ".desktop_agent" / "user_images" / uid
    img_dir.mkdir(parents=True, exist_ok=True)
    for item in attachments:
        data_url = item.get("data_url", "")
        mime_type = item.get("mime_type", "image/png")
        prefix = f"data:{mime_type};base64,"
        if not data_url.startswith(prefix):
            continue
        try:
            raw = base64.b64decode(data_url[len(prefix):])
        except Exception:
            continue
        ext = mime_type.split("/")[-1] or "png"
        h = hashlib.sha1(raw).hexdigest()[:16]
        fname = f"{item.get('name', 'img')}_{h}.{ext}"
        fpath = img_dir / fname
        fpath.write_bytes(raw)
        image_paths.append(str(fpath))
    if not image_paths:
        return message or "请分析这些图片。"
    payload = {
        "text": message or "请分析这些图片。",
        "images": image_paths,
    }
    return json.dumps(payload, ensure_ascii=False)


def _safe_attachments(attachments: list[Any]) -> list[dict]:
    safe: list[dict] = []
    TEXT_EXTS = {".md", ".txt", ".json", ".yaml", ".yml", ".xml", ".html", ".css",
                 ".js", ".ts", ".jsx", ".tsx", ".py", ".java", ".c", ".cpp", ".h",
                 ".hpp", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".sql",
                 ".cfg", ".ini", ".conf", ".toml", ".env", ".gitignore", ".dockerfile",
                 ".log", ".csv", ".svg", ".vue", ".svelte", ".kt", ".swift", ".scala"}
    for item in attachments[:4]:
        mime = (item.mime_type or "").strip().lower()
        ext = Path(item.name or "").suffix.lower()
        data_url = (item.data_url or "").strip()
        is_zip = mime in ("application/zip", "application/x-zip-compressed") or ext == ".zip"
        is_text = mime.startswith("text/") or ext in TEXT_EXTS
        prefix = "data:application/zip;base64," if is_zip else f"data:{mime};base64,"

        if not data_url.startswith(prefix):
            if not is_zip:
                if not is_text:
                    continue
                # 文本文件：宽松检测 base64
                if not data_url.startswith("data:") or ";base64," not in data_url:
                    continue
                base64_data = data_url.split(";base64,", 1)[-1]
            else:
                # ZIP 宽松检测
                if not data_url.startswith("data:") or ";base64," not in data_url:
                    continue
                base64_data = data_url.split(";base64,", 1)[-1]
        else:
            base64_data = data_url[len(prefix):]
        try:
            raw = base64.b64decode(base64_data)
        except Exception:
            continue

        if is_zip:
            if len(raw) > 50 * 1024 * 1024:
                continue
            safe.append({
                "name": item.name or "project.zip",
                "mime_type": "application/zip",
                "data_url": data_url,
                "raw": raw,
            })
        elif is_text:
            if len(raw) > 1 * 1024 * 1024:  # 文本上限 1MB
                continue
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    decoded = raw.decode("gbk")
                except UnicodeDecodeError:
                    decoded = raw.decode("utf-8", errors="replace")
            safe.append({
                "name": item.name or "file.txt",
                "mime_type": mime,
                "data_url": data_url,
                "content": decoded,
            })
        else:
            if not mime.startswith("image/") or len(data_url) > 8 * 1024 * 1024:
                continue
            safe.append({
                "name": item.name or "image.png",
                "mime_type": mime,
                "data_url": data_url,
            })
    return safe


def _user_image_urls(uid: str, image_paths: list[str], request: Request) -> list[str]:
    """将本地图片路径转换为可下载的 HTTP URL，供 LLM 工具使用。"""
    base_url = str(request.base_url).rstrip("/")
    urls: list[str] = []
    for fpath in image_paths:
        name = Path(fpath).name
        url = f"{base_url}/user-images/download?name={quote(name)}&uid={quote(uid)}"
        urls.append(url)
    return urls
