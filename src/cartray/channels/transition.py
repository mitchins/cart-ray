"""Offline cutover record and rollback plan; never changes a channel account."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .feed import google_feed, meta_feed
from .projection import _text, _url, compare


def transition_check(source, candidate, record, previous_artifact, exceptions=()):
    """Bind reported staging evidence and a rollback target to exact snapshots."""
    comparison = compare(source, candidate, exceptions)
    required = {
        "previous_source",
        "previous_url",
        "previous_schedule",
        "previous_artifact_digest",
        "replacement_source",
        "replacement_url",
        "candidate_snapshot_source",
        "staging_url",
        "staging_account",
        "ingestion",
        "evidence_reference",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError("invalid transition record")
    for key in (
        "previous_source",
        "replacement_source",
        "candidate_snapshot_source",
        "previous_schedule",
        "staging_account",
        "evidence_reference",
    ):
        _text(record[key], key, 256)
    for key in ("previous_url", "replacement_url", "staging_url"):
        _url(record[key], key)
    if record["staging_url"] in (record["previous_url"], record["replacement_url"]):
        raise ValueError("staging URL must be distinct from the live and replacement URLs")
    if (
        not isinstance(previous_artifact, bytes)
        or not previous_artifact
        or hashlib.sha256(previous_artifact).hexdigest() != record["previous_artifact_digest"]
    ):
        raise ValueError("retained previous feed artifact is missing or mismatched")
    old_time = datetime.fromisoformat(source["captured_at"].replace("Z", "+00:00"))
    candidate_time = datetime.fromisoformat(candidate["captured_at"].replace("Z", "+00:00"))
    if candidate_time < old_time:
        raise ValueError("candidate snapshot predates source snapshot")
    if record["previous_source"] != source["source"]:
        raise ValueError("previous source differs from snapshot provenance")
    if (
        record["previous_source"].casefold() == record["replacement_source"].strip().casefold()
        or record["replacement_source"] != record["replacement_source"].strip()
    ):
        raise ValueError("replacement must be a separately identified source")
    if (
        record["candidate_snapshot_source"] != candidate["source"]
        or candidate["source"].casefold() == source["source"].casefold()
    ):
        raise ValueError("candidate snapshot must identify distinct provenance")
    # Reusing item IDs in a separate data source in the same account can
    # overwrite live items; only a separate account is safe for ingestion rehearsal.
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,149}", record["staging_account"])
        or record["staging_account"].casefold() == source["account"].casefold()
    ):
        raise ValueError("staging requires a distinct channel account identifier")
    evidence = record["ingestion"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "candidate_digest",
        "artifact_digest",
        "source_id",
        "account",
        "url",
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
        and evidence["url"] == record["staging_url"]
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
            "artifact_sha256": record["previous_artifact_digest"],
            "url_equals_replacement": record["previous_url"] == record["replacement_url"],
            "verify": "Confirm item-level processing after restoring the old source.",
        },
    }
