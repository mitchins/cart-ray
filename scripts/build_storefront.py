from __future__ import annotations

import json
import os
import subprocess
import sys
from asyncio import run
from pathlib import Path
from shutil import copy2, copytree
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from cartray.build_profiles import CatalogueProfile, selected_catalogue_profile
from cartray.catalogue import FixturePriceResolver
from cartray.catalogue_bundle import build_runtime_presented_catalogue
from cartray.compiled_catalogue import COMPILED_CATALOGUE
from cartray.presentation import CsvPresentationSourceAdapter, validate_presentation_assets

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "storefront"
DIST = ROOT / "storefront-dist"
STATIC_FILES = ("index.html", "app.js", "styles.css")


def main() -> None:
    mode = os.environ.get("CARTRAY_STOREFRONT_MODE", "preview")
    if mode not in {"preview", "production"}:
        raise ValueError("CARTRAY_STOREFRONT_MODE must be preview or production")
    profile = _selected_profile(mode)
    _assert_selected_catalogue_is_valid(profile)
    config = _configuration(mode)
    DIST.mkdir(exist_ok=True)
    for name in STATIC_FILES:
        copy2(SOURCE / name, DIST / name)
    presentation_sources = run(CsvPresentationSourceAdapter(profile.presentation).load())
    validate_presentation_assets(presentation_sources, profile.product_assets)
    copytree(SOURCE / "assets", DIST / "assets", dirs_exist_ok=True)
    (DIST / "storefront-config.js").write_text(f"window.CARTRAY_STOREFRONT = {json.dumps(config)};\n")


def _configuration(mode: str) -> dict[str, object]:
    if mode == "preview":
        return {"checkoutEnabled": False, "apiBaseUrl": None, "previewCatalogue": _preview_catalogue()}
    api_base_url = os.environ.get("CARTRAY_STOREFRONT_API_BASE_URL")
    if not api_base_url:
        raise ValueError("CARTRAY_STOREFRONT_API_BASE_URL is required for production builds")
    parsed = urlparse(api_base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CARTRAY_STOREFRONT_API_BASE_URL must be an absolute https URL")
    return {"checkoutEnabled": True, "apiBaseUrl": api_base_url.rstrip("/"), "previewCatalogue": None}


def _selected_profile(mode: str) -> CatalogueProfile:
    profile = selected_catalogue_profile()
    if mode == "preview" and profile.name != "synthetic":
        raise ValueError("CARTRAY_STOREFRONT_MODE preview requires the synthetic catalogue profile")
    return profile


def _preview_catalogue() -> dict[str, object]:
    presented = run(
        build_runtime_presented_catalogue(
            COMPILED_CATALOGUE, FixturePriceResolver.from_json(ROOT / "fixtures" / "price-resolutions.json")
        )
    )
    return presented.public_manifest()


def _assert_selected_catalogue_is_valid(profile: CatalogueProfile) -> None:
    output = ROOT / "src" / "cartray" / "compiled_catalogue.py"
    if profile.name == "synthetic":
        _compile_profile(profile, output, check=True)
        return
    with TemporaryDirectory() as directory:
        _compile_profile(profile, Path(directory) / "compiled_catalogue.py")


def _compile_profile(profile: CatalogueProfile, output: Path, *, check: bool = False) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "compile_catalogue.py"),
            *profile.compiler_arguments(output, check=check),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
