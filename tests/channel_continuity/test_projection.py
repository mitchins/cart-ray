import copy

import pytest

from cartray.channels.feed import google_feed, meta_feed
from cartray.channels.projection import canonical_digest, compare, validate


def snapshot(merchant="trees", channel="google"):
    return {
        "schema": 1,
        "merchant": merchant,
        "channel": channel,
        "account": "test-account",
        "catalogue": "primary",
        "source": "legacy",
        "captured_at": "2026-11-20T00:00:00Z",
        "coverage": "complete",
        "content_language": "en",
        "feed_label": "AU",
        "items": [
            {
                "item_id": "external-456",
                "merchant_product_id": "variant-blue",
                "landing_url": "https://example.test/product/Thing/?variant=Blue",
                "canonical_url": "https://example.test/product/Thing/",
                "image_url": "https://example.test/Images/Blue.webp",
                "title": "Blue",
                "description": "A blue item",
                "price": "1.25 AUD",
                "availability": "in stock",
                "group_id": "family",
                "published": True,
                "eligible": True,
                "condition": "new",
                "attributes": {},
            },
            {
                "item_id": "external-123",
                "merchant_product_id": "enquiry",
                "landing_url": "https://example.test/enquiry/",
                "canonical_url": "https://example.test/enquiry/",
                "image_url": "https://example.test/enquiry.webp",
                "title": "Enquire",
                "description": "A service",
                "price": "0.00 AUD",
                "availability": "out of stock",
                "group_id": None,
                "published": True,
                "eligible": False,
                "condition": "new",
                "attributes": {},
            },
        ],
    }


@pytest.mark.parametrize("merchant", ["trees", "second-merchant"])
@pytest.mark.parametrize("channel", ["google", "meta"])
def test_deterministic_export_preserves_external_id_and_exact_url(merchant, channel):
    src = snapshot(merchant, channel)
    feed = (google_feed if channel == "google" else meta_feed)(src)
    assert b"external-456" in feed
    assert b"https://example.test/product/Thing/?variant=Blue" in feed
    assert b"external-123" not in feed
    reordered = copy.deepcopy(src)
    reordered["items"].reverse()
    assert feed == (google_feed if channel == "google" else meta_feed)(reordered)


def test_reconcile_requires_explicit_snapshot_bound_exception():
    a = snapshot()
    b = copy.deepcopy(a)
    b["items"][0]["image_url"] = "https://example.test/new.webp"
    report = compare(a, b)
    assert not report["ready"]
    waiver = {
        "source_digest": canonical_digest(a),
        "candidate_digest": canonical_digest(b),
        "item_id": "external-456",
        "field": "image_url",
        "reviewer": "operator",
        "reason": "approved",
    }
    assert compare(a, b, [waiver])["ready"]
    b["items"][0]["price"] = "2.00 AUD"
    with pytest.raises(ValueError, match="snapshot-bound"):
        compare(a, b, [waiver])


@pytest.mark.parametrize(
    "field,value",
    [
        ("landing_url", "javascript:alert(1)"),
        ("eligible", "yes"),
        ("price", "unknown"),
        ("image_url", "https://user:pass@example.test/a"),
    ],
)
def test_invalid_data_fails_closed(field, value):
    data = snapshot()
    data["items"][0][field] = value
    with pytest.raises(ValueError):
        validate(data)


def test_duplicate_external_ids_and_cross_account_comparison_are_rejected():
    src = snapshot()
    dup = copy.deepcopy(src)
    dup["items"][1]["item_id"] = dup["items"][0]["item_id"]
    with pytest.raises(ValueError, match="duplicate"):
        validate(dup)
    other = copy.deepcopy(src)
    other["account"] = "different-account"
    with pytest.raises(ValueError, match="scope"):
        compare(src, other)


def test_cli_refuses_overwrite_and_fails_on_unexplained_change(tmp_path, capsys):
    import json

    from cartray.channels.__main__ import main

    source = tmp_path / "source.json"
    candidate = tmp_path / "candidate.json"
    output = tmp_path / "feed.tsv"
    a = snapshot()
    b = copy.deepcopy(a)
    b["items"][0]["price"] = "2.00 AUD"
    source.write_text(json.dumps(a))
    candidate.write_text(json.dumps(b))
    assert main(["compare", str(source), str(candidate)]) == 1
    assert '"ready": false' in capsys.readouterr().out
    assert main(["export", str(source), str(output)]) == 0
    with pytest.raises(FileExistsError):
        main(["export", str(source), str(output)])


def test_variant_group_and_policy_changes_are_reported():
    a = snapshot()
    b = copy.deepcopy(a)
    b["items"][0]["group_id"] = "other-family"
    b["items"][0]["eligible"] = False
    b["items"][0]["attributes"] = {"size": "large"}
    report = compare(a, b)
    assert {item["field"] for item in report["differences"]} == {"group_id", "eligible", "attributes"}
    assert report["ready"] is False


def test_reject_spreadsheet_formula_and_other_feed_scope():
    source = snapshot()
    source["items"][0]["title"] = "=SUM(A1:A2)"
    with pytest.raises(ValueError, match="formula"):
        validate(source)
    source = snapshot()
    candidate = copy.deepcopy(source)
    candidate["feed_label"] = "NZ"
    with pytest.raises(ValueError, match="scope"):
        compare(source, candidate)


def test_missing_source_values_are_reported_and_never_silently_exported():
    source = snapshot()
    candidate = copy.deepcopy(source)
    source["items"][0]["price"] = None
    report = compare(source, candidate)
    assert report["ready"] is False
    assert any(row.get("unverified") and row["field"] == "price" for row in report["differences"])
    with pytest.raises(ValueError, match="missing channel data"):
        google_feed(source)
