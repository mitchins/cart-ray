"""Attested rehearsal must stay byte-pinned, scoped and test-only."""

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_wordpress_reconciliation import MAPPING, build_draft, source_candidate
from test_wordpress_reconciliation import MODULE as RECONCILIATION

BASE = Path(__file__).resolve().parents[1] / "migration/wordpress"
sys.path.insert(0, str(BASE))
SPEC = importlib.util.spec_from_file_location("qualification", BASE / "qualify_test_snapshot.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_inputs():
    source = source_candidate()
    for index, row in enumerate(source["commentmeta"]):
        row["meta_id"] = str(index + 1)
    raw = json.dumps(source).encode()
    draft = build_draft(
        raw,
        MAPPING,
        store="s",
        environment="test",
        expected_source="https://example.invalid",
        redact_source_emails=True,
    )
    report = RECONCILIATION.reconcile(
        raw, draft, MAPPING, expected_source="https://example.invalid", redact_source_emails=True
    )
    mapping, draft_bytes, report_bytes = (json.dumps(x).encode() for x in (MAPPING, draft, report))
    evidence = b"Independent review: limited comment graph only."

    def h(value):
        return hashlib.sha256(value).hexdigest()

    attestation = {
        "schema": "cartray.reviews.test-scope-attestation.v1",
        "scope": MODULE.SCOPE,
        "source_sha256": h(raw),
        "mapping_sha256": h(mapping),
        "draft_sha256": h(draft_bytes),
        "report_sha256": h(report_bytes),
        "review_evidence_sha256": h(evidence),
        "public_attribution": "Example legacy reviews",
        "store": "s",
        "environment": "test",
    }
    return raw, mapping, draft_bytes, report_bytes, evidence, attestation


def test_pinned_scoped_qualification_keeps_original_incomplete():
    raw, mapping, draft, report, evidence, attestation = make_inputs()
    snapshot, receipt = MODULE.qualify(
        raw,
        mapping,
        draft,
        report,
        evidence,
        attestation,
        expected_source="https://example.invalid",
        store="s",
        public_attribution="Example legacy reviews",
    )
    value = json.loads(snapshot)
    assert value["environment"] == "test" and value["source"]["complete"] is True
    assert value["schema"] == "cartray.reviews.snapshot.v4"
    assert value["subjects"][0]["aliases"] == ["wp-99"]
    assert value["source"]["identity"] == "wordpress-review-comment-graph:https://example.invalid"
    assert value["source"]["public_attribution"] == "Example legacy reviews"
    assert json.loads(draft)["snapshot"]["source"]["complete"] is False
    assert json.loads(report)["complete"] is False and receipt["production_approval"] is False


def test_rejects_unpinned_evidence_or_live_scope():
    raw, mapping, draft, report, evidence, attestation = make_inputs()
    with pytest.raises(ValueError, match="attestation mismatch"):
        MODULE.qualify(
            raw,
            mapping,
            draft,
            report,
            evidence + b"changed",
            attestation,
            expected_source="https://example.invalid",
            store="s",
            public_attribution="Example legacy reviews",
        )


def test_unused_mapping_does_not_create_unobserved_alias():
    raw, mapping, draft, report, evidence, attestation = make_inputs()
    expanded = json.loads(mapping)
    expanded["999"] = dict(expanded["99"])
    mapping = json.dumps(expanded).encode()
    attestation["mapping_sha256"] = hashlib.sha256(mapping).hexdigest()
    snapshot, _receipt = MODULE.qualify(
        raw,
        mapping,
        draft,
        report,
        evidence,
        attestation,
        expected_source="https://example.invalid",
        store="s",
        public_attribution="Example legacy reviews",
    )
    assert json.loads(snapshot)["subjects"][0]["aliases"] == ["wp-99"]
    with pytest.raises(ValueError, match="attestation mismatch"):
        MODULE.qualify(
            raw,
            mapping,
            draft,
            report,
            evidence,
            attestation,
            expected_source="https://example.invalid",
            store="s",
            public_attribution="Unreviewed label",
        )
    live = copy.deepcopy(attestation)
    live["environment"] = "live"
    with pytest.raises(ValueError, match="attestation mismatch"):
        MODULE.qualify(
            raw,
            mapping,
            draft,
            report,
            evidence,
            live,
            expected_source="https://example.invalid",
            store="s",
            public_attribution="Example legacy reviews",
        )


def test_qualified_snapshot_identity_changes_with_reviewed_attribution():
    raw, mapping, draft, report, evidence, attestation = make_inputs()
    first, _ = MODULE.qualify(
        raw,
        mapping,
        draft,
        report,
        evidence,
        attestation,
        expected_source="https://example.invalid",
        store="s",
        public_attribution="Example legacy reviews",
    )
    attestation["public_attribution"] = "Another reviewed label"
    second, _ = MODULE.qualify(
        raw,
        mapping,
        draft,
        report,
        evidence,
        attestation,
        expected_source="https://example.invalid",
        store="s",
        public_attribution="Another reviewed label",
    )
    assert json.loads(first)["snapshot_id"] != json.loads(second)["snapshot_id"]
