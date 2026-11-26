from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from test_prepare import fixture
from test_prepare import module as prepare

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ep_uat_build", ROOT / "examples/ep-uat/build.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def make_source(path):
    blobs = fixture()
    data = prepare.prepare(blobs, "synthetic-hash")
    path.mkdir()
    (path / "catalogue.json").write_text(json.dumps(data))
    for entry in data["public_media"]:
        target = path / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[entry["path"]])
    return data


@pytest.mark.parametrize("theme", ["minimal", "studio", "classic"])
def test_local_build_preserves_identity_without_checkout_or_canonical(tmp_path, theme):
    source = tmp_path / "source"
    data = make_source(source)
    hidden = source / "media/private-draft.png"
    hidden.write_bytes(b"not approved")
    output = tmp_path / "output"
    module.build(source, output, theme)
    assert (output / "index.html").read_text().count('href="/cart/"') == 1
    page = (output / "products/woo-1/index.html").read_text()
    assert data["products"][0]["key"] == "ep:woo:offer:1"
    assert "ep:woo:offer:2" in page and "checkout is not available" in page.lower()
    assert 'rel="canonical"' not in page and "stripe" not in page.lower()
    assert not (output / "media/private-draft.png").exists()
    assert not (output / "review/index.html").exists()


def test_local_build_rejects_tampered_media_and_checkout_flag(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    media = source / data["public_media"][0]["path"]
    media.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="fingerprint"):
        module.build(source, tmp_path / "out", "minimal")
    media.write_bytes(fixture()[data["public_media"][0]["path"]])
    data["products"][0]["checkout_authorized"] = True
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="checkout"):
        module.build(source, tmp_path / "out", "minimal")


def test_source_markup_rejects_active_or_undeclared_content():
    for markup in (
        "<script>alert(1)</script>",
        '<img src="/media/other.png" alt="" loading="lazy">',
        '<p onclick="alert(1)">Text</p>',
        '<!--><img src=x onerror=alert(1)>-->',
        '<!--!><img src=x onerror=alert(1)>-->',
    ):
        with pytest.raises(ValueError):
            module.SourceMarkupCheck(set()).feed(markup)

    parser = module.SourceMarkupCheck(set())
    parser.feed('<p>safe</p><img src=x onerror=alert(1)')
    with pytest.raises(ValueError, match="incomplete"):
        parser.close()
    parser = module.SourceMarkupCheck(set())
    parser.feed('<p>Less than &lt; five</p>')
    parser.close()


def test_build_requires_matching_link_policy_and_rejects_forged_anchor(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    policy = ROOT / "examples/ep-uat/approved-link-policy.json"
    data["link_policy_sha256"] = module.policy_digest(module.read_link_policy(policy))
    data["products"][0]["description_html"] = (
        '<p>Before <a href="/training-and-induction/#quote">Get one here</a> after.</p>')
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="link policy differs"):
        module.build(source, tmp_path / "without-policy", "minimal")
    wrong = tmp_path / "wrong-policy.json"
    wrong.write_text(json.dumps({"version": 1,
                                 "links": {"https://example.test/other/": "/training-and-induction/#quote"}}))
    with pytest.raises(ValueError, match="link policy differs"):
        module.build(source, tmp_path / "wrong-policy", "minimal", link_policy=wrong)
    module.build(source, tmp_path / "approved", "minimal", link_policy=policy)
    provenance = json.loads((tmp_path / "approved/provenance.json").read_text())
    assert provenance["link_policy_sha256"] == data["link_policy_sha256"]
    page = (tmp_path / "approved/products/woo-1/index.html").read_text()
    assert '<p>Before <a href="/training-and-induction/#quote">Get one here</a> after.</p>' in page
    for index, href in enumerate(("javascript:alert(1)", "//evil.test/", "/../private/",
                                  "/training-and-induction/#other")):
        data["products"][0]["description_html"] = f'<a href="{href}">Bad</a>'
        (source / "catalogue.json").write_text(json.dumps(data))
        with pytest.raises(ValueError, match="unapproved source markup link"):
            module.build(source, tmp_path / f"bad-{index}", "minimal", link_policy=policy)
    data["products"][0]["description_html"] = '<a href="/training-and-induction/#quote" onclick="bad()">Bad</a>'
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unapproved source markup link"):
        module.build(source, tmp_path / "event-handler", "minimal", link_policy=policy)


def test_approved_link_does_not_change_pdf_or_two_offer_checkout(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    first = data["products"][0]
    first["source_delivery_kind"] = "uat_dummy_transfer"
    second = copy.deepcopy(first)
    second.update(source_id=3, key="ep:woo:offer:3", title="Second", navigation_path="/products/woo-3/")
    free = copy.deepcopy(first)
    free.update(source_id=4, key="ep:woo:offer:4", title="Free", navigation_path="/products/woo-4/",
                action="free_download", free_download="media/free.pdf", source_delivery_kind="free_native_download")
    pdf = b"%PDF-1.4\nsynthetic\n%%EOF"
    (source / "media/free.pdf").write_bytes(pdf)
    data["public_media"].append({"path": "media/free.pdf", "sha256": hashlib.sha256(pdf).hexdigest()})
    data["products"].extend([second, free])
    fixture_path = tmp_path / "checkout.json"
    fixture_path.write_text(json.dumps({"livemode": False, "source_archive_sha256": data["source_archive_sha256"],
        "offers": [{"source_id": item["source_id"], "source_key": item["key"], "currency": "aud",
                    "amount_minor": 1200, "cartray_product_key": f'EP-UAT-{item["source_id"]}'}
                   for item in (first, second)]}))
    catalogue = source / "catalogue.json"
    catalogue.write_text(json.dumps(data))
    module.build(source, tmp_path / "original", "minimal", test_fixture=fixture_path)
    first["description_html"] = '<p>Quote: <a href="/training-and-induction/#quote">Get one here</a></p>'
    policy = ROOT / "examples/ep-uat/approved-link-policy.json"
    data["link_policy_sha256"] = module.policy_digest(module.read_link_policy(policy))
    catalogue.write_text(json.dumps(data))
    module.build(source, tmp_path / "mapped", "minimal", test_fixture=fixture_path, link_policy=policy)
    assert (tmp_path / "original/site-config.js").read_bytes() == (tmp_path / "mapped/site-config.js").read_bytes()
    assert (tmp_path / "mapped/media/free.pdf").read_bytes() == pdf
    assert (tmp_path / "original/products/woo-4/index.html").read_bytes() == (
        tmp_path / "mapped/products/woo-4/index.html").read_bytes()


def test_reviews_are_explicitly_scoped_to_a_product(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    mapping = tmp_path / "reviews.json"
    mapping.write_text(json.dumps({"schema_version": 1, "source_archive_sha256": data["source_archive_sha256"],
                                   "subjects": {"1": "synthetic-subject"}}))
    output = tmp_path / "output"
    module.build(source, output, "minimal", review_fixture=mapping)
    page = (output / "products/woo-1/index.html").read_text()
    assert 'id="reviews"' in page and '"review_subject": "synthetic-subject"' in page
    assert (output / "theme/reviews.mjs").is_file()
    assert (output / "review/index.html").is_file()
    assert (output / "review/manage.html").is_file()
    assert (output / "theme/native-reviews.js").read_bytes() == (ROOT / "modules/reviews/ui/reviews.js").read_bytes()
    assert (output / "theme/native-manage.js").read_bytes() == (ROOT / "modules/reviews/ui/manage.js").read_bytes()
    mapping.write_text(json.dumps({"schema_version": 1, "source_archive_sha256": "wrong", "subjects": {"1": "x"}}))
    with pytest.raises(ValueError, match="fixture"):
        module.build(source, tmp_path / "bad", "minimal", review_fixture=mapping)


def test_merchant_gel_is_injected_and_rejects_wrong_identity(tmp_path):
    source = tmp_path / "source"
    make_source(source)
    gel = json.loads((ROOT / "examples/ep-uat/gel.json").read_text())
    gel["colours"]["brand"] = "#252525"
    path = tmp_path / "merchant-gel.json"
    path.write_text(json.dumps(gel))
    output = tmp_path / "output"
    module.build(source, output, "classic", gel_file=path)
    assert "#252525" in (output / "theme/tokens.css").read_text()
    assert 'src="/assets/merchant-logo.jpg"' in (output / "index.html").read_text()
    assert "Squirrel" not in (output / "index.html").read_text()
    assert "Acorn" not in (output / "index.html").read_text()
    assert json.loads((output / "provenance.json").read_text())["gel_sha256"]
    gel["identity"]["name"] = "Different merchant"
    path.write_text(json.dumps(gel))
    with pytest.raises(ValueError, match="identity"):
        module.build(source, tmp_path / "bad-merchant", "classic", gel_file=path)


def test_listing_accepts_explicit_merchant_identity_and_navigation(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    product = data["products"][0]
    product["source_id"] = "merchant-item-7"
    product["navigation_path"] = "/product/merchant-item-seven/"
    (source / "catalogue.json").write_text(json.dumps(data))
    output = tmp_path / "output"
    module.build(source, output, "minimal")
    assert (output / "product/merchant-item-seven/index.html").is_file()
    assert 'rel="canonical"' not in (output / "product/merchant-item-seven/index.html").read_text()
    product["navigation_path"] = "/products/../checkout/"
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="navigation"):
        module.build(source, tmp_path / "unsafe", "minimal")
    product["navigation_path"] = "/uat-return/"
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="navigation"):
        module.build(source, tmp_path / "reserved", "minimal")
    del product["navigation_path"]
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="navigation"):
        module.build(source, tmp_path / "missing-path", "minimal")


def test_offline_parity_uses_same_product_renderer_with_no_test_authority(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    product = data["products"][0]
    product["navigation_path"] = "/product/approved-route/"
    (source / "catalogue.json").write_text(json.dumps(data))
    def seo(route, description=None):
        return {"title": 'Real <title>', "description": description,
                "canonical_url": "https://merchant.example" + route, "robots": "index,follow"}
    description = 'Long & " description ' * 200
    fixture = tmp_path / "seo.json"
    fixture.write_text(json.dumps({"schema_version": 1, "catalogue_path": "/policies-and-templates/",
                                   "pages": {"/policies-and-templates/": seo("/policies-and-templates/"),
                                             product["navigation_path"]:
                                                 seo(product["navigation_path"], description)}}))
    output = tmp_path / "parity"
    module.build(source, output, "minimal", offline_seo=fixture)
    html = (output / "product/approved-route/index.html").read_text()
    assert '<h1>A product</h1>' in html
    assert 'data-product-gallery' in html
    assert 'name="robots" content="noindex,nofollow"' in html
    assert 'rel="canonical" href="https://merchant.example/product/approved-route/"' in html
    assert 'Long &amp; &quot; description' in html
    assert len(description) > 3000
    assert 'UAT catalogue' not in html
    assert 'Stripe test mode' not in html
    assert 'uat-test-products' not in html
    assert 'data-uat-add' not in html
    assert not (output / "theme/uat.mjs").exists()
    assert (output / "theme/catalogue.mjs").exists()
    assert 'href="/policies-and-templates/"' in html
    assert 'class="brand" href="/policies-and-templates/"' in html
    assert '— catalogue"' in html
    assert 'class="header-cart"' not in html
    assert (output / "policies-and-templates/index.html").is_file()
    assert not (output / "uat-return/index.html").exists()
    cat = (output / "policies-and-templates/index.html").read_text()
    assert 'name="description"' not in cat
    with pytest.raises(ValueError, match="cannot use test checkout"):
        module.build(source, tmp_path / "bad", "minimal", test_fixture="unused", offline_seo=fixture)
    payload = json.loads(fixture.read_text())
    del payload["pages"][product["navigation_path"]]
    fixture.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="cover each"):
        module.build(source, tmp_path / "missing", "minimal", offline_seo=fixture)
    assert not (tmp_path / "missing").exists()


def test_uat_rejects_case_colliding_product_paths(tmp_path):
    import copy

    source = tmp_path / "source"
    data = make_source(source)
    data["products"][0]["navigation_path"] = "/product/Example/"
    other = copy.deepcopy(data["products"][0])
    other.update({"source_id": 3, "key": "merchant:other:3", "navigation_path": "/product/example/"})
    data["products"].append(other)
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="duplicate product navigation"):
        module.build(source, tmp_path / "output", "minimal")
    other["navigation_path"] = "/Product/different/"
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="casing"):
        module.build(source, tmp_path / "other-output", "minimal")


def test_merchant_currency_comes_from_catalogue_and_gel(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    data["source_currency"] = "NZD"
    (source / "catalogue.json").write_text(json.dumps(data))
    gel = json.loads((ROOT / "examples/ep-uat/gel.json").read_text())
    gel["identity"]["currency"] = "NZD"
    gel["identity"]["locale"] = "en-NZ"
    path = tmp_path / "gel.json"
    path.write_text(json.dumps(gel))
    out = tmp_path / "out"
    module.build(source, out, "minimal", gel_file=path)
    assert "NZD" in (out / "index.html").read_text()
    assert 'lang="en-NZ"' in (out / "index.html").read_text()
    assert '"currency": "NZD"' in (out / "products/woo-1/index.html").read_text()


def test_aud_sandbox_checkout_cannot_be_shown_as_another_currency(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    data["source_currency"] = "NZD"
    (source / "catalogue.json").write_text(json.dumps(data))
    fixture_path = tmp_path / "test-fixture.json"
    fixture_path.write_text(json.dumps({"livemode": False, "source_archive_sha256": data["source_archive_sha256"],
                                        "offers": []}))
    with pytest.raises(ValueError, match="requires an AUD catalogue"):
        module.build(source, tmp_path / "unsafe", "minimal", test_fixture=fixture_path)


def test_merchant_snapshot_update_preserves_subject_and_rejects_stale_mapping(tmp_path):
    import copy

    source = tmp_path / "source"
    data = make_source(source)
    retired = copy.deepcopy(data["products"][0])
    retired.update({"source_id": 3, "key": "merchant:retired:3", "navigation_path": "/products/retired-item/"})
    data["products"].append(retired)
    (source / "catalogue.json").write_text(json.dumps(data))
    mapping = tmp_path / "reviews.json"
    mapping.write_text(json.dumps({"schema_version": 1, "source_archive_sha256": data["source_archive_sha256"],
                                   "subjects": {"1": "synthetic-subject"}}))
    first = tmp_path / "first"
    module.build(source, first, "minimal", review_fixture=mapping)
    assert (first / "products/retired-item/index.html").is_file()
    product = data["products"][0]
    product["images"][0]["path"] = data["brand_logo"]
    data["products"] = [product]
    (source / "catalogue.json").write_text(json.dumps(data))
    second = tmp_path / "second"
    module.build(source, second, "minimal", review_fixture=mapping)
    page = (second / "products/woo-1/index.html").read_text()
    assert not (second / "products/retired-item/index.html").exists()
    assert data["brand_logo"] in page and '"review_subject": "synthetic-subject"' in page
    assert (second / data["brand_logo"]).read_bytes() == (source / data["brand_logo"]).read_bytes()
    mapping.write_text(json.dumps({"schema_version": 1, "source_archive_sha256": data["source_archive_sha256"],
                                   "subjects": {"3": "retired-subject"}}))
    with pytest.raises(ValueError, match="review subject"):
        module.build(source, tmp_path / "stale", "minimal", review_fixture=mapping)


def test_search_index_supports_both_original_and_casefolded_unicode(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    data["products"][0]["title"] = "Straße"
    (source / "catalogue.json").write_text(json.dumps(data))
    out = tmp_path / "out"
    module.build(source, out, "minimal")
    page = (out / "index.html").read_text()
    assert "straße" in page and "strasse" in page


@pytest.mark.parametrize("theme", ["minimal", "studio", "classic"])
def test_second_merchant_uses_own_listing_identity_gel_and_currency(tmp_path, theme):
    source = tmp_path / "source"
    data = make_source(source)
    data["merchant_name"] = "Harbour Workshop"
    data["source_currency"] = "NZD"
    product = data["products"][0]
    product.update({"key": "harbour:product:item7", "source_id": "item7",
                    "navigation_path": "/products/workshop-item/"})
    product["variations"][0]["key"] = "harbour:offer:blue"
    (source / "catalogue.json").write_text(json.dumps(data))
    gel = json.loads((ROOT / "examples/ep-uat/gel.json").read_text())
    gel["identity"].update({"key": "harbour-uat", "name": "Harbour Workshop", "short_name": "Harbour",
                            "wordmark_descriptor": "WORKSHOP", "currency": "NZD", "locale": "en-NZ"})
    gel_path = tmp_path / "harbour.json"
    gel_path.write_text(json.dumps(gel))
    output = tmp_path / "output"
    module.build(source, output, theme, gel_file=gel_path)
    page = (output / "products/workshop-item/index.html").read_text()
    assert "Harbour Workshop" in page and "harbour:offer:blue" in page and "NZD" in page
    assert "Effective Policy" not in page and "Squirrel" not in page and "Acorn" not in page
    assert 'rel="canonical"' not in page and "data-uat-add=" not in page


@pytest.mark.parametrize("theme", ["minimal", "studio", "classic"])
def test_connected_cart_uses_shared_client_and_only_explicit_offer(tmp_path, theme):
    source = tmp_path / "source"
    data = make_source(source)
    product = data["products"][0]
    product["source_delivery_kind"] = "uat_dummy_transfer"
    (source / "catalogue.json").write_text(json.dumps(data))
    price_fixture = tmp_path / "fixture.json"
    price_fixture.write_text(json.dumps({"livemode": False, "source_archive_sha256": data["source_archive_sha256"],
        "offers": [{"source_id": 1, "source_key": product["key"], "currency": "aud", "amount_minor": 1200,
                    "cartray_product_key": "EP-UAT-1"}]}))
    output = tmp_path / "out"
    module.build(source, output, theme, test_fixture=price_fixture)
    assert (output / "theme/core-storefront.js").read_bytes() == (ROOT / "storefront/app.js").read_bytes()
    assert (output / "theme/cartray-adapter.mjs").read_bytes() == (
        ROOT / "examples/gel-theme-pack/src/cartray-adapter.mjs"
    ).read_bytes()
    cart = (output / "cart/index.html").read_text()
    assert 'data-cartray-presenter="gel"' in cart and 'data-page="cart"' in cart
    assert 'src="/theme/connected-start.mjs"' in cart
    config = (output / "site-config.js").read_text()
    assert '"productKey": "EP-UAT-1"' in config and '"sourceId": 1' in config
    assert '"productKey": "EP-UAT-2"' not in config
    assert (output / "purchase-return/index.html").is_file()


@pytest.mark.parametrize("theme", ["minimal", "classic"])
def test_connected_merchant_catalogue_path_is_used_across_pages(tmp_path, theme):
    source = tmp_path / "source"
    data = make_source(source)
    product = data["products"][0]
    product["source_delivery_kind"] = "uat_dummy_transfer"
    (source / "catalogue.json").write_text(json.dumps(data))
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"livemode": False, "source_archive_sha256": data["source_archive_sha256"],
        "offers": [{"source_id": 1, "source_key": product["key"], "currency": "aud", "amount_minor": 1200,
                    "cartray_product_key": "EP-UAT-1"}]}))
    review_fixture = tmp_path / "reviews.json"
    review_fixture.write_text(json.dumps({"schema_version": 1, "source_archive_sha256": data["source_archive_sha256"],
                                          "subjects": {"1": "synthetic-subject"}}))
    output = tmp_path / "out"
    module.build(source, output, theme, test_fixture=fixture, review_fixture=review_fixture,
                 catalogue_path="/policies-and-templates/")
    assert (output / "policies-and-templates/index.html").is_file()
    assert not (output / "shop/index.html").exists()
    assert '"cataloguePath": "/policies-and-templates/"' in (output / "site-config.js").read_text()
    for path in ("products/woo-1/index.html", "cart/index.html", "purchase-return/index.html",
                 "review/index.html", "review/manage.html"):
        assert 'href="/policies-and-templates/"' in (output / path).read_text()
    assert "catalogueUrl()" in (output / "theme/app.mjs").read_text()
    if theme == "classic":
        assert 'action="/policies-and-templates/"' in (output / "cart/index.html").read_text()


def test_only_visibility_exclusions_are_labeled_hidden():
    product = {"source_id": 1, "source_visibility": ["rated-5"], "title": "Product", "summary": "",
               "description": "", "source_sku": "", "source_price": "10", "menu_order": 0,
               "navigation_path": "/products/product/", "categories": [], "images": []}
    assert "restricted visibility" not in module.product_card(product)
    product["source_visibility"] = ["exclude-from-catalog"]
    assert "restricted visibility" in module.product_card(product)


def test_generic_connected_flow_rejects_protocol_relative_context_and_images(tmp_path, monkeypatch):
    import importlib
    import sys

    pack = ROOT / "examples/gel-theme-pack"
    if "cartray_gel" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "cartray_gel", pack / "__init__.py", submodule_search_locations=[str(pack)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
    flows = importlib.import_module("cartray_gel.connected_flows")
    offer = {"product_key": "PRODUCT-1", "source_id": 1, "path": "/products/product-one/",
             "image": "/media/product.png", "image_alt": "Product", "category": "Products"}
    with pytest.raises(ValueError, match="same-origin"):
        flows.install(tmp_path, ROOT, {}, "minimal", [offer], context_path="//attacker/context")
    with pytest.raises(ValueError, match="offer presentation"):
        flows.install(tmp_path, ROOT, {}, "minimal", [{**offer, "image": "//attacker.example/image.png"}])
    with pytest.raises(ValueError, match="HTTPS origin"):
        flows.install(tmp_path, ROOT, {}, "minimal", [offer], api_origin="https://example.test@attacker.test/path")
    for cap in (0, True, 1.5, 101, None):
        with pytest.raises(ValueError, match="cart_quantity_limit"):
            flows.install(tmp_path, ROOT, {}, "minimal", [{**offer, "cart_quantity_limit": cap}])
    (tmp_path / "theme").mkdir()
    monkeypatch.setattr(flows.templates, "simple_page", lambda *args: "content")
    monkeypatch.setattr(flows.templates, "page_shell", lambda *args, **kwargs:
                        '<body class="test"><script src="/site-config.js" defer></script>'
                        '<script type="module" src="/theme/app.mjs"></script></body>')
    flows.install(tmp_path, ROOT, {"identity": {"name": "Merchant"}}, "minimal", [{**offer, "cart_quantity_limit": 1}],
                  api_origin="https://checkout.example.test/")
    assert 'apiBaseUrl = "https://checkout.example.test"' in (tmp_path / "storefront-config.js").read_text()
    site = (tmp_path / 'site-config.js').read_text()
    theme = json.JSONDecoder().raw_decode(site.split('window.CARTRAY_THEME = ', 1)[1])[0]
    assert theme['catalogue']['products'][0]['cart_quantity_limit'] == 1


def test_connected_flow_uses_explicit_merchant_path(tmp_path, monkeypatch):
    import importlib
    import sys

    pack = ROOT / "examples/gel-theme-pack"
    if "cartray_gel" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "cartray_gel", pack / "__init__.py", submodule_search_locations=[str(pack)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
    flows = importlib.import_module("cartray_gel.connected_flows")
    (tmp_path / "theme").mkdir()
    monkeypatch.setattr(flows.templates, "simple_page", lambda *args: "content")
    monkeypatch.setattr(flows.templates, "page_shell", lambda *args, **kwargs:
                        '<body class="test"><script src="/site-config.js" defer></script>'
                        '<script type="module" src="/theme/app.mjs"></script></body>')
    offer = {"product_key": "PRODUCT-1", "source_id": 1, "path": "/product/approved-route/",
             "image": "/media/product.png", "image_alt": "Product", "category": "Products"}
    flows.install(tmp_path, ROOT, {"identity": {"name": "Merchant"}}, "minimal", [offer],
                  category_links=[("/category/parent/child/", "Child")])
    assert '"PRODUCT-1": "/product/approved-route/"' in (tmp_path / "storefront-config.js").read_text()
    assert '"path": "/product/approved-route/"' in (tmp_path / "site-config.js").read_text()
    previous = (tmp_path / "site-config.js").read_text()
    with pytest.raises(ValueError, match="conflicting category"):
        flows.install(tmp_path, ROOT, {"identity": {"name": "Merchant"}}, "minimal", [offer],
                      category_links=[("/product/approved-route/", "Conflict")])
    assert (tmp_path / "site-config.js").read_text() == previous


def test_connected_flow_shared_parent_requires_explicit_subjects(tmp_path, monkeypatch):
    import importlib
    import sys

    pack = ROOT / 'examples/gel-theme-pack'
    if 'cartray_gel' not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            'cartray_gel', pack / '__init__.py', submodule_search_locations=[str(pack)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
    flows = importlib.import_module('cartray_gel.connected_flows')
    (tmp_path / 'theme').mkdir()
    monkeypatch.setattr(flows.templates, 'simple_page', lambda *args: 'content')
    monkeypatch.setattr(flows.templates, 'page_shell', lambda *args, **kwargs:
                        '<body class="test"><script src="/site-config.js" defer></script>'
                        '<script type="module" src="/theme/app.mjs"></script></body>')
    offers = [{'product_key': key, 'source_id': key, 'path': '/product/consulting/',
               'image': '/media/product.png', 'image_alt': 'Consulting', 'category': 'Services'}
              for key in ('OFFER-15', 'OFFER-30')]
    with pytest.raises(ValueError, match='identical review subject'):
        flows.install(tmp_path, ROOT, {'identity': {'name': 'Merchant'}}, 'minimal', offers)
    subjects = {'OFFER-15': 'consulting', 'OFFER-30': 'consulting'}
    flows.install(tmp_path, ROOT, {'identity': {'name': 'Merchant'}}, 'minimal', offers,
                  review_subjects=subjects)
    raw = (tmp_path / 'storefront-config.js').read_text()
    assert '"OFFER-15": "/product/consulting/"' in raw
    assert '"OFFER-30": "/product/consulting/"' in raw
    assert '"reviewSubjects": {"OFFER-15": "consulting", "OFFER-30": "consulting"}' in raw
