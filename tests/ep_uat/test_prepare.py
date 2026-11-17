from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ep_uat_prepare", ROOT / "examples/ep-uat/prepare.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def encoded(value):
    return json.dumps(value).encode()


def fixture():
    image = b"synthetic public image"
    logo = b"synthetic logo"
    pic = "media/" + hashlib.sha256(image).hexdigest() + ".png"
    brand = "media/" + hashlib.sha256(logo).hexdigest() + ".jpg"
    offer = {
        "key": "ep:woo:offer:1",
        "legacyWooId": 1,
        "sourceType": "product",
        "sourceStatus": "publish",
        "parentKey": None,
        "title": "A product",
        "slug": "a-product",
        "productType": "variable",
        "imageId": "3",
        "galleryIds": [],
        "taxonomy": [],
        "menuOrder": 0,
        "commerce": {"_price": "12", "_sku": "sku"},
        "excerptHtml": "<b>Summary</b>",
        "contentHtml": "<p>Real content</p><script>bad()</script>",
        "variationAttributes": {},
        "workflowKeys": [],
        "externalCatalogueCta": None,
        "delivery": {"kind": "none"},
        "urlResolution": {"canonicalUrl": None, "permalinkUrl": None},
    }
    child = copy.deepcopy(offer)
    child.update(
        {
            "key": "ep:woo:offer:2",
            "legacyWooId": 2,
            "sourceType": "product_variation",
            "parentKey": offer["key"],
            "productType": "variation",
            "variationAttributes": {"attribute_minutes": "15"},
        }
    )
    identities = [{"key": x["key"], "uatOffer": True, "sourceStatus": "publish"} for x in [offer, child]]
    media = [
        {
            "kind": "public_image",
            "path": pic,
            "sha256": hashlib.sha256(image).hexdigest(),
            "references": ["ep:woo:media:3"],
            "sourceRelativeFile": "image.png",
        },
        {
            "kind": "public_image",
            "path": brand,
            "sha256": hashlib.sha256(logo).hexdigest(),
            "references": ["ep:woo:media:4"],
            "sourceRelativeFile": "2023/06/Effective-policy-logo.jpg",
        },
    ]
    return {
        "offers.json": encoded([offer, child]),
        "legacy-keys.json": encoded(identities),
        "media-manifest.json": encoded({"files": media}),
        "media-relations.json": encoded([]),
        "catalogue-config.json": encoded(
            {"sourceCommerceOptions": {"woocommerce_currency": "AUD", "woocommerce_prices_include_tax": "no"}}
        ),
        pic: image,
        brand: logo,
    }


def test_products_and_variants_remain_separate_from_checkout_authority():
    result = module.prepare(fixture(), "source-hash")
    product = result["products"][0]
    assert result["reconciliation"]["products"] == 1
    assert result["reconciliation"]["variations"] == 1
    assert product["key"] == "ep:woo:offer:1"
    assert product["variations"][0]["key"] == "ep:woo:offer:2"
    assert not product["checkout_authorized"] and not product["variations"][0]["checkout_authorized"]
    assert product["canonical_url"] is None and "bad()" not in product["description"]
    assert "<p>Real content</p>" in product["description_html"]
    assert "script" not in product["description_html"]


def test_rejects_unreconciled_or_nonpublished_source_identity():
    blobs = fixture()
    items = json.loads(blobs["offers.json"])
    items[1]["parentKey"] = "ep:woo:offer:missing"
    blobs["offers.json"] = encoded(items)
    with pytest.raises(ValueError, match="parent"):
        module.prepare(blobs, "source-hash")
    blobs = fixture()
    items = json.loads(blobs["offers.json"])
    items[0]["sourceStatus"] = "draft"
    blobs["offers.json"] = encoded(items)
    with pytest.raises(ValueError, match="published"):
        module.prepare(blobs, "source-hash")


def test_rejects_unverified_canonical_and_wrong_media_bytes():
    blobs = fixture()
    items = json.loads(blobs["offers.json"])
    items[1]["urlResolution"]["canonicalUrl"] = "https://invalid.test/"
    blobs["offers.json"] = encoded(items)
    with pytest.raises(ValueError, match="canonical"):
        module.prepare(blobs, "source-hash")
    blobs = fixture()
    image = next(k for k in blobs if k.endswith(".png"))
    blobs[image] = b"tampered"
    with pytest.raises(ValueError, match="media mismatch"):
        module.prepare(blobs, "source-hash")
