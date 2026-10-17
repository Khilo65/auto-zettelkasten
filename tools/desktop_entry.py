"""Frozen desktop entry point; keep the GUI on the process main thread."""
from auto_zettelkasten.desktop import main


if __name__ == "__main__":
    raise SystemExit(main())
