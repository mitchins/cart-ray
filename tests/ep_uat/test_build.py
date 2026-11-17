from __future__ import annotations

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
    ):
        with pytest.raises(ValueError):
            module.SourceMarkupCheck(set()).feed(markup)


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
    product["navigation_path"] = "/products/merchant-item-seven/"
    (source / "catalogue.json").write_text(json.dumps(data))
    output = tmp_path / "output"
    module.build(source, output, "minimal")
    assert (output / "products/merchant-item-seven/index.html").is_file()
    assert 'rel="canonical"' not in (output / "products/merchant-item-seven/index.html").read_text()
    product["navigation_path"] = "/products/../checkout/"
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="navigation"):
        module.build(source, tmp_path / "unsafe", "minimal")


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
