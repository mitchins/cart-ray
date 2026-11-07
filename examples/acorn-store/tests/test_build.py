"""Reference store builds from its own reviewed synthetic inputs."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from cartray.build_profiles import selected_catalogue_profile
from cartray.catalogue import CsvCatalogueSourceAdapter, FixturePriceResolver, build_catalogue_from_source
from cartray.errors import CatalogueValidationError
from cartray.preflight import preflight_lock_output

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acorn_build", HERE / "build.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_fresh_default_build_is_optional_and_isolated(tmp_path):
    config = module.configuration(HERE / "store.json")
    out = tmp_path / "site"
    module.build(out, config)
    text = (out / "storefront-config.js").read_text()
    data = json.loads(text.split(" = ", 1)[1].split(";\n", 1)[0])
    assert data["checkoutEnabled"] is False and data["reviewsEnabled"] is False
    assert data["reviewInvitationsEnabled"] is False
    assert not (out / "_worker.js").exists()
    assert not (out / "consent.mjs").exists()
    assert [x["amount_minor"] for x in data["previewCatalogue"]["products"]].count(0) == 1
    assert set(data["reviewSubjects"]) == {"ACORN-KIT", "ACORN-PLANTER"}
    assert (out / "products/acorn-growing-kit/index.html").is_file()
    assert (out / "products/woodland-planter/index.html").is_file()
    assert (out / "products/acorn-growing-guide/index.html").is_file()
    assert (out / "review/index.html").is_file() and not (out / "staff").exists()
    with pytest.raises(ValueError, match="already exists"):
        module.build(out, config)


def test_profile_and_explicit_test_api(tmp_path):
    profile = selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "acorn-test"})
    assert profile.catalogue == HERE / "catalogue.csv"
    config = module.configuration(HERE / "store.json")
    lock = fixture_preflight(tmp_path)
    module.build(
        tmp_path / "site", config, checkout_api="https://example.workers.dev", reviews=True, preflight_lock=lock
    )
    built = (tmp_path / "site/storefront-config.js").read_text()
    assert '"checkoutEnabled": true' in built and '"reviewsEnabled": true' in built
    for url in ("http://example.com", "https://a.example/?x=1", "https://u:p@example.com"):
        with pytest.raises(ValueError):
            module.test_api(url)


def fixture_preflight(tmp_path):
    expansions = {k: tuple(v) for k, v in json.loads((HERE / "fulfilment-expansions.json").read_text()).items()}
    catalogue = asyncio.run(
        build_catalogue_from_source(
            CsvCatalogueSourceAdapter(HERE / "catalogue.csv"),
            FixturePriceResolver.from_json(HERE / "price-resolutions.json"),
            expansions,
        )
    )
    path = tmp_path / "test-lock.json"
    path.write_text(json.dumps(preflight_lock_output(catalogue)))
    return path


def test_checkout_fails_closed_without_matching_test_preflight(tmp_path):
    config = module.configuration(HERE / "store.json")
    with pytest.raises(Exception, match="preflight lock"):
        module.build(
            tmp_path / "missing",
            config,
            checkout_api="https://example.workers.dev",
            preflight_lock=tmp_path / "missing.json",
        )
    lock = fixture_preflight(tmp_path)
    data = json.loads(lock.read_text())
    data["prices"][0]["amount_minor"] += 1
    lock.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="approved reference amount"):
        module.build(tmp_path / "wrong-price", config, checkout_api="https://example.workers.dev", preflight_lock=lock)
    assert not (tmp_path / "wrong-price").exists()


def test_test_checkout_uses_preflight_profile_not_fixture_prices():
    profile = selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "acorn-stripe-test"})
    assert profile.price_resolutions is None
    assert profile.preflight_lock == module.PREFLIGHT_LOCK


def test_checkout_rejects_live_mode_and_stale_preflight(tmp_path):
    config = module.configuration(HERE / "store.json")
    lock = fixture_preflight(tmp_path)
    data = json.loads(lock.read_text())
    data["stripe_mode"] = "live"
    lock.write_text(json.dumps(data))
    with pytest.raises(CatalogueValidationError):
        module.build(tmp_path / "live", config, checkout_api="https://example.workers.dev", preflight_lock=lock)
    data["stripe_mode"] = "test"
    data["catalogue_version"] = "sha256:" + "0" * 64
    lock.write_text(json.dumps(data))
    with pytest.raises(CatalogueValidationError, match="reviewed preflight"):
        module.build(tmp_path / "stale", config, checkout_api="https://example.workers.dev", preflight_lock=lock)


def test_invalid_mapping_does_not_build(tmp_path):
    config = module.configuration(HERE / "store.json")
    config["products"][0]["review_subject"] = config["products"][1]["review_subject"]
    path = tmp_path / "store.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="duplicate review subject"):
        module.configuration(path)


def test_review_invitation_choice_is_optional_and_enabled_only_for_test_checkout(tmp_path):
    config = module.configuration(HERE / "store.json")
    lock = fixture_preflight(tmp_path)
    out = tmp_path / "enabled"
    module.build(out, config, checkout_api="https://example.workers.dev", preflight_lock=lock)
    assert '"reviewInvitationsEnabled": true' in (out / "storefront-config.js").read_text()
    assert '"reviewInvitationDefaultChecked": true' in (out / "storefront-config.js").read_text()
    html = (out / "index.html").read_text()
    assert 'id="review-invitation-opt-in" type="checkbox"' in html
    assert "Email me one request to review my purchase." in html
    assert "review-consent" not in html and "verification code" not in html
    config["review_invitations_default_checked"] = False
    unchecked = tmp_path / "unchecked"
    module.build(unchecked, config, checkout_api="https://example.workers.dev", preflight_lock=lock)
    assert '"reviewInvitationDefaultChecked": false' in (unchecked / "storefront-config.js").read_text()
    config["review_invitations"] = "disabled"
    disabled = tmp_path / "disabled"
    module.build(disabled, config, checkout_api="https://example.workers.dev", preflight_lock=lock)
    assert '"reviewInvitationsEnabled": false' in (disabled / "storefront-config.js").read_text()
