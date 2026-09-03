import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from cartray.catalogue import (
    CsvCatalogueSourceAdapter,
    FixturePriceResolver,
    StaticCatalogueSourceAdapter,
    build_catalogue_from_source,
)
from cartray.errors import CatalogueValidationError, CheckoutValidationError
from cartray.test_catalogue import (
    TEST_CATALOGUE_SOURCE_ADAPTER,
    TEST_CATALOGUE_SOURCES,
    TEST_FULFILMENT_EXPANSIONS,
)


def test_worker_and_preview_use_the_same_synthetic_catalogue_policy():
    root = Path(__file__).parents[1] / "fixtures"
    assert TEST_CATALOGUE_SOURCES == asyncio.run(CsvCatalogueSourceAdapter(root / "catalogue.csv").load())
    assert TEST_CATALOGUE_SOURCES == asyncio.run(TEST_CATALOGUE_SOURCE_ADAPTER.load())
    assert TEST_FULFILMENT_EXPANSIONS == {
        key: tuple(value) for key, value in json.loads((root / "fulfilment-expansions.json").read_text()).items()
    }


def test_catalogue_build_uses_the_same_contract_for_csv_and_a_future_adapter():
    root = Path(__file__).parents[1] / "fixtures"
    resolver = FixturePriceResolver.from_json(root / "price-resolutions.json")
    expansions = {
        key: tuple(value) for key, value in json.loads((root / "fulfilment-expansions.json").read_text()).items()
    }
    csv_catalogue = asyncio.run(
        build_catalogue_from_source(CsvCatalogueSourceAdapter(root / "catalogue.csv"), resolver, expansions)
    )
    static_catalogue = asyncio.run(
        build_catalogue_from_source(StaticCatalogueSourceAdapter(TEST_CATALOGUE_SOURCES), resolver, expansions)
    )

    assert static_catalogue == csv_catalogue


def test_future_adapter_cannot_bypass_normalized_record_validation():
    root = Path(__file__).parents[1] / "fixtures"
    resolver = FixturePriceResolver.from_json(root / "price-resolutions.json")
    expansions = {
        key: tuple(value) for key, value in json.loads((root / "fulfilment-expansions.json").read_text()).items()
    }
    duplicate_adapter = StaticCatalogueSourceAdapter((TEST_CATALOGUE_SOURCES[0], TEST_CATALOGUE_SOURCES[0]))

    with pytest.raises(CatalogueValidationError, match="duplicate product keys"):
        asyncio.run(build_catalogue_from_source(duplicate_adapter, resolver, expansions))

    malformed_adapter = StaticCatalogueSourceAdapter((replace(TEST_CATALOGUE_SOURCES[0], product_key=1),))
    with pytest.raises(CatalogueValidationError, match="invalid catalogue source record"):
        asyncio.run(build_catalogue_from_source(malformed_adapter, resolver, expansions))


def test_public_and_private_manifests_have_separate_concerns(fixture_catalogue):
    public = fixture_catalogue.public_manifest()
    private = fixture_catalogue.private_manifest()
    template = next(product for product in public["products"] if product["product_key"] == "TEST-TEMPLATE")
    assert template == {
        "product_key": "TEST-TEMPLATE",
        "title": "Test Digital Template",
        "amount_minor": 2500,
        "currency": "aud",
        "max_quantity": 1,
    }
    assert "stripe_price_id" not in template
    assert private["products"]["TEST-TEMPLATE"]["stripe_price_id"] == "price_fixture_template"
    assert "fulfilment_resources" not in json.dumps(public)

    hidden = replace(
        fixture_catalogue.products["TEST-BUNDLE"],
        source=replace(fixture_catalogue.products["TEST-BUNDLE"].source, active=False),
    )
    inactive_catalogue = replace(fixture_catalogue, products={**fixture_catalogue.products, "TEST-BUNDLE": hidden})
    assert {product["product_key"] for product in inactive_catalogue.public_manifest()["products"]} == {
        "TEST-FREE",
        "TEST-SUPPORT-HOURS",
        "TEST-TEMPLATE",
    }


def test_catalogue_rejects_unknown_product(fixture_catalogue):
    try:
        fixture_catalogue.product("UNKNOWN")
    except CheckoutValidationError as error:
        assert "unknown" in str(error)
    else:
        raise AssertionError("expected product validation failure")


def test_catalogue_rejects_duplicate_headers_before_dict_reader_collapses_them(tmp_path):
    source = tmp_path / "duplicate-header.csv"
    source.write_text(
        "product_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,"
        "max_quantity,active,stripe_lookup_key\n"
        "TEST-ITEM,Title,first,download,test-v1,1,true,overwritten\n"
    )

    with pytest.raises(CatalogueValidationError, match="headers"):
        asyncio.run(CsvCatalogueSourceAdapter(source).load())


@pytest.mark.parametrize(
    "contents, message",
    [
        (
            "\ufeffproduct_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,max_quantity,active\n"
            "TEST-ITEM,Title,lookup,download,test-v1,1,true\n",
            "BOM",
        ),
        (
            "product_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,max_quantity,active\n"
            "TEST-ITEM,Title, lookup,download,test-v1,1,true\n",
            "whitespace",
        ),
    ],
)
def test_catalogue_rejects_spreadsheet_encoding_or_whitespace_ambiguity(tmp_path, contents, message):
    source = tmp_path / "ambiguous.csv"
    source.write_text(contents)

    with pytest.raises(CatalogueValidationError, match=message):
        asyncio.run(CsvCatalogueSourceAdapter(source).load())


@pytest.mark.parametrize(
    "contents",
    (
        "product_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,max_quantity,active\n"
        "TEST-ITEM,Title,lookup,download,test-v1,1,true,unexpected\n",
        "product_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,max_quantity,active\n"
        "TEST-ITEM,Title,lookup,download,test-v1,1\n",
        "product_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,max_quantity,active\n"
        "TEST-ITEM,,lookup,download,test-v1,1,true\n",
    ),
)
def test_catalogue_rejects_extra_or_missing_row_values_before_conversion(tmp_path, contents):
    source = tmp_path / "invalid-row.csv"
    source.write_text(contents)

    with pytest.raises(CatalogueValidationError, match="invalid row"):
        asyncio.run(CsvCatalogueSourceAdapter(source).load())
