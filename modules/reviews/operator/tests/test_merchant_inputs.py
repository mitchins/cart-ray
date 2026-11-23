"""Merchant snapshots must retain checkout authority and build identity."""

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE = Path(__file__).resolve().parents[1]
ROOT = MODULE.parents[2]
sys.path.insert(0, str(MODULE))
from merchant_inputs import commerce_build_hash, snapshot, validate  # noqa: E402
from operate import Installation  # noqa: E402
from topology import graph  # noqa: E402


def merchant(root):
    commerce = root / "commerce"
    commerce.mkdir(parents=True)
    (commerce / "catalogue.csv").write_text("product_key,title\nMERCHANT-ONE,Product\n")
    (commerce / "presentation.csv").write_text("product_key\nMERCHANT-ONE\n")
    (commerce / "fulfilment-expansions.json").write_text("{}")
    (commerce / "product-assets").mkdir()
    site = root / "storefront"
    (site / "theme").mkdir(parents=True)
    (site / "assets").mkdir()
    (site / "assets/one.png").write_bytes(b"fixture")
    config = {"storeName": "Merchant", "checkoutEnabled": True, "previewCatalogue": None,
              "productPaths": {"MERCHANT-ONE": "/products/one/"}}
    theme = {"schemaVersion": 1, "base": "/", "preview": False, "adapter": "/theme/cartray-adapter.mjs",
             "gel": {"identity": {"name": "Merchant"}},
             "catalogue": {"products": [{"product_key": "MERCHANT-ONE", "slug": "one",
                                          "image": "/assets/one.png", "image_alt": "Product", "category": "Goods"}]},
             "browseOffers": [{"sourceId": "one", "productKey": "MERCHANT-ONE"}]}
    (site / "storefront-config.js").write_text("window.CARTRAY_STOREFRONT = " + json.dumps(config) + ";")
    (site / "site-config.js").write_text("window.CARTRAY_THEME = " + json.dumps(theme) + ";")
    for relative, source in {
        "theme/core-storefront.js": "storefront/app.js",
        "theme/cartray-adapter.mjs": "examples/gel-theme-pack/src/cartray-adapter.mjs",
        "theme/connected-browse.mjs": "examples/gel-theme-pack/src/connected-browse.mjs",
        "theme/connected-start.mjs": "examples/gel-theme-pack/src/connected-start.mjs",
        "theme/app.mjs": "examples/gel-theme-pack/src/app.mjs",
        "theme/native-presenter.mjs": "examples/gel-theme-pack/src/native-presenter.mjs",
        "theme/gallery.mjs": "examples/gel-theme-pack/src/gallery.mjs",
    }.items():
        shutil.copyfile(ROOT / source, site / relative)
    for route in ("cart", "purchase-return", "products/one"):
        (site / route).mkdir(parents=True)
        script = "/theme/connected-browse.mjs" if route.startswith("products/") else "/theme/connected-start.mjs"
        page = route if not route.startswith("products/") else "product"
        (site / route / "index.html").write_text(
            f'<body class="theme-minimal" data-cartray-presenter="gel" data-page="{page}">'
            '<script src="/storefront-config.js"></script><script src="/site-config.js"></script>'
            f'<script type="module" src="{script}"></script><div id="content"></div>'
            '<button data-cartray-add="MERCHANT-ONE"></button></body>'
        )
    return root


def test_merchant_snapshot_preserves_inputs_and_detects_changes(tmp_path):
    source = merchant(tmp_path / "source")
    destination = tmp_path / "snapshot"
    fingerprint = snapshot(source, destination, ROOT)
    assert validate(destination, ROOT) == fingerprint
    (source / "commerce/catalogue.csv").write_text("changed")
    assert validate(destination, ROOT) == fingerprint
    page = destination / "storefront/products/one/index.html"
    page.write_text(page.read_text() + "changed")
    assert validate(destination, ROOT) != fingerprint


def test_explicit_merchant_product_path_preserves_identity(tmp_path):
    source = merchant(tmp_path / "source")
    site = source / "storefront"
    config = site / "storefront-config.js"
    config.write_text(config.read_text().replace('/products/one/', '/product/merchant-one/'))
    theme = site / "site-config.js"
    theme.write_text(theme.read_text().replace('"slug": "one"',
                                               '"slug": "one", "path": "/product/merchant-one/"'))
    destination = site / "product/merchant-one"
    destination.parent.mkdir()
    (site / "products/one").rename(destination)
    assert validate(source, ROOT)
    theme.write_text(theme.read_text().replace('/product/merchant-one/', '/product/other/'))
    with pytest.raises(ValueError, match="product route"):
        validate(source, ROOT)


def test_merchant_rejects_unapproved_offers_preview_code_and_symlinks(tmp_path):
    source = merchant(tmp_path / "source")
    config = source / "storefront/storefront-config.js"
    original = config.read_text()
    config.write_text(original.replace("MERCHANT-ONE", "OTHER"))
    with pytest.raises(ValueError, match="authorised catalogue"):
        validate(source, ROOT)
    config.write_text(original)
    adapter = source / "storefront/theme/cartray-adapter.mjs"
    adapter.write_text("preview adapter")
    with pytest.raises(ValueError, match="unrecognised connected code"):
        validate(source, ROOT)
    adapter.unlink()
    adapter.symlink_to(ROOT / "examples/gel-theme-pack/src/cartray-adapter.mjs")
    with pytest.raises(ValueError, match="symlinks"):
        validate(source, ROOT)


def test_merchant_rejects_unconnected_cart_and_product_pages(tmp_path):
    source = merchant(tmp_path / "source")
    cart = source / "storefront/cart/index.html"
    original = cart.read_text()
    cart.write_text("<!doctype html><h1>Preview cart</h1>")
    with pytest.raises(ValueError, match="does not load the connected storefront"):
        validate(source, ROOT)
    cart.write_text(original)
    page = source / "storefront/products/one/index.html"
    page.write_text(page.read_text().replace('data-cartray-add="MERCHANT-ONE"', 'data-cartray-add="OTHER"'))
    with pytest.raises(ValueError, match="connected purchase action"):
        validate(source, ROOT)


def test_merchant_rejects_missing_catalogue_presentation(tmp_path):
    source = merchant(tmp_path / "source")
    config = source / "storefront/site-config.js"
    value = json.loads(config.read_text().removeprefix("window.CARTRAY_THEME = ").removesuffix(";"))
    del value["catalogue"]
    config.write_text("window.CARTRAY_THEME = " + json.dumps(value) + ";")
    with pytest.raises(ValueError, match="presentation is incomplete"):
        validate(source, ROOT)


def plan_merchant(tmp_path, source, *, reviews=False, mapped=False, **options):
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"stripe_mode": "test", "catalogue_version": "sha256:" + "b" * 64}))
    return Installation.plan(tmp_path / "installation", {
        "namespace": "merchant-test", "account_id": "a" * 32, "stripe_account_id": "acct_Test",
        "preflight_lock": str(lock), "reviews_enabled": reviews, "merchant_name": "Merchant",
        "store_key": "merchant-one", "merchant_inputs": str(source),
        "subject_map": {"MERCHANT-ONE": "product-one"} if mapped else {},
        "subject_details": [["product-one", "/products/one/", "Product"]] if mapped else [],
        **options,
    })


def test_operator_pins_merchant_inputs_and_fails_closed_on_change(tmp_path):
    source = merchant(tmp_path / "source")
    installation = plan_merchant(tmp_path, source, mapped=True)
    shutil.rmtree(source)
    resumed = Installation(installation.directory)
    resumed.verify_merchant_inputs()
    identity = resumed.merchant_config_hash()
    resumed.config["review_invitation_default_checked"] = False
    assert resumed.merchant_config_hash() != identity
    resumed.config["review_invitation_default_checked"] = True
    page = Path(resumed.config["merchant_inputs"]) / "storefront/products/one/index.html"
    page.write_text(page.read_text() + "changed")
    with pytest.raises(ValueError, match="merchant inputs changed"):
        resumed.verify_merchant_inputs()


def test_commerce_fingerprint_matches_builder_for_nested_assets(tmp_path):
    source = merchant(tmp_path / "source") / "commerce"
    (source / "product-assets/a").mkdir()
    (source / "product-assets/a/z.png").write_bytes(b"nested")
    (source / "product-assets/a.png").write_bytes(b"sibling")
    spec = importlib.util.spec_from_file_location("commerce_builder", ROOT / "scripts/build_acorn_commerce.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert commerce_build_hash(source, b"lock") == builder.snapshot_merchant_inputs(source, tmp_path / "out", b"lock")


def test_cached_build_and_deploy_reject_changed_merchant_configuration(tmp_path):
    source = merchant(tmp_path / "source")
    p = plan_merchant(tmp_path, source)
    p.vault = lambda: {}
    p.check_schema = lambda: None
    p.state["pages_subdomain"] = "merchant-test.pages.dev"
    p.state["build"] = "test-commit"
    release = p.directory / "releases/test-commit"
    release.mkdir(parents=True)
    (release / "build.json").write_text(json.dumps({
        "merchant_inputs_sha256": p.config["merchant_inputs_sha256"],
        "merchant_lock_sha256": hashlib.sha256(Path(p.config["preflight_lock"]).read_bytes()).hexdigest(),
        "merchant_config_sha256": "old-configuration",
    }))
    with patch("operate.subprocess.check_output", return_value="test-commit\n"), patch("operate.subprocess.run"):
        with pytest.raises(ValueError, match="cached release uses different merchant inputs"):
            p.build()
    with pytest.raises(ValueError, match="release merchant configuration differs"):
        p.deploy()


@pytest.mark.parametrize("shared_mail", [False, True])
def test_merchant_reviews_require_correct_subject_pages_and_native_clients(tmp_path, monkeypatch, shared_mail):
    source = merchant(tmp_path / "source")
    site = source / "storefront"
    page = site / "products/one/index.html"
    page.write_text(page.read_text() + '<section id="reviews"><p id="reviews-status"></p>'
                    '<div id="review-list"></div><button id="reviews-more"></button></section>'
                    '<script type="application/json" id="product-data">{"review_subject":"product-one"}</script>'
                    '<script type="module" src="/theme/connected-public-reviews.mjs"></script>')
    for name, source_file in {
        "theme/native-reviews.js": "modules/reviews/ui/reviews.js",
        "theme/native-manage.js": "modules/reviews/ui/manage.js",
        "theme/review-presenter.mjs": "examples/gel-theme-pack/src/connected-review.mjs",
        "theme/connected-public-reviews.mjs": "examples/gel-theme-pack/src/connected-public-reviews.mjs",
        "theme/reviews.mjs": "modules/reviews/ui/public.mjs",
    }.items():
        shutil.copyfile(ROOT / source_file, site / name)
    (site / "review").mkdir()
    for filename, mode in (("index.html", "review"), ("manage.html", "manage")):
        (site / "review" / filename).write_text(
            f'<section data-native-review="{mode}"><div id="content"></div><p id="message"></p></section>'
            '<dialog id="confirm-dialog"><button data-dialog-cancel></button>'
            '<button data-dialog-confirm></button></dialog>'
            '<script type="module" src="/theme/review-presenter.mjs"></script>'
        )
    installation = plan_merchant(tmp_path, source, reviews=True, mapped=True,
                                 review_invitation_default_checked=False)
    installation.verify_merchant_inputs()
    calls = []

    class Cloud:
        def request(self, *args):
            calls.append(("request", args))

        def query(self, *args):
            calls.append(("query", args))

    installation.api = Cloud()
    installation.credentials = {"cloudflare_api_token": "test-token"}
    installation.state["resources"]["database:commerce"] = {"id": "test-db"}
    if shared_mail:
        installation.config['post_purchase_config'] = 'fixture-config'
        monkeypatch.setattr(installation, 'post_purchase_actions', lambda: {'schema':2})
    else:
        installation.state["resources"]["queue:events"] = {"id": "events"}
        installation.state["resources"]["queue:dlq"] = {"id": "dlq"}
    installation.state["workers_subdomain"] = "workers-test"
    installation.state["pages_subdomain"] = "shop.example.test"
    installation.state["urls"] = {"source": "https://checkout.example.test", "storefront": "https://shop.example.test"}
    installation.run = lambda *args, **kwargs: None
    (installation.directory / "releases/test-release").mkdir(parents=True)
    installation.seed_subjects()
    nodes, _ = graph(installation)
    assert nodes["source"]["vars"]["CARTRAY_REVIEW_INVITATION_DEFAULT"] == "unchecked"
    installation.deploy_storefront({"commit": "test-release"})
    output = installation.directory / "releases/test-release/storefront"
    deployed = (output / "storefront-config.js").read_text()
    assert '"storeName": "Merchant"' in deployed
    assert '"MERCHANT-ONE": "product-one"' in deployed
    assert '"reviewInvitationDefaultChecked": false' in deployed
    assert '"https://checkout.example.test"' in deployed
    assert '"contextPath": null' in (output / "site-config.js").read_text()
    assert (output / "_worker.js").read_bytes() == (ROOT / "modules/reviews/gateway/worker.mjs").read_bytes()
    assert all(call[1][2][0] == "merchant-one" for call in calls if call[0] == "query")
    assert any(call[0] == "request" and "REVIEWS_GATEWAY" in call[1][2]["deployment_configs"]["production"]["env_vars"]
               for call in calls)
    if shared_mail:
        deployments = [call[1][2]['deployment_configs']['production'] for call in calls if call[0]=='request']
        assert deployments[-1]['env_vars']['ORDER_ACCESS_GATEWAY']['value']=='test'
        assert deployments[-1]['services']['CARTRAY_SOURCE']['service']==installation.name('source')
    output_page = output / "products/one/index.html"
    original_output = output_page.read_text()
    output_page.write_text(original_output + "tampered")
    with pytest.raises(ValueError, match="retained merchant storefront changed"):
        installation.deploy_storefront({"commit": "test-release"})
    output_page.write_text(original_output)
    (output.parent / "storefront-integrity.json").unlink()
    installation.deploy_storefront({"commit": "test-release"})
    assert output_page.read_text() == original_output
    installation.config["subject_details"][0][0] = "another-subject"
    with pytest.raises(ValueError, match="unapproved review subject"):
        installation.verify_merchant_inputs()
    installation.config["subject_details"][0][0] = "product-one"
    snap = Path(installation.config["merchant_inputs"])
    manage = snap / "storefront/review/manage.html"
    original_manage = manage.read_text()
    manage.write_text(original_manage.replace("data-dialog-confirm", "data-unrelated"))
    installation.config["merchant_inputs_sha256"] = validate(snap, ROOT)
    with pytest.raises(ValueError, match="review page does not load"):
        installation.verify_merchant_inputs()
    manage.write_text(original_manage)
    category = snap / "storefront/category/example"
    category.mkdir(parents=True)
    (category / "index.html").write_text('<script type="application/json" id="product-data">[]</script>')
    installation.config["merchant_inputs_sha256"] = validate(snap, ROOT)
    installation.verify_merchant_inputs()
    unrelated = snap / "storefront/products/unrelated"
    unrelated.mkdir()
    (unrelated / "index.html").write_text(
        '<script type="application/json" id="product-data">{"review_subject":"product-one"}</script>'
    )
    installation.config["merchant_inputs_sha256"] = validate(snap, ROOT)
    with pytest.raises(ValueError, match="unapproved review subject"):
        installation.verify_merchant_inputs()
    shutil.rmtree(unrelated)
    unrelated = snap / "storefront/product/unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "index.html").write_text(
        '<script type="application/json" id="product-data">{"review_subject":"product-one"}</script>'
    )
    installation.config["merchant_inputs_sha256"] = validate(snap, ROOT)
    with pytest.raises(ValueError, match="unapproved review subject"):
        installation.verify_merchant_inputs()


def test_fulfilment_credentials_are_required_by_resumed_merchant_installation(tmp_path):
    source = merchant(tmp_path / "source")
    installation = plan_merchant(tmp_path, source, fulfilment={"buckets": {}})
    credentials = {"cloudflare_api_token": "operator", "stripe_test_key": "sk_test_fixture"}
    with pytest.raises(ValueError, match="credential handoff is incomplete"):
        Installation(installation.directory, credentials)
    resumed = Installation(installation.directory, {**credentials, "fulfilment_resources": "{}"})
    assert resumed.config["fulfilment"] == {"buckets": {}}

def test_explicit_corrected_build_inputs_preserve_installation_state(tmp_path):
    source = merchant(tmp_path / "source")
    install = plan_merchant(tmp_path, source)
    before = json.loads(json.dumps(install.state))
    (source / "storefront/cart/index.html").write_text(
        (source / "storefront/cart/index.html").read_text() + "<!-- updated -->"
    )
    checksum = validate(source, ROOT)
    partial = install.directory / 'inputs' / ('merchant-' + checksum)
    partial.mkdir()
    (partial / 'interrupted.txt').write_text('incomplete generated snapshot')
    install.update_merchant_inputs(source)
    assert validate(Path(install.config['merchant_inputs']), ROOT) == checksum
    assert install.state["resources"] == before["resources"]
    assert install.state["secret_fingerprints"] == before["secret_fingerprints"]
    assert install.state["activation_authorized"] == before["activation_authorized"]
    assert install.config["merchant_inputs_sha256"] != before["config"]["merchant_inputs_sha256"]
    resumed = Installation(install.directory)
    resumed.verify_merchant_inputs()
    assert resumed.config["merchant_inputs_sha256"] == install.config["merchant_inputs_sha256"]
