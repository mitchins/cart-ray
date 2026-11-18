"""Existing-site/private-source opt-in must not alter default Pages installs."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from operate import Installation, validate_config  # noqa: E402
from test_merchant_inputs import merchant, plan_merchant  # noqa: E402
from test_provision import Catalog  # noqa: E402
from topology import graph, wrangler_config  # noqa: E402

EXTERNAL = {
    "storefront_origin": "https://shop.example.test",
    "success_path": "/purchase-return/",
    "cancel_path": "/cart/",
    "source_mode": "private",
    "source_api_path": "/api/commerce",
    "webhook_url": "https://shop.example.test/api/commerce/stripe/webhook",
}


def external(tmp_path):
    return plan_merchant(tmp_path, merchant(tmp_path / "source"), **EXTERNAL)


@pytest.mark.parametrize("field,value", [
    ("storefront_origin", "http://shop.example.test"),
    ("storefront_origin", "https://shop.example.test/"),
    ("storefront_origin", "https://user:pass@shop.example.test"),
    ("storefront_origin", "https://shop.example.test?x=1"),
    ("storefront_origin", 1), ("webhook_url", "https://shop.example.test/api#x"),
    ("webhook_url", "https://elsewhere.example.test/stripe/webhook"),
    ("webhook_url", "https://shop.example.test//stripe/webhook"),
    ("success_path", "//other.example.test/"), ("cancel_path", "/../checkout/"),
    ("success_path", "//"), ("cancel_path", "/cart//"),
    ("success_path", "/return/?session_id=bad"), ("source_api_path", "/"),
    ("source_api_path", "/api/commerce/"), ("source_mode", "live"),
])
def test_invalid_external_contract(tmp_path, field, value):
    installation = external(tmp_path)
    with pytest.raises(ValueError):
        validate_config({**installation.config, field: value})


@pytest.mark.parametrize("missing", ["storefront_origin", "webhook_url", "source_api_path"])
def test_private_source_requires_explicit_gateway_contract(tmp_path, missing):
    config = external(tmp_path).config.copy()
    del config[missing]
    with pytest.raises(ValueError, match="private source requires"):
        validate_config(config)


def test_default_pages_topology_unchanged(tmp_path):
    p = plan_merchant(tmp_path, merchant(tmp_path / "source"))
    p.state.update(pages_subdomain="assigned.pages.dev", workers_subdomain="account")
    p.state["resources"]["database:commerce"] = {"id": "db"}
    nodes, urls = graph(p)
    assert nodes["source"]["public"] is True
    assert urls["source"] == "https://merchant-test-source.account.workers.dev"
    assert urls["storefront"] == "https://assigned.pages.dev"
    assert nodes["source"]["vars"]["CARTRAY_SUCCESS_URL"] == "https://assigned.pages.dev/purchase-return/?checkout=complete"


def test_private_topology_and_fingerprint(tmp_path):
    p = external(tmp_path)
    p.state["workers_subdomain"] = "account"
    p.state["resources"]["database:commerce"] = {"id": "db"}
    nodes, urls = graph(p)
    assert not nodes["source"]["public"]
    assert urls["source"] == "https://shop.example.test/api/commerce"
    assert urls["storefront"] == EXTERNAL["storefront_origin"]
    assert nodes["source"]["vars"]["CARTRAY_CANCEL_URL"] == "https://shop.example.test/cart/?checkout=cancelled"
    assert 'workers_dev = false' in wrangler_config(p, "source", nodes["source"])
    before = p.merchant_config_hash()
    p.config["cancel_path"] = "/purchase-return/"
    assert before != p.merchant_config_hash()


def test_external_provision_skips_pages_and_uses_explicit_webhook(tmp_path):
    p = external(tmp_path)
    p.credentials = {"stripe_test_key": "sk_test_fixture"}
    p.api = Catalog()
    endpoints = []

    def stripe(key, path, data=None, idempotency=None):
        if path == "account":
            return {"id": "acct_Test"}
        endpoints.append(data["url"])
        return {"id": "we_fixture", "secret": "whsec_fixture"}

    with patch("operate.stripe", side_effect=stripe):
        p.provision()
        p.provision()
    assert endpoints == [EXTERNAL["webhook_url"]]
    assert p.api.items["pages/projects"] == []
    assert set(p.state["resources"]) == {"database:commerce", "worker:source"}


def test_external_export_retains_all_browse_html_without_pages_calls(tmp_path):
    source = merchant(tmp_path / "source")
    # One authorised product plus 117 display-only pages requires no new browse model.
    for index in range(117):
        page = source / f"storefront/products/browse-{index}/index.html"
        page.parent.mkdir(parents=True)
        page.write_text(f"<h1>Approved browse-only {index}</h1>")
    p = plan_merchant(tmp_path, source, **EXTERNAL)
    p.api = None  # Any accidental provider request fails this test.
    p.state["urls"] = {"source": EXTERNAL["storefront_origin"] + EXTERNAL["source_api_path"]}
    p.deploy_storefront({"commit": "fixture"})
    output = p.directory / "releases/fixture/storefront"
    assert len(list((output / "products").glob("*/index.html"))) == 118
    for index in range(117):
        relative = f"products/browse-{index}/index.html"
        assert (output / relative).read_bytes() == (source / "storefront" / relative).read_bytes()
    assert EXTERNAL["storefront_origin"] + EXTERNAL["source_api_path"] in (output / "storefront-config.js").read_text()
    assert p.state["external_storefront"]["deployment"] == "external-operator-required"
    p.deploy_storefront({"commit": "fixture"})
    assert Installation(p.directory).state["external_storefront"] == p.state["external_storefront"]
    theme = (output / "site-config.js").read_text().removeprefix("window.CARTRAY_THEME = ").strip().removesuffix(";")
    assert json.loads(theme)["contextPath"] is None


def backend_for_visibility(p, visibility):
    p.state["workers_subdomain"] = "account"
    p.state["resources"]["database:commerce"] = {"id": "db"}
    p.state["deployment"] = {"versions": {"source": "v1"}}
    nodes, _ = graph(p)
    bindings = [{**b, **({"database_id": b["id"]} if b["type"] == "d1" else {})}
                for b in nodes["source"]["bindings"]]
    p.api = SimpleNamespace(request=lambda *args: visibility,
                            version=lambda name: {"id": "v1", "resources": {"bindings": bindings}})
    p.check_schema = lambda: None
    p.vault = lambda: {}
    p.pin_suppression = lambda: None
    p.verifier = lambda mode: {"status": "active", "revision": 0}


@pytest.mark.parametrize("visibility", [
    {"enabled": True, "previews_enabled": False},
    {"enabled": False, "previews_enabled": True}, {},
])
@pytest.mark.parametrize("reviews", [False, True])
def test_doctor_and_verify_reject_public_private_source(tmp_path, visibility, reviews):
    p = external(tmp_path)
    # Visibility contract must also be enforced by the Reviews-enabled verify path.
    backend_for_visibility(p, visibility)
    assert p.doctor()["healthy"] is False
    p.config["reviews_enabled"] = reviews
    with pytest.raises(ValueError, match="visibility|commerce deployment differs"):
        p.verify()
    assert p.state["phase"] == "planned"


def test_doctor_accepts_private_visibility_and_pages_default_is_unchanged(tmp_path):
    p = external(tmp_path)
    backend_for_visibility(p, {"enabled": False, "previews_enabled": False})
    assert p.doctor()["healthy"] is True
    p.config.pop("source_mode")
    p.api.request = lambda *args: pytest.fail("default public mode must not enforce private visibility")
    assert p.private_source_visibility_errors() == []


@pytest.mark.parametrize("private", [False, True])
def test_rollback_enforces_private_flags_before_and_after_publish_only_when_opted_in(tmp_path, private):
    p = external(tmp_path)
    backend_for_visibility(p, {"enabled": False, "previews_enabled": False})
    if not private:
        p.config.pop("source_mode")
    p.state.update(migration_ledgers={}, secret_fingerprints={"recipient_hash": "fingerprint"},
                   releases=[{"schema": {}, "suppression_key_fingerprint": "fingerprint",
                              "versions": {"source": "v0"}, "commit": "old-commit"}])
    p.prepare_queue_transition = lambda: None
    p.recover_paused_hints = lambda: None
    p.configure_queues = lambda **kwargs: None
    calls = []

    def request(path, method="GET", body=None):
        assert path == f"workers/scripts/{p.name('source')}/subdomain"
        calls.append((method, body))
        return {"enabled": False, "previews_enabled": False}

    p.api.request = request
    p.api.publish = lambda name, version: calls.append(("publish", version))
    p.rollback(0)
    off = {"enabled": False, "previews_enabled": False}
    assert calls == ([("POST", off), ("publish", "v0"), ("POST", off), ("GET", None)]
                     if private else [("publish", "v0")])


def test_rollback_fails_closed_if_provider_keeps_private_source_public(tmp_path):
    p = external(tmp_path)
    backend_for_visibility(p, {"enabled": True, "previews_enabled": True})
    p.state.update(migration_ledgers={}, secret_fingerprints={"recipient_hash": "fingerprint"},
                   releases=[{"schema": {}, "suppression_key_fingerprint": "fingerprint",
                              "versions": {"source": "v0"}, "commit": "old-commit"}])
    p.prepare_queue_transition = lambda: None
    p.api.publish = lambda *args: None
    p.verifier = lambda mode: pytest.fail("visibility must be checked before verification")
    with pytest.raises(ValueError, match="source.visibility"):
        p.rollback(0)
    assert p.state["phase"] == "rolling-back"
