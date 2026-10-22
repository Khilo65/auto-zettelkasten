"""Build the native desktop bundle on its target operating system."""
from __future__ import annotations

import platform
import importlib.metadata
import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
NAME = "Auto-Zettelkasten"


def check_dependencies() -> None:
    """Reject an incomplete build environment instead of shipping a broken mapper."""
    required = {
        "PyInstaller": "pyinstaller", "webview": "pywebview", "certifi": "certifi",
        "numpy": "numpy", "scipy": "scipy", "torch": "torch",
        "transformers": "transformers", "sentence_transformers": "sentence-transformers",
        "tokenizers": "tokenizers", "safetensors": "safetensors",
        "sklearn": "scikit-learn", "igraph": "igraph", "leidenalg": "leidenalg",
    }
    missing = []
    for module, distribution in required.items():
        try:
            if importlib.util.find_spec(module) is None:
                raise importlib.metadata.PackageNotFoundError(distribution)
            importlib.metadata.version(distribution)
        except (ImportError, ValueError, importlib.metadata.PackageNotFoundError):
            missing.append(distribution)
    if missing:
        raise SystemExit(
            "Desktop build dependencies are missing: " + ", ".join(missing)
            + '. Install the build environment with python -m pip install -e ".[desktop,desktop-build,local-mapping]".'
        )


def build_command() -> list[str]:
    if sys.platform not in {"darwin", "win32"}:
        raise SystemExit("Build on macOS or Windows; cross-compilation is not supported.")
    check_dependencies()
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
    # MiniLM's BERT family and sentence-transformer modules are loaded by name.
    # Collect these runtime modules, not every Transformers model/training backend.
    for module in (
        "sentence_transformers.SentenceTransformer",
        "sentence_transformers.models.Transformer",
        "sentence_transformers.models.Pooling",
        "sentence_transformers.models.Normalize",
    ):
        command += ["--hidden-import", module]
    command += ["--collect-submodules", "transformers.models.bert"]
    for module in ("sentence_transformers", "transformers"):
        command += ["--collect-data", module]
    for module in ("tokenizers", "safetensors", "igraph", "leidenalg"):
        command += ["--collect-all", module]
    for distribution in ("numpy", "torch", "sentence-transformers", "igraph", "leidenalg"):
        command += ["--recursive-copy-metadata", distribution]
    if importlib.util.find_spec("hdbscan") is not None:
        command += ["--collect-all", "hdbscan", "--copy-metadata", "hdbscan"]
    excluded = [
        "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "kivy",
        "cefpython3", "webview.platforms.qt", "webview.platforms.gtk",
        "webview.platforms.android", "webview.platforms.cef",
        "webview.platforms.winui3", "webview.platforms.mshtml",
        "matplotlib", "tensorflow", "flax", "jax", "jaxlib",
    ]
    if sys.platform == "darwin":
        command += [
            "--icon", str(ROOT / "src" / "auto_zettelkasten" / "web" / "app-icon.icns"),
            "--hidden-import", "webview.platforms.cocoa",
            "--osx-bundle-identifier", "org.auto-zettelkasten.desktop",
            "--target-architecture", platform.machine(),
        ]
        excluded += ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr", "pythonnet"]
    else:
        command += [
            "--icon", str(ROOT / "src" / "auto_zettelkasten" / "web" / "app-icon.ico"),
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
