"""Copy CartRay's first-party Worker package beside Pywrangler's vendored dependencies."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from cartray.build_profiles import CatalogueProfile, selected_catalogue_profile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "cartray"
DESTINATION = ROOT / "python_modules" / "cartray"


def main() -> None:
    if not SOURCE.is_dir():
        raise RuntimeError(f"CartRay Worker source is missing: {SOURCE}")
    _assert_synthetic_source_is_current()
    profile = selected_catalogue_profile()
    _synchronize(profile, DESTINATION)


def _assert_synthetic_source_is_current() -> None:
    profile = selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "synthetic"})
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "compile_catalogue.py"),
            *profile.compiler_arguments(SOURCE / "compiled_catalogue.py", check=True),
        ],
        check=True,
    )


def _synchronize(profile: CatalogueProfile, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(SOURCE, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if profile.name != "synthetic":
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "compile_catalogue.py"),
                *profile.compiler_arguments(destination / "compiled_catalogue.py"),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
