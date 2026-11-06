import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from cartray_reviews.snapshot import SnapshotError, load_snapshot

_spec = importlib.util.spec_from_file_location(
    "wordpress_migration_adapter", Path(__file__).resolve().parents[1] / "migration/wordpress/adapter.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
build_draft = _module.build_draft


def candidate():
    return {
        "schema": "restricted.review-candidate.v1",
        "complete": False,
        "site": "https://example.invalid",
        "captured_utc": "2026-11-05T00:00:00Z",
        "comments": [
            {
                "comment_ID": "1",
                "comment_type": "review",
                "comment_approved": "1",
                "comment_parent": "0",
                "comment_post_ID": "99",
                "comment_author": "Example",
                "comment_content": "Useful",
                "comment_date": "2024-01-02 11:00:00",
                "comment_date_gmt": "2024-01-02 00:00:00",
                "comment_author_email": "private-canary@example.invalid",
            }
        ],
        "commentmeta": [
            {"comment_id": "1", "meta_key": "rating", "meta_value": "5"},
            {"comment_id": "1", "meta_key": "title", "meta_value": "Title"},
        ],
    }


def test_incomplete_draft_excludes_private_source_and_requires_explicit_mapping():
    raw = json.dumps(candidate()).encode()
    mapping = {"99": {"id": "subject", "kind": "service", "path": "/service/"}}
    result = build_draft(
        raw, mapping, store="test-store", environment="test", expected_source="https://example.invalid"
    )
    result = result["snapshot"]
    assert result["source"]["complete"] is False
    assert result["contributions"][0]["title"] == "Title"
    assert result["contributions"][0]["date"]["source_local_raw"] == "2024-01-02 11:00:00"
    assert "private-canary" not in json.dumps(result)
    encoded = json.dumps(result).encode()
    with pytest.raises(SnapshotError):
        load_snapshot(
            encoded,
            expected_sha256=hashlib.sha256(encoded).hexdigest(),
            store="test-store",
            environment="test",
        )
    with pytest.raises(ValueError):
        build_draft(raw, {}, store="test-store", environment="test", expected_source="https://example.invalid")


def test_rejects_unknown_populated_metadata():
    source = candidate()
    source["commentmeta"].append({"comment_id": "1", "meta_key": "unknown", "meta_value": "populated"})
    with pytest.raises(ValueError):
        build_draft(
            json.dumps(source).encode(),
            {"99": {"id": "subject", "kind": "service", "path": "/service/"}},
            store="test-store",
            environment="test",
            expected_source="https://example.invalid",
        )


def test_draft_revisions_change_with_material_votes_and_reject_duplicate_keys():
    source = candidate()
    mapping = {"99": {"id": "subject", "kind": "service", "path": "/service/"}}
    first = build_draft(
        json.dumps(source).encode(),
        mapping,
        store="test-store",
        environment="test",
        expected_source="https://example.invalid",
    )
    source["commentmeta"].append({"comment_id": "1", "meta_key": "ivole_review_votes", "meta_value": "1"})
    second = build_draft(
        json.dumps(source).encode(),
        mapping,
        store="test-store",
        environment="test",
        expected_source="https://example.invalid",
    )
    assert first["snapshot"]["contributions"][0]["revision_id"] != second["snapshot"]["contributions"][0]["revision_id"]
    with pytest.raises(ValueError):
        build_draft(
            b'{"schema":"x","schema":"y"}',
            mapping,
            store="test-store",
            environment="test",
            expected_source="https://example.invalid",
        )


def test_draft_reports_restricted_metadata_and_rejects_orphan_or_duplicate():
    source = candidate()
    source["commentmeta"].append({"comment_id": "1", "meta_key": "flag_count", "meta_value": "2"})
    mapping = {"99": {"id": "subject", "kind": "service", "path": "/service/"}}
    result = build_draft(
        json.dumps(source).encode(),
        mapping,
        store="test-store",
        environment="test",
        expected_source="https://example.invalid",
    )
    assert result["report"]["restricted_archive_only_field_counts"]["flag_count"] == 1
    source["comments"].append(dict(source["comments"][0]))
    with pytest.raises(ValueError):
        build_draft(
            json.dumps(source).encode(),
            mapping,
            store="test-store",
            environment="test",
            expected_source="https://example.invalid",
        )
    source["comments"].pop()
    orphan = dict(source["comments"][0], comment_ID="2", comment_type="comment", comment_parent="missing")
    source["comments"].append(orphan)
    with pytest.raises(ValueError):
        build_draft(
            json.dumps(source).encode(),
            mapping,
            store="test-store",
            environment="test",
            expected_source="https://example.invalid",
        )


def test_invalid_raw_timestamp_is_not_projected():
    source = candidate()
    source["comments"][0]["comment_date"] = "private data canary"
    mapping = {"99": {"id": "subject", "kind": "service", "path": "/service/"}}
    with pytest.raises(ValueError):
        build_draft(
            json.dumps(source).encode(),
            mapping,
            store="test-store",
            environment="test",
            expected_source="https://example.invalid",
        )
