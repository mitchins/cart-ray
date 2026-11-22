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


def test_plain_flushes_trailing_source_text():
    assert module.plain("<p>Supplied by AT&T") == "Supplied by AT&T"


def test_only_explicit_source_link_is_preserved_without_changing_surrounding_copy():
    source = "https://effectivepolicy.com.au/get-a-quote/"
    policy = {"version": 1, "links": {source: "/training-and-induction/#quote"}}
    blobs = fixture()
    items = json.loads(blobs["offers.json"])
    items[0]["contentHtml"] = (f'<p>Want a quote? <a class="old" href="{source}" onclick="bad()">'
                                '<strong>Get one here</strong></a> today. '
                                '<a href="https://unknown.example/">Other</a></p>')
    blobs["offers.json"] = encoded(items)
    product = module.prepare(blobs, "hash", link_policy=policy)["products"][0]
    assert ('<p>Want a quote? <a href="/training-and-induction/#quote"><strong>Get one here</strong></a> today.'
            in product["description_html"])
    assert 'Other</span><span class="muted"> [source link disabled in UAT]</span>' in product["description_html"]
    assert "onclick" not in product["description_html"] and "bad()" not in product["description_html"]
    default = module.prepare(blobs, "hash")["products"][0]["description_html"]
    assert '<a href=' not in default and default.count("source link disabled") == 2


@pytest.mark.parametrize("target", ["javascript:alert(1)", "//evil.test/", "/../secret/",
                                    "/%2e%2e/secret/", "/ok/#x\n", "https://checkout.stripe.com/c/pay/",
                                    "https://example.test/checkout/", "https://example.test/?add-to-cart=1",
                                    "mailto:a@example.test?subject=hello", "https://127.0.0.1/admin/",
                                    "https://internal.local/private/"])
def test_link_policy_rejects_unsafe_destinations(target):
    with pytest.raises(ValueError, match="destination"):
        module.prepare(fixture(), "hash", link_policy={"version": 1, "links": {"https://example.test/a/": target}})


@pytest.mark.parametrize("source", ["javascript:alert(1)", "//evil.test/", "https://evil.test/../x/", "https://evil.test/%2e%2e/x/"])
def test_link_policy_rejects_unsafe_source_urls(source):
    with pytest.raises(ValueError, match="source link"):
        module.prepare(fixture(), "hash", link_policy={"version": 1, "links": {source: "/safe/"}})


def test_link_policy_file_rejects_duplicate_mappings(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"version":1,"links":{"https://example.test/a/":"/one/",'
                    '"https://example.test/a/":"/two/"}}')
    with pytest.raises(ValueError, match="duplicate"):
        module.read_link_policy(path)


@pytest.mark.parametrize(("href", "target"), [
    ("mailto:enquiry@example.test", "mailto:enquiry@example.test"),
    ("https://youtube.com/watch?v=ABC", "https://youtube.com/watch?v=ABC"),
    ("http://example.test/service/", "https://example.test/service/"),
    ("https://example.test/catalog/info/id:649", "https://example.test/catalog/info/id:649"),
    ("https://example.test/initial-consult", "/initial-consult"),
    ("https://example.test/media/file.png", "/media/file.png"),
    ("/service/", "/service/"),
])
def test_explicit_safe_external_and_email_mappings_can_be_reviewed(href, target):
    mapping = {"version": 1, "links": {href: target}}
    assert module.validate_link_policy(mapping) == {href: target}


def encoded(value):
    return json.dumps(value).encode()


def fixture():
    image = b"synthetic public image"
    logo = b"\xff\xd8synthetic logo"
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
