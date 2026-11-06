"""Prepare an explicitly attested review-comment graph for local test only.

The attestation is operator-supplied evidence, not an authentication mechanism
or production approval. Original candidate and report remain incomplete.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from cartray_reviews.snapshot import _reject_constant, _unique_pairs, load_snapshot
from reconcile import reconcile

SCOPE = "wordpress_review_comment_graph_test_only"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def qualify(
    raw, mapping_bytes, draft_bytes, report_bytes, evidence, attestation, *, expected_source, store, public_attribution
):
    """Recheck exact inputs and a human-reviewed test-only scope attestation."""
    if any(not isinstance(x, bytes) for x in (raw, mapping_bytes, draft_bytes, report_bytes, evidence)):
        raise ValueError("inputs must be bytes")
    if (
        len(raw) > 10_000_000
        or len(mapping_bytes) > 1_000_000
        or len(draft_bytes) > 10_000_000
        or len(report_bytes) > 10_000_000
        or not 1 <= len(evidence) <= 100_000
    ):
        raise ValueError("input exceeds limit")
    expected = {
        "schema": "cartray.reviews.test-scope-attestation.v1",
        "scope": SCOPE,
        "source_sha256": digest(raw),
        "mapping_sha256": digest(mapping_bytes),
        "draft_sha256": digest(draft_bytes),
        "report_sha256": digest(report_bytes),
        "review_evidence_sha256": digest(evidence),
        "public_attribution": public_attribution,
        "store": store,
        "environment": "test",
    }
    if attestation != expected:
        raise ValueError("test scope attestation mismatch")
    mapping = json.loads(mapping_bytes, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    draft = json.loads(draft_bytes, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    report = json.loads(report_bytes, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    if draft["snapshot"]["store"] != store or draft["snapshot"]["environment"] != "test":
        raise ValueError("test destination mismatch")
    if reconcile(raw, draft, mapping, expected_source=expected_source, redact_source_emails=True) != report:
        raise ValueError("independent reconciliation mismatch")
    snapshot = draft["snapshot"]
    snapshot["schema"] = "cartray.reviews.snapshot.v4"
    snapshot["source"]["identity"] = "wordpress-review-comment-graph:" + expected_source
    snapshot["source"]["public_attribution"] = public_attribution
    snapshot["source"]["complete"] = True
    observed = {
        str(row["comment_post_ID"])
        for row in json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)["comments"]
        if str(row["comment_approved"]) == "1"
    }
    for subject in snapshot["subjects"]:
        source_ids = [key for key, value in mapping.items() if key in observed and value["id"] == subject["id"]]
        if any(not isinstance(key, str) or not key.isascii() or not key.isdecimal() for key in source_ids):
            raise ValueError("invalid WordPress subject identity")
        subject["aliases"] = sorted({"wp-" + key for key in source_ids})
    identity_bytes = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    snapshot["snapshot_id"] = "test-" + digest(identity_bytes)[:32]
    result = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    load_snapshot(result, expected_sha256=digest(result), store=store, environment="test")
    return result, expected | {"snapshot_sha256": digest(result), "production_approval": False}


def main():
    parser = argparse.ArgumentParser()
    for name in ("source", "mapping", "draft", "report", "evidence", "attestation"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--public-attribution", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()

    def read(path, limit):
        with path.open("rb") as file:
            data = file.read(limit + 1)
        if len(data) > limit:
            raise ValueError("input exceeds limit")
        return data

    raw, mapping, draft, report, evidence, attestation = (
        read(args.source, 10_000_000),
        read(args.mapping, 1_000_000),
        read(args.draft, 10_000_000),
        read(args.report, 10_000_000),
        read(args.evidence, 100_000),
        read(args.attestation, 100_000),
    )
    attestation = json.loads(attestation, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    snapshot, receipt = qualify(
        raw,
        mapping,
        draft,
        report,
        evidence,
        attestation,
        expected_source=args.expected_source,
        store=args.store,
        public_attribution=args.public_attribution,
    )
    args.output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in (("snapshot.json", snapshot), ("qualification.json", json.dumps(receipt, indent=2).encode())):
        fd = os.open(args.output_directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
    print("Scoped test snapshot created; no production approval or publication.")


if __name__ == "__main__":
    main()
