"""Assemble the PyInstaller one-folder build into the backend bundle folder.

Used by build.cmd as the input for electron-builder. The Windows desktop
deliverable is the Electron installer, not a standalone extracted folder, so
this step only assembles the self-contained backend directory.

Copies the built bundle with shutil.copytree (faithful, low memory - unlike
xcopy which corrupts trees with trailing backslashes and chokes on large
dirs), then bundles the Playwright Chromium binaries into _internal/.

Usage:
    package_dist.py <src_dir> <dest_dir> <root_dir>
"""
import shutil
import sys
from pathlib import Path


def main():
    src = Path(sys.argv[1])
    dest = Path(sys.argv[2])
    root = Path(sys.argv[3]) if len(sys.argv) > 3 else src.parent.parent

    # The standalone extracted folder is no longer a distributed deliverable,
    # so no extra files (Start Desktop Agent.bat / README) are injected.
    extras = []

    if not src.is_dir():
        print(f"Error: build output not found: {src}")
        sys.exit(1)

    # clean target so copytree starts fresh (avoids stale/duplicate trees)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(str(src), str(dest))

    for s, d in extras:
        if s.exists():
            shutil.copy2(str(s), str(d))
        else:
            print(f"Warning: extra file not found, skipped: {s}")

    # ponytail: 浏览器二进制不走 PyInstaller（见 DesktopAgent.spec 注释，逐条 datas 会让
    # COLLECT 把 exe 误建成目录）。这里在 COLLECT 之后忠实拷贝进 <pkg>/_internal/ms-playwright，
    # 运行时 main.py 用 PLAYWRIGHT_BROWSERS_PATH 指向它。先 rm 再 copytree，避免旧构建残留嵌套。
    browsers_src = root / ".playwright-browsers"
    browsers_dst = dest / "_internal" / "ms-playwright"
    if browsers_src.is_dir():
        if browsers_dst.exists():
            shutil.rmtree(str(browsers_dst))
        shutil.copytree(str(browsers_src), str(browsers_dst))
        print(f"Browsers bundled: {browsers_dst}")
    else:
        print("Warning: .playwright-browsers not found - browser tool will not work in package.")

    print(f"Backend bundle assembled: {dest}")
    print("This folder is consumed by electron-builder (packaging/windows/build-electron.cmd).")


if __name__ == "__main__":
    main()
