#!/usr/bin/env python3
"""Build the native standalone LLMWiki CLI consumed by the Electron app."""
from __future__ import annotations

import os
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
RUNTIME_DIR = APP_DIR / "runtime"
DIST_DIR = ROOT / "build" / "cli-dist"
WORK_DIR = ROOT / "build" / "pyinstaller"
SPEC_DIR = ROOT / "build"


def main() -> int:
    required_modules = {
        "PyInstaller": "pyinstaller",
        "numpy": "numpy",
        "httpx": "httpx",
        "networkx": "networkx",
        "yaml": "pyyaml",
        "pypdf": "pypdf",
    }
    missing = [package for module, package in required_modules.items() if importlib.util.find_spec(module) is None]
    if missing:
        raise SystemExit(
            "Desktop build environment is incomplete (missing: "
            + ", ".join(missing)
            + "). Run: python3 -m pip install -e '.[pdf,build]'"
        )

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    separator = ";" if os.name == "nt" else ":"
    skill_file = ROOT / ".agents" / "skills" / "llmwiki" / "SKILL.md"
    command = [
        sys.executable,
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name", "llmwiki",
        "--paths", str(ROOT),
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(SPEC_DIR),
        "--collect-submodules", "networkx",
        "--collect-submodules", "httpx",
    ]
    if skill_file.exists():
        command.extend(["--add-data", f"{skill_file}{separator}llmwiki_skill"])
    command.append(str(ROOT / "scripts" / "llmwiki_entry.py"))

    subprocess.run(command, cwd=ROOT, check=True)

    built_name = "llmwiki.exe" if os.name == "nt" else "llmwiki"
    built = DIST_DIR / built_name
    for stale in RUNTIME_DIR.glob("llmwiki*"):
        if stale.is_file() or stale.is_symlink():
            stale.unlink()
    target = RUNTIME_DIR / built_name
    if target.exists():
        target.unlink()
    shutil.copy2(built, target)
    if os.name != "nt":
        target.chmod(target.stat().st_mode | 0o111)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
