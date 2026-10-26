"""Build the distributable folder and zip.

    .venv\\Scripts\\python packaging\\build.py [--no-zip]

Steps: icon -> PyInstaller -> copy tools (Java + veraPDF) and docs -> self-test the BUILT program -> zip.
Output: build\\dist\\PDF Accessibility Studio\\   and   build\\PDF-Accessibility-Studio-<version>-win64.zip
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
NAME = "PDF Accessibility Studio"


def run(cmd: list[str], **kw) -> None:
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def rmtree(path: Path) -> None:
    """Delete a folder even if it holds read-only files (Java's class archive is one)."""
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if path.exists():
        shutil.rmtree(path, onerror=onerror)
    if path.exists():
        raise SystemExit(f"could not delete {path}: is a program from the last build still running?")


def size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()
    t0 = time.time()

    sys.path.insert(0, str(ROOT / "src"))
    from pdfa11y import __version__

    print("1/5 icon")
    run([sys.executable, ROOT / "packaging" / "make_icon.py"])

    print("2/5 PyInstaller")
    rmtree(BUILD)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", BUILD / "dist", "--workpath", BUILD / "work", ROOT / "packaging" / "pdfa11y.spec"], cwd=ROOT)
    dist = BUILD / "dist" / NAME
    if not (dist / f"{NAME}.exe").exists():
        print("build failed: exe not found")
        return 1

    print("3/5 tools + documents")
    for sub in ("jre", "verapdf"):
        src = ROOT / "tools" / sub
        if not src.exists():
            print(f"missing {src}")
            return 1
        shutil.copytree(src, dist / "tools" / sub)
    for f in ("README-dist.txt", "THIRD-PARTY-NOTICES.txt"):
        shutil.copyfile(ROOT / "packaging" / f, dist / f.replace("-dist", ""))

    print("4/5 self-test of the built program")
    r = subprocess.run([str(dist / "pdfa11y.exe"), "selftest"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        return 1

    print(f"5/5 zip  (folder size {size_mb(dist):.0f} MB)")
    if not args.no_zip:
        z = BUILD / f"PDF-Accessibility-Studio-{__version__}-win64.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for f in dist.rglob("*"):
                if f.is_file():
                    zf.write(f, Path(NAME) / f.relative_to(dist))
        print(f"  wrote {z}  ({z.stat().st_size / 1e6:.0f} MB)")
    print(f"done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
