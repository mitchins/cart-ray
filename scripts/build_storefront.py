from __future__ import annotations

import json
import os
from asyncio import run
from pathlib import Path
from shutil import copy2, copytree
from urllib.parse import urlparse

from cartray.catalogue import CsvCatalogueSourceAdapter, FixturePriceResolver, build_catalogue_from_source
from cartray.presentation import CsvPresentationSourceAdapter, build_presented_catalogue, validate_presentation_assets

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "storefront"
DIST = ROOT / "storefront-dist"
STATIC_FILES = ("index.html", "app.js", "styles.css")


def main() -> None:
    mode = os.environ.get("CARTRAY_STOREFRONT_MODE", "preview")
    if mode not in {"preview", "production"}:
        raise ValueError("CARTRAY_STOREFRONT_MODE must be preview or production")
    config = _configuration(mode)
    DIST.mkdir(exist_ok=True)
    for name in STATIC_FILES:
        copy2(SOURCE / name, DIST / name)
    presentation_sources = run(CsvPresentationSourceAdapter(ROOT / "fixtures" / "catalogue-presentation.csv").load())
    validate_presentation_assets(presentation_sources, SOURCE / "assets" / "products")
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
    catalogue = run(
        build_catalogue_from_source(
            CsvCatalogueSourceAdapter(ROOT / "fixtures" / "catalogue.csv"),
            FixturePriceResolver.from_json(ROOT / "fixtures" / "price-resolutions.json"),
            _fixture_expansions(),
        )
    )
    presentation = run(CsvPresentationSourceAdapter(ROOT / "fixtures" / "catalogue-presentation.csv").load())
    return build_presented_catalogue(catalogue, presentation).public_manifest()


def _fixture_expansions() -> dict[str, tuple[str, ...]]:
    raw = json.loads((ROOT / "fixtures" / "fulfilment-expansions.json").read_text())
    return {key: tuple(value) for key, value in raw.items()}


if __name__ == "__main__":
    main()
