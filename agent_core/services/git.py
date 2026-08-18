"""Git subprocess 共享封装。

统一三套平行实现（P0-2）：
- api/routes/files.py 的 ``_run_git``（2 元组）与 ``_git_rc``（3 元组）
- tools/git_tools.py 的 ``_run_git``（LLM 白名单版）

本模块只做「执行 git 命令 / 解析 porcelain 输出」这一件事，不做业务判断：
- HTTP 端点（files.py）把异常转 HTTPException，把 ``run_git`` 包成薄适配层
- LLM 工具（git_tools.py）保留白名单校验 + 文本格式化，内部复用 ``run_git``
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Union


def run_git(repo: Union[str, Path], *args: str, timeout: int = 20, extra_env: Union[dict, None] = None) -> tuple[str, str, int]:
    """在指定仓库执行 git 命令，返回 (stdout, stderr, returncode)。

    - 用 ``git -C <repo>`` 定位仓库（等价 cd，但参数形式更稳健）
    - ``LC_ALL=C`` 保证输出语言稳定（前端解析依赖英文状态码）
    - ``GIT_PAGER=cat`` / ``GIT_EXTERNAL_DIFF=`` 防止 pager 卡住与 ext-diff
      执行外部程序（对齐原 git_tools 的安全设置）
    - extra_env: 调用方可附加/覆盖环境变量（git_tools 用它保留 PATH 白名单，
      防止 git 间接执行环境里被篡改的可执行文件——安全边界，不能丢）
    - FileNotFoundError / TimeoutExpired 原样抛出，由调用方按自身语义处理
      （HTTP 端点转 500/504，LLM 工具格式化为错误文本）
    """
    env = {**os.environ, "LC_ALL": "C", "GIT_PAGER": "cat"}
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        return r.stdout.rstrip(), r.stderr.rstrip(), r.returncode
    except FileNotFoundError:
        raise RuntimeError("未找到 git 命令，请确认系统已安装 git")
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Git 命令超时（{timeout}s）")


def parse_porcelain(status_out: str) -> list[dict]:
    """解析 ``git status --porcelain=v1`` 输出为变更列表。

    返回结构与 /files/changes 的 changes 数组一致，供各接口复用。
    （从 api/routes/files.py 平移，作为共享原语）
    """
    changes = []
    for line in (status_out or "").splitlines():
        if len(line) < 4:
            continue
        # porcelain v1 固定格式为 "XY PATH"：XY 是 2 个字符（空格也是有效占位），
        # 第 3 个字符起是路径。不能用 split(None, 1) 解析——它会吃掉 X 列前导空格，
        # 把未暂存的 " M file" 误判为已暂存的 "M  file"（index_status 失真）。
        xy = line[:2].ljust(2)   # 保证长度为 2（单字符状态补空格）
        path_raw = line[3:]      # 跳过 "XY " 三字符
        # 处理 rename 格式：old -> new
        if "\x00" in path_raw:
            parts_path = path_raw.split("\x00")
            old_path = parts_path[0]
            new_path = parts_path[1] if len(parts_path) > 1 else old_path
        elif " -> " in path_raw:
            old_path, new_path = (p.strip() for p in path_raw.split(" -> ", 1))
        else:
            old_path = new_path = path_raw

        status_map = {
            "M": "modified", "A": "added", "D": "deleted",
            "R": "renamed", "C": "copied", "U": "unmerged",
            "?": "untracked", "!": "ignored",
        }
        x_status = status_map.get(xy[0], "unknown") if xy[0].strip() else ""
        y_status = status_map.get(xy[1], "unknown") if xy[1].strip() else ""

        entry = {
            "path": new_path,
            "status": x_status or y_status,
            "index_status": x_status,
            "work_status": y_status,
            "raw_xy": xy,
        }
        if x_status == "renamed" or y_status == "renamed":
            entry["old_path"] = old_path
        changes.append(entry)
    return changes
