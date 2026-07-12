"""
增量更新模块。

服务端 manifest 格式（version.json）：
{
  "latest": "0.1.2",
  "changelog": "修复了...",
  "versions": {
    "0.1.0": {
      "full": {"url": "...", "size": 500000000, "sha256": "..."}
    },
    "0.1.1": {
      "from": "0.1.0",
      "to": "0.1.1",
      "patches": [
        {"url": "...", "size": 5000000, "sha256": "..."}
      ]
    },
    "0.1.2": {
      "from": "0.1.1",
      "to": "0.1.2",
      "patches": [
        {"url": "...", "size": 3000000, "sha256": "..."}
      ]
    }
  }
}

补丁包格式（zip）：
  manifest.json: {"from": "0.1.0", "to": "0.1.1", "added": [...], "modified": [...], "deleted": [...]}
  files/...: 实际文件内容（相对路径）

ponytail: 增量更新只改文件内容，不碰二进制/被占用文件；遇到占用会跳过并标记需重启。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

from version import __version__

# 默认更新源（Gitee Releases 页面，用于解析 latest tag）
DEFAULT_UPDATE_SERVER = (
    "https://gitee.com/mingwuda/desktop-agent/releases/latest"
)


class UpdateError(Exception):
    """更新过程中的可预期错误（不抛给上层，只记日志）。"""


def _current_backend_dir() -> Path:
    """返回后端代码目录（补丁包内的相对路径即相对于此目录）。

    开发模式与冻结模式下 ``__file__`` 都指向实际运行的 ``agent_core/updater.py``
    所在目录，因此统一取其所在目录（即 ``agent_core/``）。

    注意（冻结模式）：PyInstaller one-folder 每次启动会重新从可执行文件中解包，
    因此直接修改解包目录里的文件在下次启动会被覆盖——冻结模式下只有**全量更新**
    （由启动器替换整个产物）才能可靠生效；增量补丁在冻结模式下可能不持久，
    仅建议在源码/开发模式下使用增量更新。
    """
    return Path(__file__).parent


def _platform_tag() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "macos-arm64"
        return "macos-x64"
    return ""


# ---------- 版本号处理 ----------

def _norm_ver(v: str) -> str:
    """归一化版本号：小写并去掉前导 'v'（如 'V0.1.2' -> '0.1.2'）。"""
    if not v:
        return ""
    return v.strip().lower().lstrip("v")


def _ver_key(v: str) -> tuple:
    """将版本号转为可比元组（'0.1.12' -> (0, 1, 12)），非数字段原样保留。"""
    parts = _norm_ver(v).split(".")
    out = []
    for p in parts:
        out.append(int(p) if p.isdigit() else p)
    return tuple(out)


# ---------- 网络请求 ----------

def _get(url: str, timeout: int = 30) -> requests.Response:
    """统一 GET 请求。"""
    return requests.get(url, timeout=timeout, allow_redirects=True)


def _download(url: str, dest: Path, expected_sha256: str = "") -> None:
    """流式下载到 dest，可选校验 sha256。"""
    with _get(url, timeout=120) as r:
        r.raise_for_status()
        h = hashlib.sha256()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    h.update(chunk)
                    f.write(chunk)
    if expected_sha256 and h.hexdigest() != expected_sha256:
        raise UpdateError(f"sha256 校验失败: {h.hexdigest()} != {expected_sha256}")


# ---------- Manifest 解析 ----------

def _parse_gitee_manifest(url: str) -> dict[str, Any] | None:
    """从 Gitee Releases 的 latest 页面解析出 manifest。
    
    如果仓库 releases 的 assets 中有 version.json，直接读取；
    否则回退到旧版全量包逻辑。
    """
    try:
        resp = _get(url)
        if resp.status_code != 200:
            return None
        tag = resp.url.rstrip("/").split("/")[-1]
        if not tag:
            return None
        api_url = (
            "https://gitee.com/api/v5/repos/mingwuda/desktop-agent"
            f"/releases/{tag}"
        )
        r = _get(api_url)
        if r.status_code != 200:
            return None
        data = r.json()
        # 找 version.json asset
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if name == "version.json":
                manifest_url = asset.get("browser_download_url", "")
                if manifest_url:
                    mr = _get(manifest_url)
                    if mr.status_code == 200:
                        return mr.json()
        # 没有 version.json，回退到旧版逻辑
        return None
    except Exception:
        return None


def _parse_gitee_latest_legacy(url: str) -> dict[str, Any] | None:
    """旧版逻辑：从 Gitee Releases 解析 latest release 的 tag 与 assets。"""
    try:
        resp = _get(url)
        if resp.status_code != 200:
            return None
        tag = resp.url.rstrip("/").split("/")[-1]
        if not tag:
            return None
        api_url = (
            "https://gitee.com/api/v5/repos/mingwuda/desktop-agent"
            f"/releases/{tag}"
        )
        r = _get(api_url)
        if r.status_code != 200:
            return None
        data = r.json()
        assets = [
            {
                "name": a.get("name", ""),
                "url": a.get("browser_download_url", ""),
            }
            for a in data.get("assets", [])
        ]
        return {"tag": tag, "assets": assets}
    except Exception:
        return None


def _find_backend_zip(assets: list[dict[str, str]]) -> dict[str, str] | None:
    """从 assets 中挑选当前平台对应的后端 zip（旧版逻辑）。"""
    tag = _platform_tag()
    if not tag:
        return None
    keywords = {
        "windows": ["windows", "win"],
        "macos-arm64": ["macos", "arm64", "darwin"],
        "macos-x64": ["macos", "x64", "intel", "darwin"],
    }.get(tag, [])
    for asset in assets:
        name = asset.get("name", "").lower()
        if any(k in name for k in keywords) and name.endswith(".zip"):
            return asset
    return None


def _find_latest_full_version(versions: dict[str, Any]) -> str | None:
    """找版本号最大的有 full 包的版本（按数值比较，避免 '0.1.9' > '0.1.12'）。"""
    full_versions = [ver for ver, info in versions.items() if "full" in info]
    if not full_versions:
        return None
    full_versions.sort(key=_ver_key, reverse=True)
    return full_versions[0]


def _resolve_chain(
    manifest: dict[str, Any],
    current_version: str,
    latest_version: str,
) -> list[dict[str, Any]]:
    """解析从当前版本到最新版本的更新链。

    返回 list of {"type": "patch"|"full", "from": ..., "to": ..., "url": ..., "size": ..., "sha256": ...}
    """
    versions = manifest.get("versions", {})
    norm_map = {_norm_ver(k): k for k in versions}
    chain: list[dict[str, Any]] = []
    v = _norm_ver(current_version)
    latest = _norm_ver(latest_version)
    visited: set[str] = set()
    guard = 0
    max_iter = len(versions) + 5

    while v != latest:
        # 防环 / 畸形 manifest 导致死循环
        if v in visited or guard > max_iter:
            raise UpdateError(
                f"更新链解析异常（可能存在环或畸形 manifest），当前版本={current_version}"
            )
        visited.add(v)
        guard += 1

        # 找 from=v 的下一个版本
        next_ver = None
        patches = None
        for ver, info in versions.items():
            if _norm_ver(info.get("from", "")) == v and info.get("patches"):
                next_ver = ver
                patches = info["patches"]
                break

        if patches:
            for patch in patches:
                chain.append({
                    "type": "patch",
                    "from": v,
                    "to": next_ver,
                    "url": patch["url"],
                    "size": patch.get("size", 0),
                    "sha256": patch.get("sha256", ""),
                })
            v = _norm_ver(next_ver)
            continue

        # 没有从 v 出发的补丁
        if v not in norm_map:
            # 当前版本未知，找版本号最大的全量包
            full_ver = _find_latest_full_version(versions)
            if full_ver is None:
                raise UpdateError(f"无法找到版本 {current_version} 的更新信息")
            finfo = versions[full_ver]
            chain.append({
                "type": "full",
                "version": full_ver,
                "url": finfo["full"]["url"],
                "size": finfo["full"].get("size", 0),
                "sha256": finfo["full"].get("sha256", ""),
            })
            v = _norm_ver(full_ver)
            continue

        # v 在 manifest 中，但没有 from=v 的补丁
        ver_info = versions[norm_map[v]]
        if "full" in ver_info:
            chain.append({
                "type": "full",
                "version": norm_map[v],
                "url": ver_info["full"]["url"],
                "size": ver_info["full"].get("size", 0),
                "sha256": ver_info["full"].get("sha256", ""),
            })
            # 全量包后，如果 v != latest，无法继续
            if v != latest:
                raise UpdateError(
                    f"版本 {current_version} 只有全量包，没有补丁链到 {latest_version}"
                )
            break
        else:
            raise UpdateError(f"版本 {current_version} 没有可用的更新包")

    return chain


# ---------- 补丁应用 ----------

def _apply_patch(patch_zip: Path, target_dir: Path) -> None:
    """应用单个补丁包到目标目录（target_dir 应为已准备好的副本/待生效目录）。

    补丁包内容：
      manifest.json: {"from": "...", "to": "...", "added": [...], "modified": [...], "deleted": [...]}
      files/...: 实际文件

    新增/修改的文件直接覆盖到 target_dir；标记为删除的文件尝试删除，
    被占用时仅告警（交由下次启动的待生效切换处理），不中断整个更新。
    """
    if not patch_zip.exists() or not zipfile.is_zipfile(patch_zip):
        raise UpdateError(f"补丁包无效: {patch_zip}")

    with tempfile.TemporaryDirectory(prefix="desktop-agent-patch-") as tmp:
        with zipfile.ZipFile(patch_zip, "r") as zf:
            zf.extractall(tmp)

        manifest_path = Path(tmp) / "manifest.json"
        if not manifest_path.exists():
            raise UpdateError("补丁包缺少 manifest.json")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files_dir = Path(tmp) / "files"
        deleted = set(manifest.get("deleted", []))
        added_modified = manifest.get("added", []) + manifest.get("modified", [])

        # 复制新增和修改的文件
        for rel_path in added_modified:
            src = files_dir / rel_path
            if not src.exists():
                continue
            dest = target_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

        # 删除标记为删除的文件
        for rel_path in deleted:
            dest = target_dir / rel_path
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    logger.warning("[更新] 删除 %s 失败（可能被占用），将在重启后生效", dest)


def _extract_zip_to(zip_path: Path, target_dir: Path) -> None:
    """解压 zip 到 target_dir（合并拷贝，处理单层顶级目录包裹）。"""
    if not zip_path.exists() or not zipfile.is_zipfile(zip_path):
        raise UpdateError(f"压缩包无效: {zip_path}")

    with tempfile.TemporaryDirectory(prefix="desktop-agent-x-") as tmp:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp)
        entries = list(Path(tmp).iterdir())
        src = entries[0] if (len(entries) == 1 and entries[0].is_dir()) else Path(tmp)
        if not src.exists():
            raise UpdateError("压缩包内容为空")
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            dest = target_dir / rel
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)


# ---------- 公开 API ----------

def check_update(update_server: str = "") -> dict[str, Any]:
    """检查是否有可用更新。
    
    返回示例：
    {
        "current_version": "0.1.0",
        "latest_version": "0.1.2",
        "has_update": true,
        "changelog": "...",
        "update_type": "incremental",  // 或 "full"
        "patches": [
            {"from": "0.1.0", "to": "0.1.1", "url": "...", "size": 5000000},
            {"from": "0.1.1", "to": "0.1.2", "url": "...", "size": 3000000}
        ],
        "full_url": "https://.../full-0.1.2.zip",  // 可选
        "full_size": 500000000,
        "error": ""
    }
    """
    server = update_server or DEFAULT_UPDATE_SERVER
    result: dict[str, Any] = {
        "current_version": __version__,
        "latest_version": __version__,
        "has_update": False,
        "changelog": "",
        "update_type": "none",
        "patches": [],
        "full_url": "",
        "full_size": 0,
        "full_sha256": "",
        "error": "",
    }

    try:
        # 尝试获取增量 manifest
        manifest = _parse_gitee_manifest(server)
        if manifest:
            latest = manifest.get("latest", __version__)
            result["latest_version"] = latest
            result["changelog"] = manifest.get("changelog", "")

            if _norm_ver(latest) == _norm_ver(__version__):
                return result

            chain = _resolve_chain(manifest, __version__, latest)
            if chain:
                result["has_update"] = True
                result["update_type"] = "incremental" if any(
                    c["type"] == "patch" for c in chain
                ) else "full"
                result["patches"] = [
                    {
                        "from": c["from"],
                        "to": c["to"],
                        "url": c["url"],
                        "size": c.get("size", 0),
                        "sha256": c.get("sha256", ""),
                    }
                    for c in chain
                    if c["type"] == "patch"
                ]
                # 找全量包（如果有）
                for c in chain:
                    if c["type"] == "full":
                        result["full_url"] = c["url"]
                        result["full_size"] = c.get("size", 0)
                        result["full_sha256"] = c.get("sha256", "")
                        break
            return result

        # 回退到旧版全量包逻辑
        release = _parse_gitee_latest_legacy(server)
        if not release:
            result["error"] = "无法获取最新版本信息"
            return result

        latest_tag = release.get("tag", "").lstrip("v")
        if not latest_tag:
            result["error"] = "版本号为空"
            return result

        result["latest_version"] = latest_tag
        if _norm_ver(latest_tag) == _norm_ver(__version__):
            return result

        asset = _find_backend_zip(release.get("assets", []))
        if not asset:
            result["error"] = f"未找到当前平台({_platform_tag()})的更新包"
            return result

        result["has_update"] = True
        result["update_type"] = "full"
        result["full_url"] = asset.get("url", "")
        result["changelog"] = release.get("tag", "")
    except Exception as exc:
        result["error"] = str(exc)

    return result


def _write_version(target_dir: Path, version: str) -> None:
    """把目标版本号写回 target_dir/version.py，使下次检查不再重复提示同一更新。"""
    try:
        (target_dir / "version.py").write_text(
            f'"""Moss Agent 后端版本号（单一真相源）。"""\n\n__version__ = "{version}"\n',
            encoding="utf-8",
        )
    except OSError:
        logger.warning("[更新] 写入版本号文件失败: %s", target_dir / "version.py")


def install_update(
    patches: list[dict[str, Any]],
    full_url: str = "",
    target_version: str = "",
    full_sha256: str = "",
) -> dict[str, Any]:
    """下载并安装更新（先构建到待生效目录，再原子切换）。

    流程：
      1. 构建待生效目录 ``<backend>.pending``：
         - 增量：复制当前后端目录为基线，逐补丁应用；
         - 全量（补丁数 > 2 或仅 full_url）：直接解包到 pending。
      2. 每个下载的包都按 manifest 中的 sha256 校验。
      3. 写入目标版本号与 .UPDATE_VERSION 标记。
      4. 原子切换：``live -> .old``，``pending -> live``。
         若切换因文件被占用（Windows 运行中的 exe）失败，保留 pending，
         返回 ``pending=True``，由下次启动时的 _apply_pending_update_at_boot 完成切换。
    返回 {"ok", "restart", "pending", "applied_patches", "error"}。
    """
    target = _current_backend_dir()
    result: dict[str, Any] = {
        "ok": False,
        "restart": False,
        "pending": False,
        "applied_patches": [],
        "error": "",
    }
    tv = target_version or (patches[-1]["to"] if patches else "")

    pending = Path(str(target) + ".pending")
    try:
        if pending.exists():
            shutil.rmtree(pending, ignore_errors=True)

        if full_url and len(patches) > 2:
            # 长链优先走全量
            pending.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="desktop-agent-dl-") as tmp:
                zip_path = Path(tmp) / "full.zip"
                _download(full_url, zip_path, full_sha256)
                _extract_zip_to(zip_path, pending)
            result["applied_patches"] = [f"full -> {tv}"]
        else:
            if not patches:
                raise UpdateError("没有可应用的更新包")
            shutil.copytree(str(target), str(pending), symlinks=False)
            for patch_info in patches:
                with tempfile.TemporaryDirectory(prefix="desktop-agent-patch-") as tmp:
                    zip_path = Path(tmp) / f"patch-{patch_info['from']}-{patch_info['to']}.zip"
                    _download(patch_info["url"], zip_path, patch_info.get("sha256", ""))
                    _apply_patch(zip_path, pending)
                result["applied_patches"].append(
                    f"{patch_info['from']} -> {patch_info['to']}"
                )

        # 写版本号 + 待生效标记
        if tv:
            _write_version(pending, tv)
        (pending / ".UPDATE_VERSION").write_text(tv or "", encoding="utf-8")
    except Exception as exc:
        if pending.exists():
            shutil.rmtree(pending, ignore_errors=True)
        result["error"] = str(exc)
        return result

    # 原子切换
    backup = Path(str(target) + ".old")
    try:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        os.rename(target, backup)
    except OSError:
        # 当前目录被占用（Windows 运行中的 exe）：保留 pending，交启动切换
        result["ok"] = True
        result["restart"] = True
        result["pending"] = True
        return result

    try:
        os.rename(pending, target)
    except OSError:
        # 极端情况：尝试恢复
        try:
            os.rename(backup, target)
        except OSError:
            pass
        result["ok"] = True
        result["restart"] = True
        result["pending"] = True
        return result

    # 切换成功
    try:
        shutil.rmtree(backup, ignore_errors=True)
    except OSError:
        pass
    result["ok"] = True
    result["restart"] = True
    return result


def check_update_async(update_server: str = "", callback=None) -> None:
    """后台线程检查更新，完成后调用 callback(result)。"""

    def _run():
        result = check_update(update_server=update_server)
        if callback:
            try:
                callback(result)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
