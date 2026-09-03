from __future__ import annotations

import json
import os
import subprocess
import sys
from asyncio import run
from pathlib import Path
from shutil import copy2, copytree
from urllib.parse import urlparse

from cartray.catalogue import FixturePriceResolver
from cartray.catalogue_bundle import build_runtime_presented_catalogue
from cartray.compiled_catalogue import COMPILED_CATALOGUE
from cartray.presentation import validate_presentation_assets

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "storefront"
DIST = ROOT / "storefront-dist"
STATIC_FILES = ("index.html", "app.js", "styles.css")


def main() -> None:
    mode = os.environ.get("CARTRAY_STOREFRONT_MODE", "preview")
    if mode not in {"preview", "production"}:
        raise ValueError("CARTRAY_STOREFRONT_MODE must be preview or production")
    _assert_compiled_catalogue_is_current()
    config = _configuration(mode)
    DIST.mkdir(exist_ok=True)
    for name in STATIC_FILES:
        copy2(SOURCE / name, DIST / name)
    validate_presentation_assets(COMPILED_CATALOGUE.presentation_sources, SOURCE / "assets" / "products")
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


def _preview_catalogue() -> dict[str, object]:
    presented = run(
        build_runtime_presented_catalogue(
            COMPILED_CATALOGUE, FixturePriceResolver.from_json(ROOT / "fixtures" / "price-resolutions.json")
        )
    )
    return presented.public_manifest()


def _assert_compiled_catalogue_is_current() -> None:
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
            str(SOURCE / "assets" / "products"),
            "--output",
            str(ROOT / "src" / "cartray" / "compiled_catalogue.py"),
            "--check",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
