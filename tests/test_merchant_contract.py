import pytest

from cartray.merchant_contract import merchant_path, page_seo, register_path, seo_head


@pytest.mark.parametrize("path", ["//evil/", "/product/../cart/", "/product/%2e%2e/", "/cart/", "/api/orders/",
                                  "/uat-return/", "/_headers/", "/_redirects/", "/_worker/",
                                  "/product/name/?x=1", "/product/name#x/", "/product//"])
def test_reject_unsafe_or_reserved_product_paths(path):
    with pytest.raises(ValueError):
        merchant_path(path, product=True)


def test_accept_explicit_merchant_case_and_underscore():
    assert merchant_path("/product/Policy_Template/", product=True) == "/product/Policy_Template/"
    with pytest.raises(ValueError):
        merchant_path("/Cart/", product=True)
    with pytest.raises(ValueError):
        merchant_path("/product/" + "a" * 256 + "/", product=True)


def test_shared_path_prefix_casing_must_be_consistent():
    seen = {}
    register_path("/product/first/", seen)
    register_path("/product/second/", seen)
    with pytest.raises(ValueError, match="casing"):
        register_path("/Product/third/", seen)


def test_metadata_rejects_unapproved_claims_and_unsafe_canonicals():
    seo = {"title": "Product", "description": "Description", "canonical_url": "https://example.test/product/p/",
           "robots": "noindex,nofollow"}
    page_seo(seo)
    for value in ("javascript:alert(1)", "https://example.test@evil.test/product/p/",
                  "https://999.1.2.3/product/p/", "https://bad..host/product/p/",
                  "https://example.test../product/p/",
                  "https://example.test/product/../p/", "https://example.test/product/p/?x=1"):
        with pytest.raises(ValueError):
            page_seo({**seo, "canonical_url": value})
    with pytest.raises(ValueError):
        page_seo({**seo, "structured_data": {"@type": "Product", "name": "P", "aggregateRating": "5"}})


def test_source_length_and_absent_description_are_preserved():
    seo = {"title": "Product", "canonical_url": "https://example.test/product/p/", "robots": "index,follow"}
    assert 'name="description"' not in seo_head(seo, force_noindex=True)
    description = ('Long & " text ' * 250) + " end"
    assert len(description) > 3000
    rendered = seo_head({**seo, "description": description}, force_noindex=True)
    assert 'Long &amp; &quot; text' in rendered
    assert " end" in rendered
    assert 'content="noindex,nofollow"' in rendered
    assert 'name="description" content=""' in seo_head({**seo, "description": ""})
    with pytest.raises(ValueError):
        page_seo({**seo, "description": "x" * 16385})
