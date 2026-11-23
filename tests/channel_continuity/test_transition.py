import copy
import hashlib
import json

import pytest
from test_projection import snapshot

from cartray.channels.feed import google_feed
from cartray.channels.projection import canonical_digest
from cartray.channels.transition import transition_check


def test_transition_requires_item_level_evidence_in_separate_account_and_records_rollback(tmp_path, capsys):
    source = snapshot()
    candidate = copy.deepcopy(source)
    candidate["source"] = "candidate-build"
    record = {
        "previous_source": "legacy",
        "previous_url": "https://shop.example.test/old.tsv",
        "previous_schedule": "daily",
        "previous_artifact_digest": hashlib.sha256(b"prior feed").hexdigest(),
        "replacement_source": "new-feed",
        "replacement_url": "https://shop.example.test/new.tsv",
        "staging_account": "staging-account",
        "candidate_snapshot_source": "candidate-build",
        "staging_url": "https://staging.example.test/feed.tsv",
        "evidence_reference": "synthetic-receipt",
        "ingestion": {
            "candidate_digest": canonical_digest(candidate),
            "artifact_digest": hashlib.sha256(google_feed(candidate)).hexdigest(),
            "source_id": "new-feed",
            "account": "staging-account",
            "url": "https://staging.example.test/feed.tsv",
            "accepted_ids": ["external-456"],
            "rejected_ids": [],
        },
    }
    report = transition_check(source, candidate, record, b"prior feed")
    assert report["ready_for_owner_review"]
    from cartray.channels.__main__ import _run

    paths = [tmp_path / name for name in ("source.json", "candidate.json", "record.json", "previous.tsv")]
    for path, value in zip(paths[:3], (source, candidate, record), strict=True):
        path.write_text(json.dumps(value))
    paths[3].write_bytes(b"prior feed")
    assert _run(["transition-check", *(str(path) for path in paths)]) == 0
    assert json.loads(capsys.readouterr().out)["ready_for_owner_review"]
    assert report["rollback"]["source"] == "legacy"
    assert report["rollback"]["url"].endswith("/old.tsv")
    assert "not independently verified" in report["evidence_basis"]
    record["ingestion"]["accepted_ids"] = []
    assert not transition_check(source, candidate, record, b"prior feed")["ready_for_owner_review"]
    record["staging_account"] = source["account"]
    with pytest.raises(ValueError, match="distinct channel account"):
        transition_check(source, candidate, record, b"prior feed")
    for ambiguous in (source["account"].upper(), source["account"] + " "):
        record["staging_account"] = ambiguous
        with pytest.raises(ValueError, match="distinct channel account"):
            transition_check(source, candidate, record, b"prior feed")


def test_transition_rejects_stale_candidate_and_live_staging():
    source = snapshot()
    candidate = copy.deepcopy(source)
    candidate["source"] = "candidate-build"
    record = {
        "previous_source": "legacy",
        "previous_url": "https://shop.example.test/old.tsv",
        "previous_schedule": "daily",
        "previous_artifact_digest": hashlib.sha256(b"old").hexdigest(),
        "replacement_source": "new-feed",
        "replacement_url": "https://shop.example.test/old.tsv",
        "staging_account": "separate",
        "staging_url": "https://shop.example.test/old.tsv",
        "candidate_snapshot_source": "candidate-build",
        "evidence_reference": "test",
        "ingestion": {
            "candidate_digest": canonical_digest(candidate),
            "artifact_digest": hashlib.sha256(google_feed(candidate)).hexdigest(),
            "source_id": "new-feed",
            "account": "separate",
            "url": "https://shop.example.test/old.tsv",
            "accepted_ids": ["external-456"],
            "rejected_ids": [],
        },
    }
    with pytest.raises(ValueError, match="staging URL"):
        transition_check(source, candidate, record, b"old")
    record["staging_url"] = "https://staging.example.test/feed.tsv"
    candidate["captured_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="predates"):
        transition_check(source, candidate, record, b"old")
