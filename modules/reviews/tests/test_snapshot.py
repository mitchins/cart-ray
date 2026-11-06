import hashlib
import json

import pytest
from cartray_reviews.snapshot import SnapshotError, load_snapshot


def snapshot():
    return {
        "schema": "cartray.reviews.snapshot.v1",
        "snapshot_id": "one",
        "source": {"identity": "wp:ep", "extracted_at": "2026-11-05T00:00:00Z", "sha256": "a" * 64, "complete": True},
        "publication_revision": 1,
        "store": "ep",
        "environment": "test",
        "subjects": [{"id": "p", "kind": "product", "path": "/product/"}],
        "contributions": [
            {
                "id": "r",
                "revision_id": "r1",
                "subject_id": "p",
                "parent_id": None,
                "kind": "review",
                "author": "A",
                "body": "Good",
                "rating": 5,
                "date": {"original": "2024-01-02", "precision": "date", "timezone_provenance": "unknown", "utc": None},
                "verification": "legacy_woo_verified_assertion",
                "anchors": ["review-r", "comment-12"],
                "votes": {"up": 2, "down": 1, "net": 1},
            },
            {
                "id": "u",
                "revision_id": "u1",
                "subject_id": "p",
                "parent_id": None,
                "kind": "review",
                "author": "B",
                "body": "Thanks",
                "rating": None,
                "date": {"original": "2024-01-03", "precision": "date", "timezone_provenance": "unknown", "utc": None},
                "verification": "unverified",
                "anchors": ["review-u"],
                "votes": {"up": None, "down": None, "net": None},
            },
            {
                "id": "a",
                "revision_id": "a1",
                "subject_id": "p",
                "parent_id": "r",
                "kind": "reply",
                "author": "Staff",
                "body": "Thanks",
                "rating": None,
                "date": {"original": "2024-01-04", "precision": "date", "timezone_provenance": "unknown", "utc": None},
                "verification": "unverified",
                "anchors": ["reply-a"],
                "votes": {"up": None, "down": None, "net": None},
            },
        ],
        "summaries": [{"subject_id": "p", "count": 1, "sum": 5, "histogram": [0, 0, 0, 0, 1]}],
    }


def read(value):
    raw = json.dumps(value).encode()
    return load_snapshot(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), store="ep", environment="test")


def test_unrated_and_replies_do_not_change_summary():
    assert read(snapshot())["summaries"][0]["count"] == 1


@pytest.mark.parametrize("change", ["private", "boolean", "summary", "anchor", "parent", "revision", "kind"])
def test_invalid_snapshots_are_rejected(change):
    """Reject malformed fields, relationships and unsupported contribution kinds."""
    data = snapshot()
    c = data["contributions"]
    if change == "private":
        c[0]["email"] = "private@example.invalid"
    if change == "boolean":
        c[0]["rating"] = True
    if change == "summary":
        data["summaries"][0]["count"] = 2
    if change == "anchor":
        c[1]["anchors"] = ["comment-12"]
    if change == "parent":
        c[2]["parent_id"] = "missing"
    if change == "revision":
        c[1]["revision_id"] = "r1"
    if change == "kind":
        c[0]["kind"] = "video"
    with pytest.raises(SnapshotError):
        read(data)


def test_digest_and_duplicate_keys_rejected():
    raw = b'{"schema":"one","schema":"two"}'
    with pytest.raises(SnapshotError):
        load_snapshot(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), store="ep", environment="test")
    with pytest.raises(SnapshotError):
        load_snapshot(raw, expected_sha256="0" * 64, store="ep", environment="test")


@pytest.mark.parametrize("path", ["/\\evil.example/", "//evil.example/", "/test/?x=1", "/test/#x", "/../"])
def test_unsafe_paths_rejected(path):
    data = snapshot()
    data["subjects"][0]["path"] = path
    with pytest.raises(SnapshotError):
        read(data)


def test_malformed_summary_and_environment_rejected():
    data = snapshot()
    data["summaries"][0]["subject_id"] = []
    with pytest.raises(SnapshotError):
        read(data)
    data = snapshot()
    data["environment"] = "live"
    with pytest.raises(SnapshotError):
        read(data)


def test_parent_cycle_and_unknown_schema_rejected():
    data = snapshot()
    data["contributions"][2]["parent_id"] = "a"
    with pytest.raises(SnapshotError):
        read(data)
    data = snapshot()
    data["schema"] = "cartray.reviews.snapshot.v2"
    with pytest.raises(SnapshotError):
        read(data)


def test_nonfinite_json_and_noncanonical_date_rejected():
    raw = b'{"x":NaN}'
    with pytest.raises(SnapshotError):
        load_snapshot(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), store="ep", environment="test")
    data = snapshot()
    data["contributions"][0]["date"]["timezone_provenance"] = "invented"
    with pytest.raises(SnapshotError):
        read(data)


def test_original_instant_must_match_utc_and_date_only_has_no_instant():
    data = snapshot()
    data["contributions"][0]["date"] = {
        "original": "2024-01-02T11:00:00+11:00",
        "precision": "second",
        "timezone_provenance": "source_offset",
        "utc": "2024-01-02T00:00:00Z",
    }
    assert read(data)["contributions"][0]["date"]["original"].endswith("+11:00")
    data["contributions"][0]["date"]["utc"] = "2024-01-02T01:00:00Z"
    with pytest.raises(SnapshotError):
        read(data)
    data = snapshot()
    data["contributions"][0]["date"]["utc"] = "2024-01-02T00:00:00Z"
    with pytest.raises(SnapshotError):
        read(data)


def test_unroutable_subject_and_oversized_revision_are_rejected():
    data = snapshot()
    data["subjects"][0]["id"] = "not routable"
    with pytest.raises(SnapshotError):
        read(data)
    data = snapshot()
    data["publication_revision"] = 2**63
    with pytest.raises(SnapshotError):
        read(data)


def test_v2_preserves_title_and_raw_timestamp_provenance():
    data = snapshot()
    data["schema"] = "cartray.reviews.snapshot.v2"
    for item in data["contributions"]:
        item["title"] = "A public title" if item["kind"] == "review" else None
        item["date"]["source_local_raw"] = "2024-01-02 11:00:00"
        item["date"]["source_utc_raw"] = None
    value = read(data)
    assert value["contributions"][0]["title"] == "A public title"
    assert value["contributions"][0]["date"]["source_local_raw"] == "2024-01-02 11:00:00"
    data["contributions"][0]["date"]["source_local_raw"] = "private canary"
    with pytest.raises(SnapshotError):
        read(data)


def test_snapshot_rejects_email_like_public_text():
    data = snapshot()
    data["contributions"][0]["body"] = "Contact 'recipient@example.invalid' for details."
    with pytest.raises(SnapshotError, match="email-like public text"):
        read(data)


def test_email_like_check_accepts_bounded_non_address_text():
    data = snapshot()
    data["contributions"][0]["body"] = "a" * 9999 + "@"
    assert read(data)["contributions"][0]["body"].endswith("@")


def test_source_timestamp_precision_is_not_invented():
    data = snapshot()
    data["contributions"][0]["date"] = {
        "original": "2024-01-02T00:00:00.123Z",
        "precision": "microsecond",
        "timezone_provenance": "source_utc",
        "utc": "2024-01-02T00:00:00.123000Z",
    }
    with pytest.raises(SnapshotError, match="declared precision"):
        read(data)
    data["contributions"][0]["date"] = {
        "original": "2024-01-02T00:00:00Z",
        "precision": "second",
        "timezone_provenance": "source_utc",
        "utc": "2024-01-02T00:00:00.000000Z",
    }
    with pytest.raises(SnapshotError, match="declared precision"):
        read(data)
    data["contributions"][0]["date"] = {
        "original": "2024-01-02T00:00:00.123456Z",
        "precision": "microsecond",
        "timezone_provenance": "source_utc",
        "utc": "2024-01-02T00:00:00.123456Z",
    }
    assert read(data)["contributions"][0]["date"]["original"].endswith(".123456Z")


def test_snapshot_v3_requires_safe_public_attribution():
    data = snapshot()
    data["schema"] = "cartray.reviews.snapshot.v3"
    data["source"]["public_attribution"] = "Example historical reviews"
    for item in data["contributions"]:
        item["title"] = None
        item["date"]["source_local_raw"] = None
        item["date"]["source_utc_raw"] = None
    assert read(data)["source"]["public_attribution"] == "Example historical reviews"
    for bad in ("private@example.invalid", "hello\nworld", "evil\u202elabel", "<script>"):
        data["source"]["public_attribution"] = bad
        with pytest.raises(SnapshotError, match="public source attribution"):
            read(data)


def test_canonical_paths_are_bounded_to_lookup_contract():
    data = snapshot()
    data["subjects"][0]["path"] = "/" + "a" * 2046 + "/"
    assert len(read(data)["subjects"][0]["path"]) == 2048
    data["subjects"][0]["path"] = "/" + "a" * 2047 + "/"
    with pytest.raises(SnapshotError, match="subject path"):
        read(data)
