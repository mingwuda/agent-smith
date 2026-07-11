"""Package the PyInstaller one-folder build into a distributable folder + zip.

Used by build.cmd. Copies the built bundle with shutil.copytree (faithful,
low memory - unlike xcopy which corrupts trees with trailing backslashes and
chokes on large dirs), then streams a zip with zipfile (no RAM buffering,
unlike PowerShell Compress-Archive which fails with "Insufficient memory" on
large packages).

Usage:
    package_dist.py <src_dir> <dest_dir> <zip_path> <root_dir>
"""
import os
import shutil
import sys
import zipfile
from pathlib import Path


def main():
    src = Path(sys.argv[1])
    dest = Path(sys.argv[2])
    zip_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    root = Path(sys.argv[4]) if len(sys.argv) > 4 else src.parent.parent

    extras = [
        (root / "packaging" / "windows" / "Start Desktop Agent.bat", dest / "Start Desktop Agent.bat"),
        (root / "packaging" / "windows" / "README-Windows.md", dest / "README-Windows.md"),
    ]

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

    if zip_path:
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            # store with the top-level folder name so unzip yields DesktopAgent-Windows/
            for root_dir, _dirs, files in os.walk(str(dest)):
                for f in files:
                    fp = os.path.join(root_dir, f)
                    arcname = os.path.relpath(fp, str(dest.parent))
                    z.write(fp, arcname)
        size_mb = os.path.getsize(str(zip_path)) // 1024 // 1024
        print(f"Zip created: {zip_path} ({size_mb} MB)")

    print(f"Packaging done: {dest}")


if __name__ == "__main__":
    main()
