# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).parents[1]

datas = [
    (str(ROOT / "desktop"), "desktop"),
    (str(ROOT / "agent_core" / "samples"), "agent_core/samples"),
    # ponytail: 运行时实际读取的资源，漏打包会导致功能缺失
    (str(ROOT / "skills"), "skills"),  # main.py 始终从 app_base/"skills" 加载技能
    (str(ROOT / "AGENTS.md"), "."),    # agent.py 读取项目指引
    (str(ROOT / "agent_core" / "dbcli" / "permissions.yaml"), "agent_core/dbcli"),  # dbcli 直接读取
]

# ponytail: Playwright 的 driver（node.exe + JS 包）不被 PyInstaller 自动收集，
# 缺了它 chromium.launch() 在打包环境里找不到 node 驱动而失败。连同浏览器二进制一起打进包。
import playwright as _pw_pkg

_pw_dir = Path(_pw_pkg.__path__[0])          # .../site-packages/playwright
_driver_src = _pw_dir / "driver"
for _root, _dirs, _files in os.walk(str(_driver_src)):
    for _f in _files:
        _src = os.path.join(_root, _f)
        _rel = os.path.relpath(_src, str(_pw_dir))
        datas.append((_src, os.path.dirname(_rel).replace(os.sep, "/")))

# ponytail: 浏览器二进制（~400-690M）不走 PyInstaller 收集——若按文件逐条塞进 datas，
# COLLECT（upx=True）会把 chrome-headless-shell.exe 之类误建为目录节点（file 变成 dir/file 嵌套），
# 运行时 Playwright 找不到 chrome.exe 而 ENOENT。改为由 build.cmd 装到 .playwright-browsers，
# 再由 packaging/windows/package_dist.py 在 COLLECT 之后用 shutil.copytree 忠实拷进
# <package>/_internal/ms-playwright，运行时由 main.py 用 PLAYWRIGHT_BROWSERS_PATH 指回。
_browsers_src = ROOT / ".playwright-browsers"
if not _browsers_src.exists():
    print("WARNING: .playwright-browsers not found - run build.cmd first. "
          "Browser tool will not work in the package.")

hiddenimports = []
for package in (
    "anyio",
    "fastapi",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "playwright",
    "pydantic",
    "pydantic_core",
    "sniffio",
    "starlette",
    "typing_extensions",
    "uvicorn",
):
    hiddenimports += collect_submodules(package)

a = Analysis(
    [str(ROOT / "agent_core" / "main.py")],
    pathex=[str(ROOT / "agent_core"), str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DesktopAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DesktopAgent",
)
