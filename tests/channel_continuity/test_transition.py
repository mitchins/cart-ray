import copy
import hashlib

import pytest
from test_projection import snapshot

from cartray.channels.feed import google_feed
from cartray.channels.projection import canonical_digest
from cartray.channels.transition import transition_check


def test_transition_requires_item_level_evidence_in_separate_account_and_records_rollback():
    source = snapshot()
    candidate = copy.deepcopy(source)
    record = {
        "previous_source": "legacy",
        "previous_url": "https://shop.example.test/old.tsv",
        "previous_schedule": "daily",
        "replacement_source": "new-feed",
        "replacement_url": "https://shop.example.test/new.tsv",
        "staging_account": "staging-account",
        "evidence_reference": "synthetic-receipt",
        "ingestion": {
            "candidate_digest": canonical_digest(candidate),
            "artifact_digest": hashlib.sha256(google_feed(candidate)).hexdigest(),
            "source_id": "new-feed",
            "account": "staging-account",
            "accepted_ids": ["external-456"],
            "rejected_ids": [],
        },
    }
    report = transition_check(source, candidate, record)
    assert report["ready_for_owner_review"]
    assert report["rollback"]["source"] == "legacy"
    assert report["rollback"]["url"].endswith("/old.tsv")
    assert "not independently verified" in report["evidence_basis"]
    record["ingestion"]["accepted_ids"] = []
    assert not transition_check(source, candidate, record)["ready_for_owner_review"]
    record["staging_account"] = source["account"]
    with pytest.raises(ValueError, match="separate channel account"):
        transition_check(source, candidate, record)
