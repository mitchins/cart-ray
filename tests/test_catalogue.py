import json
from dataclasses import replace

import pytest

from cartray.catalogue import load_catalogue_sources
from cartray.errors import CatalogueValidationError, CheckoutValidationError


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
        load_catalogue_sources(source)


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
        load_catalogue_sources(source)
