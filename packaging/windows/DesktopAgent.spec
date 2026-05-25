# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).parents[1]

datas = [
    (str(ROOT / "desktop"), "desktop"),
    (str(ROOT / "agent_core" / "samples"), "agent_core/samples"),
]

hiddenimports = []
for package in (
    "anyio",
    "fastapi",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
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
