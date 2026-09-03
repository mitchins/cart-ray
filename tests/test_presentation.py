from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from cartray.errors import CatalogueValidationError
from cartray.models import PresentationSource
from cartray.presentation import (
    CsvPresentationSourceAdapter,
    build_presented_catalogue,
    image_url,
    validate_presentation_assets,
)


def fixture_presentation_sources() -> tuple[PresentationSource, ...]:
    path = Path(__file__).parents[1] / "fixtures" / "catalogue-presentation.csv"
    return asyncio.run(CsvPresentationSourceAdapter(path).load())


def test_presentation_composition_keeps_commerce_and_presentation_versions_independent(fixture_catalogue):
    sources = fixture_presentation_sources()
    presented = build_presented_catalogue(fixture_catalogue, sources)
    manifest = presented.public_manifest()
    template = next(product for product in manifest["products"] if product["product_key"] == "TEST-TEMPLATE")

    assert manifest["version"] == fixture_catalogue.version
    assert manifest["presentation_version"] == presented.presentation_version
    assert template["short_description"] == "A synthetic downloadable template for storefront testing."
    assert template["image_url"] == "/assets/products/test-template-cover-v1.webp"
    assert "stripe_lookup_key" not in str(manifest)
    assert "fulfilment" not in str(manifest)

    changed = build_presented_catalogue(
        fixture_catalogue,
        (replace(sources[0], short_description="An updated public description."), *sources[1:]),
    )
    assert changed.catalogue.version == presented.catalogue.version
    assert changed.presentation_version != presented.presentation_version


def test_presentation_requires_an_exact_join_including_inactive_products(fixture_catalogue):
    sources = fixture_presentation_sources()
    with pytest.raises(CatalogueValidationError, match="exactly match"):
        build_presented_catalogue(fixture_catalogue, sources[:-1])

    inactive_product = replace(
        fixture_catalogue.products["TEST-FREE"],
        source=replace(fixture_catalogue.products["TEST-FREE"].source, active=False),
    )
    inactive_catalogue = replace(
        fixture_catalogue, products={**fixture_catalogue.products, "TEST-FREE": inactive_product}
    )
    manifest = build_presented_catalogue(inactive_catalogue, sources).public_manifest()
    assert "TEST-FREE" not in {product["product_key"] for product in manifest["products"]}


def test_presentation_rejects_surrounding_whitespace_from_any_adapter(fixture_catalogue):
    sources = fixture_presentation_sources()

    with pytest.raises(CatalogueValidationError, match="invalid catalogue presentation record"):
        build_presented_catalogue(
            fixture_catalogue,
            (replace(sources[0], short_description=" A description with surrounding whitespace. "), *sources[1:]),
        )


@pytest.mark.parametrize(
    "contents, message",
    [
        ("product_key,short_description,image_path\nTEST-ITEM,Description,image\n", "headers"),
        ("product_key,short_description,image_key\nTEST-ITEM,Description, image\n", "whitespace"),
        ("product_key,short_description,image_key\nTEST-ITEM,Description,Image_1\n", "invalid"),
        (
            "product_key,short_description,image_key\n"
            "TEST-ITEM,Description,image\n"
            "TEST-ITEM,Another description,image-two\n",
            "duplicate",
        ),
    ],
)
def test_presentation_csv_rejects_ambiguous_or_invalid_records(tmp_path, contents, message):
    path = tmp_path / "catalogue-presentation.csv"
    path.write_text(contents)

    with pytest.raises(CatalogueValidationError, match=message):
        asyncio.run(CsvPresentationSourceAdapter(path).load())


def test_image_url_and_assets_are_static_webp_only(tmp_path):
    sources = (PresentationSource("TEST-ITEM", "A public description.", "test-item-cover-v1"),)
    directory = tmp_path / "products"
    directory.mkdir()
    asset = directory / "test-item-cover-v1.webp"
    asset.write_bytes(b"RIFF\x00\x00\x00\x00WEBPfixture")

    assert image_url("test-item-cover-v1") == "/assets/products/test-item-cover-v1.webp"
    validate_presentation_assets(sources, directory)

    asset.write_bytes(b"not-webp")
    with pytest.raises(CatalogueValidationError, match="not WebP"):
        validate_presentation_assets(sources, directory)
    with pytest.raises(CatalogueValidationError, match="unavailable"):
        validate_presentation_assets(
            (PresentationSource("TEST-OTHER", "Another public description.", "missing-cover-v1"),), directory
        )
    with pytest.raises(CatalogueValidationError, match="image key"):
        image_url("https://attacker.invalid/image.webp")
