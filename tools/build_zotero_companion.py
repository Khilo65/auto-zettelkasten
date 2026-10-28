"""Create the capability-only companion XPI; never install it or access a profile."""
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


def build(destination: Path, *, root: Path | None = None) -> Path:
    source = (root or Path(__file__).resolve().parents[1]) / "zotero-companion"
    destination = Path(destination)
    if destination.suffix != ".xpi":
        raise ValueError("The companion output must be an .xpi file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in ("bootstrap.js", "manifest.json"):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, (source / name).read_bytes())
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    print(build(parser.parse_args().output))
