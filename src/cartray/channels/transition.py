"""Offline cutover record and rollback plan; never changes a channel account."""

from __future__ import annotations

import hashlib

from .feed import google_feed, meta_feed
from .projection import _text, _url, compare


def transition_check(source, candidate, record, exceptions=()):
    """Bind reported staging evidence and a rollback target to exact snapshots."""
    comparison = compare(source, candidate, exceptions)
    required = {
        "previous_source",
        "previous_url",
        "previous_schedule",
        "replacement_source",
        "replacement_url",
        "staging_account",
        "ingestion",
        "evidence_reference",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError("invalid transition record")
    for key in ("previous_source", "replacement_source", "previous_schedule", "staging_account", "evidence_reference"):
        _text(record[key], key, 256)
    for key in ("previous_url", "replacement_url"):
        _url(record[key], key)
    if record["previous_source"] != source["source"]:
        raise ValueError("previous source differs from snapshot provenance")
    if record["previous_source"] == record["replacement_source"]:
        raise ValueError("replacement must be a separately identified source")
    # Reusing item IDs in a separate data source in the same account can
    # overwrite live items; only a separate account is safe for ingestion rehearsal.
    if record["staging_account"] == source["account"]:
        raise ValueError("staging requires a separate channel account")
    evidence = record["ingestion"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "candidate_digest",
        "artifact_digest",
        "source_id",
        "account",
        "accepted_ids",
        "rejected_ids",
    }:
        raise ValueError("invalid ingestion evidence")
    if not isinstance(evidence["accepted_ids"], list) or not isinstance(evidence["rejected_ids"], list):
        raise ValueError("invalid ingestion item statuses")
    if any(not isinstance(value, str) for value in evidence["accepted_ids"] + evidence["rejected_ids"]):
        raise ValueError("invalid ingestion item identity")
    eligible = {item["item_id"] for item in candidate["items"] if item["published"] and item["eligible"]}
    accepted = evidence["accepted_ids"]
    artifact = (
        (google_feed if candidate["channel"] == "google" else meta_feed)(candidate)
        if comparison["candidate_locally_exportable"]
        else None
    )
    verified = (
        evidence["candidate_digest"] == comparison["candidate_digest"]
        and artifact is not None
        and evidence["artifact_digest"] == hashlib.sha256(artifact).hexdigest()
        and evidence["source_id"] == record["replacement_source"]
        and evidence["account"] == record["staging_account"]
        and len(accepted) == len(set(accepted))
        and set(accepted) == eligible
        and not evidence["rejected_ids"]
    )
    return {
        "ready_for_owner_review": comparison["ready"] and verified,
        "evidence_basis": "operator-reported staging evidence; not independently verified by this tool",
        "comparison": comparison,
        "rollback": {
            "source": record["previous_source"],
            "url": record["previous_url"],
            "schedule": record["previous_schedule"],
            "verify": "Confirm item-level processing after restoring the old source.",
        },
    }
