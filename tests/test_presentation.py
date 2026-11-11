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


def test_gallery_order_alt_and_versions(fixture_catalogue):
    sources = fixture_presentation_sources()
    original = build_presented_catalogue(fixture_catalogue, sources)
    updated = replace(
        sources[0], image_alt="Primary cover", images_json='[{"image_key":"detail.png","alt":"Detail view"}]'
    )
    gallery = build_presented_catalogue(fixture_catalogue, (updated, *sources[1:]))
    product = next(p for p in gallery.public_manifest()["products"] if p["product_key"] == sources[0].product_key)
    assert product["image_url"] == product["images"][0]["url"]
    assert product["images"] == [
        {"url": image_url(sources[0].image_key), "alt": "Primary cover"},
        {"url": "/assets/products/detail.png", "alt": "Detail view"},
    ]
    assert gallery.catalogue.version == original.catalogue.version
    assert gallery.presentation_version != original.presentation_version
    alt_changed = replace(updated, image_alt="Other cover")
    assert (
        build_presented_catalogue(fixture_catalogue, (alt_changed, *sources[1:])).presentation_version
        != gallery.presentation_version
    )


@pytest.mark.parametrize(
    "metadata",
    [
        "{}",
        '[{"image_key":"../escape","alt":"Bad"}]',
        '[{"image_key":"https://example.com/a.png","alt":"Bad"}]',
        '[{"image_key":"detail.exe","alt":"Bad"}]',
        '[{"image_key":"detail","alt":""}]',
        '[{"image_key":"detail","alt":1}]',
        '[{"image_key":"detail","alt":"Text","price":100}]',
        '[{"image_key":"detail","alt":"Text"},{"image_key":"detail","alt":"Again"}]',
        "[" + ",".join(f'{{"image_key":"detail-{n}","alt":"Text"}}' for n in range(8)) + "]",
    ],
)
def test_gallery_rejects_unsafe_or_unbounded_metadata(fixture_catalogue, metadata):
    sources = fixture_presentation_sources()
    with pytest.raises(CatalogueValidationError):
        build_presented_catalogue(fixture_catalogue, (replace(sources[0], images_json=metadata), *sources[1:]))


def test_gallery_checks_all_assets_and_containment(tmp_path):
    source = PresentationSource(
        "TEST-ITEM", "Description", "primary", images_json='[{"image_key":"detail.png","alt":"Detail"}]'
    )
    (tmp_path / "primary.webp").write_bytes(b"RIFF0000WEBP")
    with pytest.raises(CatalogueValidationError, match="unavailable"):
        validate_presentation_assets((source,), tmp_path)
    (tmp_path / "detail.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    validate_presentation_assets((source,), tmp_path)
    (tmp_path / "detail.png").unlink()
    (tmp_path / "detail.png").symlink_to(tmp_path.parent / "outside.png")
    with pytest.raises(CatalogueValidationError, match="escapes"):
        validate_presentation_assets((source,), tmp_path)


def test_asset_bytes_change_presentation_only(fixture_catalogue, tmp_path):
    from cartray.presentation import fingerprint_presentation_assets

    sources = fixture_presentation_sources()
    for source in sources:
        (tmp_path / f"{source.image_key}.webp").write_bytes(b"RIFF0000WEBPfirst")
    first = build_presented_catalogue(fixture_catalogue, fingerprint_presentation_assets(sources, tmp_path))
    (tmp_path / f"{sources[0].image_key}.webp").write_bytes(b"RIFF0000WEBPchanged")
    second = build_presented_catalogue(fixture_catalogue, fingerprint_presentation_assets(sources, tmp_path))
    assert first.presentation_version != second.presentation_version
    assert first.catalogue.version == second.catalogue.version


def test_legacy_sources_keep_original_presentation_hash(fixture_catalogue):
    # Historical compiled records omit all optional gallery and fingerprint fields.
    sources = tuple(
        PresentationSource(
            **{
                "product_key": source.product_key,
                "short_description": source.short_description,
                "image_key": source.image_key,
            }
        )
        for source in fixture_presentation_sources()
    )
    assert build_presented_catalogue(fixture_catalogue, sources).presentation_version == (
        "sha256:f9df98a40ac1a83944e74489edab771262e43836723b74524ea964d1c96fbd8b"
    )


@pytest.mark.parametrize(
    "filename, contents",
    [
        ("photo.jpg", b"\xff\xd8\xfffixture"),
        ("photo.jpeg", b"\xff\xd8\xfffixture"),
        ("diagram.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        ("photo.png", b"\x89PNG\r\n\x1a\nfixture"),
    ],
)
def test_explicit_image_types_preserve_safe_local_urls(tmp_path, filename, contents):
    source = PresentationSource("TEST-ITEM", "Description", filename)
    (tmp_path / filename).write_bytes(contents)
    validate_presentation_assets((source,), tmp_path)
    assert image_url(filename) == f"/assets/products/{filename}"


@pytest.mark.parametrize("primary, extra", [("cover", "cover.webp"), ("cover.webp", "cover")])
def test_gallery_rejects_aliases_resolving_to_same_public_asset(fixture_catalogue, primary, extra):
    sources = fixture_presentation_sources()
    duplicate = replace(
        sources[0],
        image_key=primary,
        images_json=f'[{{"image_key":"{extra}","alt":"Duplicate cover"}}]',
    )
    with pytest.raises(CatalogueValidationError, match="duplicate catalogue gallery image"):
        build_presented_catalogue(fixture_catalogue, (duplicate, *sources[1:]))
