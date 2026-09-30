#!/usr/bin/env python3
"""
ProofMark release helper (run from the project folder, no dependencies).

  ./release.py status                      show version, build, channel
  ./release.py bump patch "Fixed X" "Added Y"   bump version (major|minor|patch), build += 1, add changelog lines
  ./release.py build                       build dist/: proofmark.py, .sha256, source tarball (for RPM / Flatpak)
  ./release.py icon                        export dist/proofmark-512.png (needs PySide6)

Typical release:
  ./release.py bump minor "New thing"      # edits proofmark.py, packaging/*.metainfo.xml, *.spec
  git add -A && git commit -m "Release v1.2.0" && git tag v1.2.0 && git push --tags
  ./release.py build                       # upload dist/proofmark.py + proofmark.py.sha256 to the GitHub release
The in-app updater looks for those two assets on the latest GitHub release.
"""
from __future__ import annotations

import datetime
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "proofmark.py"
PKG = ROOT / "packaging"
DIST = ROOT / "dist"
METAINFO = PKG / "io.github.proofmark.ProofMark.metainfo.xml"
SPEC = PKG / "proofmark.spec"
MARKER = "    # <changelog-insert>\n"


def read_version() -> tuple[str, int]:
    src = APP.read_text(encoding="utf-8")
    v = re.search(r'^APP_VERSION = "([^"]+)"', src, re.M)
    b = re.search(r"^BUILD_NUMBER = (\d+)", src, re.M)
    if not v or not b:
        sys.exit("Could not find APP_VERSION / BUILD_NUMBER in proofmark.py")
    return v.group(1), int(b.group(1))


def bump_version(v: str, part: str) -> str:
    major, minor, patch = (int(x) for x in v.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    sys.exit("bump part must be major, minor or patch")


def sub_once(text: str, pattern: str, repl: str, what: str) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"Could not update {what}")
    return new


def cmd_status() -> None:
    v, b = read_version()
    print(f"ProofMark {v} (build {b})")


def cmd_bump(part: str, notes: list[str]) -> None:
    old, build = read_version()
    new = bump_version(old, part)
    today = datetime.date.today().isoformat()
    src = APP.read_text(encoding="utf-8")
    src = sub_once(src, r'^APP_VERSION = "[^"]+"', f'APP_VERSION = "{new}"', "APP_VERSION")
    src = sub_once(src, r"^BUILD_NUMBER = \d+", f"BUILD_NUMBER = {build + 1}", "BUILD_NUMBER")
    src = sub_once(src, r'^BUILD_DATE = "[^"]+"', f'BUILD_DATE = "{today}"', "BUILD_DATE")
    if MARKER not in src:
        sys.exit("Changelog marker missing in proofmark.py")
    lines = notes or ["Maintenance release"]
    entry = f'    ("{new}", "{today}", [\n' + ",\n".join(
        f"        {l!r}" if "'" not in l else f'        "{l}"' for l in lines) + "]),\n"
    src = src.replace(MARKER, MARKER + entry, 1)
    compile(src, "proofmark.py", "exec")
    APP.write_text(src, encoding="utf-8")

    if SPEC.exists():
        s = SPEC.read_text(encoding="utf-8")
        s = sub_once(s, r"^Version:\s+.*$", f"Version:        {new}", "spec Version")
        SPEC.write_text(s, encoding="utf-8")
    if METAINFO.exists():
        m = METAINFO.read_text(encoding="utf-8")
        items = "".join(f"<li>{l.replace('&', '&amp;').replace('<', '&lt;')}</li>" for l in lines)
        rel = (f'    <release version="{new}" date="{today}">\n      <description><ul>{items}</ul></description>\n'
               f"    </release>\n")
        m = m.replace("<releases>\n", "<releases>\n" + rel, 1)
        METAINFO.write_text(m, encoding="utf-8")
    print(f"{old} -> {new}   build {build} -> {build + 1}")
    print(f"Next: git commit, git tag v{new}, then ./release.py build")


def cmd_build() -> None:
    v, b = read_version()
    DIST.mkdir(exist_ok=True)
    shutil.copy2(APP, DIST / "proofmark.py")
    digest = hashlib.sha256(APP.read_bytes()).hexdigest()
    (DIST / "proofmark.py.sha256").write_text(f"{digest}  proofmark.py\n", encoding="utf-8")
    tar = DIST / f"proofmark-{v}.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        for name in ("proofmark.py", "README.md", "LICENSE", "release.py"):
            if (ROOT / name).exists():
                tf.add(ROOT / name, arcname=f"proofmark-{v}/{name}")
        if PKG.exists():
            tf.add(PKG, arcname=f"proofmark-{v}/packaging")
    print(f"Built {v} (build {b}) in {DIST}/")
    print(f"  proofmark.py, proofmark.py.sha256  -> attach to the GitHub release (enables in-app update)")
    print(f"  {tar.name}  -> RPM / Flatpak source   sha256 {hashlib.sha256(tar.read_bytes()).hexdigest()}")


def cmd_icon() -> None:
    DIST.mkdir(exist_ok=True)
    subprocess.run([sys.executable, str(APP), "--export-icon", str(DIST / "proofmark-512.png")], check=True)


def main() -> None:
    a = sys.argv[1:]
    if not a or a[0] == "status":
        cmd_status()
    elif a[0] == "bump" and len(a) >= 2:
        cmd_bump(a[1], a[2:])
    elif a[0] == "build":
        cmd_build()
    elif a[0] == "icon":
        cmd_icon()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
