#!/usr/bin/env python3
"""Build the pinned official reader outside the checkout and package its web assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = ROOT / "third_party/zotero-reader/pin.json"
OUTPUT = ROOT / "src/auto_zettelkasten/web/zotero-reader"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args: list[str], cwd: Path, env: dict | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def verify_source(source: Path, pin: dict) -> None:
    if run(["git", "rev-parse", "HEAD"], source) != pin["revision"]:
        raise RuntimeError("Reader revision does not match pin")
    for relative, revision in pin["submodules"].items():
        if run(["git", "rev-parse", "HEAD"], source / relative) != revision:
            raise RuntimeError(f"Reader submodule differs from pin: {relative}")
        if run(["git", "status", "--porcelain", "--untracked-files=no"], source / relative):
            raise RuntimeError(f"Reader submodule has modified source: {relative}")
    if run(["git", "status", "--porcelain", "--untracked-files=no"], source):
        raise RuntimeError("Reader checkout has modified source")
    if (source / ".zotero-locale-commit").read_text().strip() != pin["locale_revision"]:
        raise RuntimeError("Locale revision does not match pin")


def license_inventory(source: Path) -> tuple[list[dict], str]:
    """Retain an intentionally inclusive inventory of build and runtime dependencies."""
    inventory, notices = [], []
    seen = set()
    packages = [source, source / "pdfjs/pdf.js", source / "epubjs/epub.js",
                source / "structured-document-text"]
    for base in (source / "node_modules", source / "pdfjs/pdf.js/node_modules"):
        # npm installations can contain nested dependency versions; retain each.
        for directory, _, files in os.walk(base, followlinks=False):
            if "package.json" in files:
                packages.append(Path(directory))
    for package in packages:
        metadata_path = package / "package.json"
        if not metadata_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text())
        except (ValueError, UnicodeDecodeError):
            continue
        if not metadata.get("name"):
            continue
        identity = (metadata["name"], metadata.get("version", "unversioned"))
        if identity in seen:
            continue
        seen.add(identity)
        license_files = [p for p in package.iterdir() if p.is_file() and
                         p.name.lower().startswith(("license", "licence", "copying", "notice"))]
        record = {"name": identity[0], "version": identity[1],
                  "license": metadata.get("license", metadata.get("licenses", "UNSPECIFIED")),
                  "scope": "inclusive-build-and-runtime",
                  "license_files": [p.name for p in sorted(license_files)]}
        inventory.append(record)
        notices.append(f"\n{'=' * 72}\n{identity[0]} {identity[1]}\nLicense: {record['license']}\n")
        for path in sorted(license_files):
            notices.append(f"\n--- {path.name} ---\n{path.read_text(errors='replace')}\n")
    return sorted(inventory, key=lambda x: (x["name"], x["version"])), "".join(notices)


def package_build(source: Path, pin: dict, output: Path = OUTPUT) -> dict:
    verify_source(source, pin)
    build = source / "build/web"
    for name in ("reader.js", "reader.css", "pdf/web/viewer.html", "pdf/build/pdf.mjs",
                 "pdf/build/pdf.worker.mjs"):
        if not (build / name).is_file():
            raise RuntimeError(f"Incomplete reader web build: {name}")
    output.mkdir(parents=True, exist_ok=True)
    # Replace only the generated directory; never touch the adjacent host or workspace.
    shutil.copytree(build, output, dirs_exist_ok=True)
    for file in output.rglob("*"):
        if file.is_file() and not (build / file.relative_to(output)).exists():
            file.unlink()
    shutil.copy2(source / "COPYING", output / "COPYING")
    inventory, notices = license_inventory(source)
    (output / "THIRD_PARTY_NOTICES.txt").write_text(notices)
    inputs = {name: digest(source / name) for name in
              ("package-lock.json", "pdfjs/pdf.js/package-lock.json")}
    files = {str(p.relative_to(output)): digest(p) for p in sorted(output.rglob("*")) if p.is_file()}
    manifest = {**pin, "input_lockfiles_sha256": inputs,
                "assets_sha256": files, "dependencies": inventory,
                "distribution_review_required": True,
                "corresponding_source_instructions": "third_party/zotero-reader/README.md"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--reuse-build", action="store_true",
                        help="Package an already-built private checkout after pin verification")
    args = parser.parse_args()
    cache = args.cache_root.expanduser().resolve()
    if cache == ROOT or ROOT in cache.parents:
        parser.error("Reader sources and dependencies must stay outside the repository")
    cache.mkdir(parents=True, exist_ok=True)
    pin = json.loads(PIN_FILE.read_text())
    source = cache / "source"
    if not source.exists():
        if args.reuse_build:
            parser.error("No existing private build")
        run(["git", "clone", "--filter=blob:none", "--no-checkout", pin["repository"], str(source)], cache)
        run(["git", "fetch", "--depth", "1", "origin", pin["revision"]], source)
        run(["git", "checkout", "--detach", pin["revision"]], source)
        run(["git", "submodule", "update", "--init", "--recursive", "--depth", "1"], source)
    verify_source(source, pin)
    if not args.reuse_build:
        if shutil.disk_usage(cache).free < 3 * 1024**3:
            parser.error("Reader build requires at least 3 GiB available in its cache volume")
        env = {**os.environ, "NODE_OPTIONS": "--max-old-space-size=3072", "PDFJS_CONFIG": "web"}
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=source, env=env, check=True)
        subprocess.run(["bash", "pdfjs/build"], cwd=source, env=env, check=True)
        subprocess.run(["./node_modules/.bin/webpack", "--config-name", "web"], cwd=source, env=env, check=True)
    manifest = package_build(source, pin)
    print(json.dumps({"output": str(OUTPUT), "revision": manifest["revision"],
                      "files": len(manifest["assets_sha256"]),
                      "bytes": sum(p.stat().st_size for p in OUTPUT.rglob("*") if p.is_file())}))


if __name__ == "__main__":
    main()
