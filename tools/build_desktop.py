"""Build the native desktop bundle on its target operating system."""
from __future__ import annotations

import platform
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
NAME = "Auto-Zettelkasten"


def build_command() -> list[str]:
    if sys.platform not in {"darwin", "win32"}:
        raise SystemExit("Build on macOS or Windows; cross-compilation is not supported.")
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--noupx", "--name", NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "desktop"),
        "--specpath", str(ROOT / "build" / "desktop"),
        "--paths", str(ROOT / "src"),
        "--add-data", f"{ROOT / 'src' / 'auto_zettelkasten' / 'web'}:auto_zettelkasten/web",
        "--collect-data", "webview",
        "--copy-metadata", "auto-zettelkasten",
    ]
    excluded = [
        "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "kivy",
        "cefpython3", "webview.platforms.qt", "webview.platforms.gtk",
        "webview.platforms.android", "webview.platforms.cef",
        "webview.platforms.winui3", "webview.platforms.mshtml",
        "matplotlib", "numpy", "pandas", "scipy", "torch", "tensorflow",
    ]
    if sys.platform == "darwin":
        command += [
            "--hidden-import", "webview.platforms.cocoa",
            "--osx-bundle-identifier", "org.auto-zettelkasten.desktop",
            "--target-architecture", platform.machine(),
        ]
        excluded += ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr", "pythonnet"]
    else:
        command += [
            "--hidden-import", "webview.platforms.winforms",
            "--hidden-import", "webview.platforms.edgechromium",
        ]
        excluded += ["webview.platforms.cocoa"]
    for module in excluded:
        command += ["--exclude-module", module]
    command.append(str(ROOT / "tools" / "desktop_entry.py"))
    return command


if __name__ == "__main__":
    subprocess.run(build_command(), cwd=ROOT, check=True)
