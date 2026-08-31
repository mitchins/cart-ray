"""Copy CartRay's first-party Worker package beside Pywrangler's vendored dependencies."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "cartray"
DESTINATION = ROOT / "python_modules" / "cartray"


def main() -> None:
    if not SOURCE.is_dir():
        raise RuntimeError(f"CartRay Worker source is missing: {SOURCE}")
    shutil.copytree(SOURCE, DESTINATION, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


if __name__ == "__main__":
    main()
