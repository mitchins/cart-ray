"""Reference store builds from its own reviewed synthetic inputs."""

import importlib.util
import json
from pathlib import Path

import pytest

from cartray.build_profiles import selected_catalogue_profile

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
    module.build(tmp_path / "site", config, checkout_api="https://example.workers.dev", reviews=True)
    built = (tmp_path / "site/storefront-config.js").read_text()
    assert '"checkoutEnabled": true' in built and '"reviewsEnabled": true' in built
    for url in ("http://example.com", "https://a.example/?x=1", "https://u:p@example.com"):
        with pytest.raises(ValueError):
            module.test_api(url)


def test_invalid_mapping_does_not_build(tmp_path):
    config = module.configuration(HERE / "store.json")
    config["products"][0]["review_subject"] = config["products"][1]["review_subject"]
    path = tmp_path / "store.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="duplicate review subject"):
        module.configuration(path)
