"""Copy CartRay's first-party Worker package beside Pywrangler's vendored dependencies."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "cartray"
DESTINATION = ROOT / "python_modules" / "cartray"


def main() -> None:
    if not SOURCE.is_dir():
        raise RuntimeError(f"CartRay Worker source is missing: {SOURCE}")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "compile_catalogue.py"),
            "--catalogue",
            str(ROOT / "fixtures" / "catalogue.csv"),
            "--price-resolutions",
            str(ROOT / "fixtures" / "price-resolutions.json"),
            "--fulfilment-expansions",
            str(ROOT / "fixtures" / "fulfilment-expansions.json"),
            "--presentation",
            str(ROOT / "fixtures" / "catalogue-presentation.csv"),
            "--product-assets",
            str(ROOT / "storefront" / "assets" / "products"),
            "--output",
            str(SOURCE / "compiled_catalogue.py"),
            "--check",
        ],
        check=True,
    )
    if DESTINATION.exists():
        shutil.rmtree(DESTINATION)
    shutil.copytree(SOURCE, DESTINATION, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


if __name__ == "__main__":
    main()
