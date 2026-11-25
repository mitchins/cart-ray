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
from merchant_inputs import commerce_build_hash, javascript_json, snapshot, tree_hash, validate  # noqa: E402
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
        with patch.object(p, 'render', side_effect=ValueError('new input-specific build')):
            with pytest.raises(ValueError, match="new input-specific build"):
                p.build()
    assert json.loads((release / 'build.json').read_text())['merchant_config_sha256'] == 'old-configuration'
    with pytest.raises(ValueError, match="release merchant configuration differs"):
        p.deploy()


@pytest.mark.parametrize("timing", ["receipt", "followup", "both"])
@pytest.mark.parametrize("shared_mail", [False, True])
def test_merchant_reviews_require_correct_subject_pages_and_native_clients(tmp_path, monkeypatch, shared_mail, timing):
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
                                 review_invitation_default_checked=False, review_requests={"timing":timing})
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
        installation.config['source_api_path'] = '/api'
        installation.config['post_purchase_config'] = 'fixture-config'
        monkeypatch.setattr(installation, 'post_purchase_actions', lambda: {'schema':2})
    installation.state["workers_subdomain"] = "workers-test"
    installation.state["pages_subdomain"] = "shop.example.test"
    installation.state["urls"] = {"source": "https://checkout.example.test", "storefront": "https://shop.example.test"}
    installation.run = lambda *args, **kwargs: None
    (installation.directory / "releases/test-release").mkdir(parents=True)
    installation.seed_subjects()
    nodes, _ = graph(installation)
    assert nodes["source"]["vars"]["CARTRAY_REVIEW_INVITATION_DEFAULT"] == "unchecked"
    assert nodes["source"]["vars"]["CARTRAY_REVIEW_INVITATION_MODE"] == ("automatic" if shared_mail else "disabled")
    installation.deploy_storefront({"commit": "test-release"})
    output = installation.directory / "releases/test-release/storefront"
    deployed = (output / "storefront-config.js").read_text()
    assert '"reviewInvitationWordingVersion": "review-invitation-v3"' in deployed
    assert "I'm happy to receive invitations to review products I purchase." in deployed
    assert "CARTRAY_REVIEW_INVITATION_LIMIT" not in nodes["source"]["vars"]
    assert '"storeName": "Merchant"' in deployed
    assert '"MERCHANT-ONE": "product-one"' in deployed
    assert '"reviewInvitationDefaultChecked": false' in deployed
    assert '"reviewInvitationsEnabled": ' + ("true" if shared_mail else "false") in deployed
    assert '"https://checkout.example.test"' in deployed
    assert '"contextPath": null' in (output / "site-config.js").read_text()
    assert (output / "_worker.js").read_bytes() == (ROOT / "modules/reviews/gateway/worker.mjs").read_bytes()
    assert all(call[1][2][0] == "merchant-one" for call in calls if call[0] == "query")
    assert any(call[0] == "request" and "REVIEWS_GATEWAY" in call[1][2]["deployment_configs"]["production"]["env_vars"]
               for call in calls)
    if shared_mail:
        deployments = [call[1][2]['deployment_configs']['production'] for call in calls if call[0]=='request']
        assert deployments[-1]['env_vars']['ORDER_ACCESS_GATEWAY']['value']=='test'
        assert deployments[-1]['env_vars']['CARTRAY_SOURCE_API_PATH']['value']=='/api'
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


def catalogue_revision(root, *, second=True):
    """Real compiler inputs, with no payment/provider calls or fabricated runtime authority."""
    import asyncio

    from cartray.catalogue import CsvCatalogueSourceAdapter, FixturePriceResolver, build_catalogue_from_source
    from cartray.models import ResolvedPrice
    from cartray.preflight import preflight_lock_output

    package = merchant(root)
    commerce = package / 'commerce'
    products = ['ONE', 'TWO'] if second else ['ONE']
    (commerce / 'catalogue.csv').write_text(
        'product_key,title,stripe_lookup_key,fulfilment_type,fulfilment_version,max_quantity,active\n' +
        ''.join(f'MERCHANT-{key},Product {key},lookup_{key.lower()},download,dummy-v1,10,true\n' for key in products))
    (commerce / 'presentation.csv').write_text('product_key,short_description,image_key\n' +
        ''.join(f'MERCHANT-{key},Dummy product,cover.webp\n' for key in products))
    shutil.copyfile(ROOT / 'storefront/assets/products/test-template-cover-v1.webp',
                    commerce / 'product-assets/cover.webp')
    (commerce / 'fulfilment-expansions.json').write_text('{"dummy-v1":["dummy-file"]}')
    prices = FixturePriceResolver({f'lookup_{key.lower()}': ResolvedPrice('price_' + key, 100, 'aud')
                                   for key in products})
    catalogue = asyncio.run(build_catalogue_from_source(
        CsvCatalogueSourceAdapter(commerce / 'catalogue.csv'), prices, {'dummy-v1': ('dummy-file',)}))
    lock = root.parent / 'revised-lock.json'
    lock.write_text(json.dumps(preflight_lock_output(catalogue)))
    if second:
        site = package / 'storefront'
        path = site / 'storefront-config.js'
        config = javascript_json(path, 'window.CARTRAY_STOREFRONT = ')
        config['productPaths']['MERCHANT-TWO'] = '/products/two/'
        path.write_text('window.CARTRAY_STOREFRONT = ' + json.dumps(config) + ';')
        path = site / 'site-config.js'
        theme = javascript_json(path, 'window.CARTRAY_THEME = ')
        theme['catalogue']['products'].append({**theme['catalogue']['products'][0],
                                              'product_key': 'MERCHANT-TWO', 'slug': 'two'})
        theme['browseOffers'].append({'sourceId': 'two', 'productKey': 'MERCHANT-TWO'})
        path.write_text('window.CARTRAY_THEME = ' + json.dumps(theme) + ';')
        (site / 'products/two').mkdir()
        (site / 'products/two/index.html').write_text(
            (site / 'products/one/index.html').read_text().replace('MERCHANT-ONE', 'MERCHANT-TWO'))
    return {'merchant_inputs': str(package), 'preflight_lock': str(lock),
            'subject_map': {f'MERCHANT-{key}': 'product-' + key.lower() for key in products},
            'subject_details': [['product-' + key.lower(), '/products/' + key.lower() + '/',
                                 'Product' if key == 'ONE' else key]
                                for key in products]}


def test_catalogue_refresh_compiles_resumes_and_preserves_installation(tmp_path):
    source = merchant(tmp_path / 'original')
    install = plan_merchant(tmp_path, source, mapped=True)
    install.state.update(resources={'database:commerce': {'id': 'existing-d1'}},
                         secret_fingerprints={'recipient_hash': 'original-key'},
                         post_purchase_watermark=17, activation_watermark=17,
                         phase='paused', automation_paused=True, activation_authorized=True)
    original = install.catalogue_config()
    install.state['deployment'] = {'commit': 'old', 'complete': True, 'catalogue_config': None}
    install.state['releases'] = [{'commit': 'old', 'merchant_inputs_sha256': original['merchant_inputs_sha256'],
                                  'merchant_config_sha256': install.merchant_config_hash(),
                                  'merchant_lock_sha256': hashlib.sha256(
                                      Path(install.config['preflight_lock']).read_bytes()).hexdigest()}]
    install.save()
    before = json.loads(install.state_path.read_text())
    values = catalogue_revision(tmp_path / 'replacement')
    result = install.reconfigure_catalogue(values)
    resumed = Installation(install.directory)
    resumed.verify_merchant_inputs()
    first = json.loads(install.state_path.read_text())
    assert first['resources'] == before['resources']
    assert first['secret_fingerprints'] == before['secret_fingerprints']
    assert first['post_purchase_watermark'] == 17
    assert first['activation_watermark'] == 17
    assert first['activation_authorized'] is True
    assert first['automation_paused'] is True
    assert first['deployment']['catalogue_config'] == original
    assert first['releases'][0]['catalogue_config'] == original
    assert Path(install.directory / original['preflight_lock']).is_file()
    assert result['resources_changed'] is False
    assert result['sending'] == 'disabled'
    assert 'MERCHANT-TWO' in resumed.config['subject_map']
    resumed.reconfigure_catalogue(values)
    assert json.loads(install.state_path.read_text()) == first


@pytest.mark.parametrize('failure', ['live', 'digest', 'keys', 'identity', 'remap', 'active'])
def test_catalogue_refresh_rejects_bad_revision_without_inventory_changes(tmp_path, failure):
    from cloud import CloudError

    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'), mapped=True)
    values = catalogue_revision(tmp_path / 'replacement')
    lock = Path(values['preflight_lock'])
    data = json.loads(lock.read_text())
    if failure == 'live':
        data['stripe_mode'] = 'live'
    elif failure == 'digest':
        data['catalogue_version'] = 'sha256:' + '0' * 64
    elif failure == 'keys':
        data['prices'].pop()
    elif failure == 'identity':
        values['account_id'] = 'b' * 32
    elif failure == 'remap':
        values['subject_map']['MERCHANT-ONE'] = 'product-two'
    elif failure == 'active':
        install.state.update(activation_authorized=True, automation_paused=False)
        install.save()
    lock.write_text(json.dumps(data))
    before = install.state_path.read_bytes()
    with pytest.raises((ValueError, CloudError)):
        install.reconfigure_catalogue(values)
    assert install.state_path.read_bytes() == before
    assert Installation(install.directory).config['subject_map'] == {'MERCHANT-ONE': 'product-one'}
    if failure in ('live', 'digest', 'keys'):
        log = install.directory / 'logs/catalogue-preflight.log'
        assert log.is_file()
        assert log.stat().st_mode & 0o777 == 0o600
        assert log.read_text()


@pytest.mark.parametrize("private_resources", [False, True])
def test_same_source_rebuild_retains_both_catalogues_and_rollback_storefront(tmp_path, monkeypatch, private_resources):
    from types import SimpleNamespace

    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'), mapped=True)
    install.state.update(workers_subdomain='fixture', pages_subdomain='merchant-test.pages.dev',
                         secret_fingerprints={'recipient_hash': 'key'},
                         migration_ledgers={}, resources={'database:commerce': {'id': 'existing'}})
    monkeypatch.setattr(install, 'vault', lambda: {})
    monkeypatch.setattr(install, 'check_schema', lambda: None)
    monkeypatch.setattr(install, 'pin_suppression', lambda: None)
    monkeypatch.setattr('operate.subprocess.check_output', lambda *args, **kwargs: 'a' * 40)
    import subprocess
    real_subprocess = subprocess.run
    monkeypatch.setattr('operate.subprocess.run', lambda args, **kwargs:
                        SimpleNamespace(returncode=0) if args == ['git', 'diff', '--quiet', 'HEAD']
                        else real_subprocess(args, **kwargs))
    original_run = install.run

    def tools(step, command, environment=None):
        if step != 'build-source':
            return original_run(step, command, environment)
        output = Path(command[command.index('--output') + 1])
        (output / 'bundle').mkdir(parents=True)
        (output / 'bundle/worker.py').write_text('# test artifact')
        lock = Path(install.config['preflight_lock']).read_bytes()
        (output / 'receipt.json').write_text(json.dumps({
            'source_commit': 'a' * 40, 'catalogue_lock_sha256': hashlib.sha256(lock).hexdigest(),
            'merchant_input_sha256': commerce_build_hash(Path(install.config['merchant_inputs']) / 'commerce', lock)}))
    monkeypatch.setattr(install, 'run', tools)
    if private_resources:
        install.config['fulfilment'] = {'buckets': {}}
        install.state['config']['fulfilment'] = {'buckets': {}}
        install.credentials = {'fulfilment_resources': '{}'}
    first = install.build()
    if private_resources:
        install.credentials['fulfilment_resources'] = json.dumps({
            'extra': {'provider': 'external', 'url': 'https://example.test/private'}})
        changed_private = install.build()
        assert changed_private['commit'] == first['commit']
        assert changed_private['release_id'] != first['release_id']
        assert changed_private['resource_config_sha256'] != first['resource_config_sha256']
        install.credentials['fulfilment_resources'] = '{}'
    first_path = install.directory / 'releases' / first['release_id'] / 'build.json'
    first_bytes = first_path.read_bytes()
    # A completed release retains its actually published storefront bytes.
    for_build = install.directory / 'releases' / first['release_id']
    (for_build / 'storefront').mkdir()
    (for_build / 'storefront/.operator-build-complete').write_text(first['commit'])
    (for_build / 'storefront/index.html').write_text('retained catalogue ONE')
    (for_build / 'storefront-integrity.json').write_text(json.dumps({
        'sha256': tree_hash(for_build / 'storefront')}))
    values = catalogue_revision(tmp_path / 'replacement')
    install.reconfigure_catalogue(values)
    second = install.build()
    assert first['commit'] == second['commit']
    assert first['release_id'] != second['release_id']
    assert first_path.read_bytes() == first_bytes
    assert install.build() == second
    install.state['releases'] = [{**first, 'versions': {'source': 'v1'}, 'sending': False},
                                 {**second, 'versions': {'source': 'v2'}, 'sending': False}]
    published = []
    install.api = SimpleNamespace(publish=lambda name, version: published.append(version))
    monkeypatch.setattr(install, 'verifier', lambda mode: {'revision': 0})
    storefront = []
    monkeypatch.setattr(install, 'deploy_storefront', lambda release: storefront.append(
        (release['release_id'], dict(install.config['subject_map']))))
    target_config = install.state['releases'][0].pop('catalogue_config')
    with pytest.raises(ValueError, match='rollback catalogue snapshot'):
        install.rollback(0)
    assert published == []
    install.state['releases'][0]['catalogue_config'] = target_config
    monkeypatch.setattr('operate.validate_merchant_inputs', lambda *args: pytest.fail(
        'archived rollback must not validate clients against the current checkout'))
    install.rollback(0)
    assert published == ['v1']
    assert storefront == [(first['release_id'], {'MERCHANT-ONE': 'product-one'})]
    assert install.state['build'] == first['release_id']
    assert install.state['deployment']['release_id'] == first['release_id']
    assert install.state['resources'] == {'database:commerce': {'id': 'existing'}}


@pytest.mark.parametrize('change', ['path', 'label', 'mapping'])
def test_removed_subject_identity_cannot_be_reinterpreted_when_reintroduced(tmp_path, change):
    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'), mapped=True)
    original = install.catalogue_config()
    install.state['releases'] = [{'commit': 'retained', 'catalogue_config': original}]
    install.use_catalogue_config({**original, 'subject_map': {}, 'subject_details': []})
    install.save()
    values = catalogue_revision(tmp_path / 'replacement')
    if change == 'mapping':
        values['subject_map']['MERCHANT-ONE'] = 'product-two'
    elif change == 'path':
        values['subject_details'][0][1] = '/products/moved/'
    else:
        values['subject_details'][0][2] = 'Changed name'
    before = install.state_path.read_bytes()
    with pytest.raises(ValueError, match='existing'):
        install.reconfigure_catalogue(values)
    assert install.state_path.read_bytes() == before


def test_retired_subject_path_cannot_be_claimed_by_a_new_subject(tmp_path):
    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'), mapped=True)
    previous = install.catalogue_config()
    install.state['releases'] = [{'commit': 'old', 'catalogue_config': previous}]
    install.use_catalogue_config({**previous, 'subject_map': {}, 'subject_details': []})
    candidate = {**install.config, 'subject_map': {'MERCHANT-TWO': 'product-two'},
                 'subject_details': [['product-two', '/products/one/', 'New product']]}
    with pytest.raises(ValueError, match='retained canonical path'):
        install.check_catalogue_subjects(candidate)


def test_catalogue_bindings_keep_prior_order_manifest_mappings(tmp_path):
    from topology import catalogue_subject_maps

    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'), mapped=True)
    original = install.catalogue_config()
    install.state['releases'] = [{'commit': 'old', 'versions': {'source': 'v1'}, 'catalogue_config': original}]
    values = catalogue_revision(tmp_path / 'replacement')
    install.reconfigure_catalogue(values)
    mappings = catalogue_subject_maps(install)
    original_version = 'sha256:' + 'b' * 64
    new_version = json.loads(Path(install.config['preflight_lock']).read_text())['catalogue_version']
    assert mappings[original_version] == {'MERCHANT-ONE': 'product-one'}
    assert mappings[new_version] == values['subject_map']
    assert catalogue_subject_maps(install, {'source': 'v1'}) == {original_version: original['subject_map']}
    install.use_catalogue_config({**install.catalogue_config(), 'subject_map': {}, 'subject_details': []})
    install.state['releases'].append({'catalogue_config': {**install.catalogue_config(),
                                                        'subject_map': values['subject_map']}})
    with pytest.raises(ValueError, match='one catalogue version'):
        catalogue_subject_maps(install)


def test_shared_parent_page_preserves_each_offer_action_and_subject_identity(tmp_path):
    values = catalogue_revision(tmp_path / 'package')
    source = Path(values['merchant_inputs'])
    site = source / 'storefront'
    config_path = site / 'storefront-config.js'
    config = javascript_json(config_path, 'window.CARTRAY_STOREFRONT = ')
    config['productPaths']['MERCHANT-TWO'] = '/products/one/'
    config['reviewSubjects'] = {'MERCHANT-ONE': 'product-one', 'MERCHANT-TWO': 'product-one'}
    config_path.write_text('window.CARTRAY_STOREFRONT = ' + json.dumps(config) + ';')
    theme_path = site / 'site-config.js'
    theme = javascript_json(theme_path, 'window.CARTRAY_THEME = ')
    theme['catalogue']['products'][1]['path'] = '/products/one/'
    theme_path.write_text('window.CARTRAY_THEME = ' + json.dumps(theme) + ';')
    page = site / 'products/one/index.html'
    page.write_text(page.read_text() + '<button data-cartray-add="MERCHANT-TWO"></button>')
    assert validate(source, ROOT)
    install = plan_merchant(tmp_path, source)
    install.config.update(subject_map=config['reviewSubjects'],
                          subject_details=[['product-one', '/products/one/', 'Product']])
    install.verify_merchant_inputs()
    install.config['subject_map'] = {'MERCHANT-ONE': 'different', 'MERCHANT-TWO': 'different'}
    with pytest.raises(ValueError, match='declaration differs'):
        install.verify_merchant_inputs()
    install.config['subject_map'] = {'MERCHANT-ONE': 'product-one', 'MERCHANT-TWO': 'different'}
    with pytest.raises(ValueError, match='identical review subject'):
        install.verify_merchant_inputs()
    page.write_text(page.read_text().replace('<button data-cartray-add="MERCHANT-TWO"></button>', ''))
    with pytest.raises(ValueError, match='purchase action'):
        validate(source, ROOT)


def test_recorded_again_old_source_keeps_its_original_manifest_binding(tmp_path):
    from topology import catalogue_subject_maps

    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'), mapped=True)
    first = install.catalogue_config()
    install.state['releases'] = [{'versions': {'source': 'v1'}, 'catalogue_config': first}]
    values = catalogue_revision(tmp_path / 'replacement')
    install.reconfigure_catalogue(values)
    second = install.catalogue_config()
    install.state['releases'].extend([
        {'versions': {'source': 'v2'}, 'catalogue_config': second},
        {'versions': {'source': 'v1'}, 'catalogue_config': first}])
    original_version = 'sha256:' + 'b' * 64
    assert catalogue_subject_maps(install, {'source': 'v1'}) == {original_version: first['subject_map']}
    assert len(catalogue_subject_maps(install, {'source': 'v2'})) == 2


def test_compiler_uses_retained_snapshot_if_external_source_changes(tmp_path, monkeypatch):
    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'))
    values = catalogue_revision(tmp_path / 'replacement')
    source = Path(values['merchant_inputs'])
    checksum = validate(source, ROOT)
    cached = install.directory / 'inputs' / ('merchant-' + checksum)
    snapshot(source, cached, ROOT)
    original = install.snapshot_catalogue_inputs

    def snapshot_then_external_changes(*args):
        result = original(*args)
        path = source / 'commerce/catalogue.csv'
        path.write_text(path.read_text().replace('Product ONE', 'Changed externally'))
        return result

    monkeypatch.setattr(install, 'snapshot_catalogue_inputs', snapshot_then_external_changes)
    install.reconfigure_catalogue(values)
    assert install.config['merchant_inputs_sha256'] == checksum
    assert 'Product ONE' in (Path(install.config['merchant_inputs']) / 'commerce/catalogue.csv').read_text()
    assert validate(Path(install.config['merchant_inputs']), ROOT) == checksum


def test_legacy_mapping_conflict_is_rejected_before_inventory_save(tmp_path):
    values = catalogue_revision(tmp_path / 'package')
    install = plan_merchant(tmp_path, Path(values['merchant_inputs']), mapped=True)
    Path(install.config['preflight_lock']).write_bytes(Path(values['preflight_lock']).read_bytes())
    install.state['releases'] = [{
        'commit': 'legacy', 'merchant_inputs_sha256': install.config['merchant_inputs_sha256'],
        'merchant_config_sha256': install.merchant_config_hash(),
        'merchant_lock_sha256': hashlib.sha256(Path(install.config['preflight_lock']).read_bytes()).hexdigest()}]
    install.save()
    before = install.state_path.read_bytes()
    with pytest.raises(ValueError, match='one catalogue version'):
        install.reconfigure_catalogue(values)
    assert install.state_path.read_bytes() == before
    assert 'catalogue_config' not in install.state['releases'][0]


@pytest.mark.parametrize('cap', [0, -1, True, 1.5, 101, None])
def test_merchant_rejects_invalid_presentation_quantity_cap(tmp_path, cap):
    source = merchant(tmp_path / 'source')
    path = source / 'storefront/site-config.js'
    theme = javascript_json(path, 'window.CARTRAY_THEME = ')
    theme['catalogue']['products'][0]['max_quantity'] = cap
    path.write_text('window.CARTRAY_THEME = ' + json.dumps(theme) + ';')
    with pytest.raises(ValueError, match='maximum quantity'):
        validate(source, ROOT)


@pytest.mark.parametrize('cap', [1, 3])
def test_merchant_accepts_per_product_presentation_quantity_cap(tmp_path, cap):
    source = merchant(tmp_path / 'source')
    path = source / 'storefront/site-config.js'
    theme = javascript_json(path, 'window.CARTRAY_THEME = ')
    theme['catalogue']['products'][0]['max_quantity'] = cap
    path.write_text('window.CARTRAY_THEME = ' + json.dumps(theme) + ';')
    assert validate(source, ROOT)
