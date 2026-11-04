"""Validation of a pinned, approved public snapshot; not an approval mechanism."""

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal


class SnapshotError(ValueError):
    """An invalid, unsupported, or unexpected snapshot."""


def _fail(message):
    """Reject a snapshot with a bounded diagnostic."""
    raise SnapshotError(message)


def _object(value, keys):
    """Require exactly the explicitly allowed object fields."""
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail("unexpected object fields")


def _string(value):
    """Require a nonempty, bounded string."""
    if not isinstance(value, str) or not value.strip() or len(value) > 10000:
        _fail("invalid string")


def _unique_pairs(pairs):
    """Reject duplicate object keys while decoding JSON."""
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    """Reject JSON extensions such as NaN and Infinity."""
    _fail("non-finite number")


def _utc_time(value):
    """Validate a UTC timestamp without silently changing its precision."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value):
        _fail("expected UTC timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError("invalid UTC timestamp") from exc


def _source_date(value):
    """Retain source strings and declared precision without guessing an offset."""
    _object(value, ("original", "precision", "timezone_provenance", "utc"))
    _string(value["original"])
    if value["precision"] not in ("date", "second", "microsecond", "unknown"):
        _fail("invalid date precision")
    if value["timezone_provenance"] not in ("source_utc", "source_offset", "source_local", "unknown"):
        _fail("invalid timezone provenance")
    if value["precision"] == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["original"]):
            _fail("invalid original date")
        try:
            datetime.strptime(value["original"], "%Y-%m-%d")
        except ValueError as exc:
            raise SnapshotError("invalid original date") from exc
        if value["utc"] is not None:
            _fail("date-only source cannot establish an instant")
    if value["utc"] is not None:
        if value["timezone_provenance"] not in ("source_utc", "source_offset"):
            _fail("UTC value without authoritative timezone")
        _utc_time(value["utc"])
        try:
            original = datetime.fromisoformat(value["original"].replace("Z", "+00:00"))
            utc = datetime.fromisoformat(value["utc"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise SnapshotError("invalid original timestamp") from exc
        if original.tzinfo is None or original != utc:
            _fail("original timestamp does not match UTC")


def load_snapshot(data: bytes, *, expected_sha256: str, store: str, environment: str):
    """Load exact pinned bytes; caller must independently approve the artifact and pin.

    Public prose must be reviewed for sensitive information before approval. Hashing
    proves byte identity, not authorship, privacy, freshness, or publication consent.
    """
    if not isinstance(data, bytes) or len(data) > 10_000_000:
        _fail("invalid snapshot size")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or hashlib.sha256(data).hexdigest() != expected_sha256:
        _fail("snapshot digest mismatch")
    try:
        payload = json.loads(
            data, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant, parse_float=Decimal
        )
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise SnapshotError("invalid JSON") from exc
    _object(
        payload,
        (
            "schema",
            "snapshot_id",
            "store",
            "environment",
            "source",
            "publication_revision",
            "subjects",
            "contributions",
            "summaries",
        ),
    )
    if (
        payload["schema"] != "cartray.reviews.snapshot.v1"
        or payload["store"] != store
        or payload["environment"] != environment
    ):
        _fail("snapshot identity mismatch")
    for key in ("snapshot_id", "store", "environment"):
        _string(payload[key])
    _object(payload["source"], ("identity", "extracted_at", "sha256", "complete"))
    source = payload["source"]
    _string(source["identity"])
    if not isinstance(source["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"]):
        _fail("invalid source digest")
    if source["complete"] is not True:
        _fail("source is not complete")
    _utc_time(source["extracted_at"])
    if type(payload["publication_revision"]) is not int or payload["publication_revision"] < 1:
        _fail("invalid publication revision")
    if environment not in ("test", "live"):
        _fail("invalid environment")
    for key in ("subjects", "contributions", "summaries"):
        if not isinstance(payload[key], list):
            _fail("expected list")
    subjects = {}
    for item in payload["subjects"]:
        _object(item, ("id", "kind", "path"))
        for value in item.values():
            _string(value)
        if item["kind"] not in ("product", "service", "store") or item["id"] in subjects:
            _fail("invalid subject")
        path = item["path"]
        if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*", path) or "//" in path:
            _fail("invalid subject path")
        subjects[item["id"]] = item
    contributions = {}
    anchors = set()
    for item in payload["contributions"]:
        _object(
            item,
            (
                "id",
                "revision_id",
                "subject_id",
                "parent_id",
                "kind",
                "author",
                "body",
                "rating",
                "date",
                "verification",
                "anchors",
                "votes",
            ),
        )
        for key in ("id", "revision_id", "subject_id", "kind", "author", "body", "verification"):
            _string(item[key])
        if item["id"] in contributions or item["subject_id"] not in subjects:
            _fail("duplicate contribution or unknown subject")
        if item["kind"] not in ("review", "reply") or item["verification"] not in (
            "cartray_verified_acquisition",
            "legacy_woo_verified_assertion",
            "legacy_cusrev_assertion",
            "email_only",
            "unverified",
        ):
            _fail("unsupported contribution or verification")
        if item["parent_id"] is not None:
            _string(item["parent_id"])
        if (item["kind"] == "review") != (item["parent_id"] is None):
            _fail("invalid parent")
        rating = item["rating"]
        if rating is not None and (type(rating) is not int or not 1 <= rating <= 5 or item["kind"] != "review"):
            _fail("invalid rating")
        _source_date(item["date"])
        _object(item["votes"], ("up", "down", "net"))
        votes = item["votes"]
        for key in ("up", "down", "net"):
            if votes[key] is not None and type(votes[key]) is not int:
                _fail("invalid vote count")
        if any(votes[key] is not None and votes[key] < 0 for key in ("up", "down")):
            _fail("invalid vote count")
        if votes["up"] is not None and votes["down"] is not None and votes["net"] is not None:
            if votes["up"] - votes["down"] != votes["net"]:
                _fail("inconsistent vote counts")
        if not isinstance(item["anchors"], list) or not item["anchors"]:
            _fail("missing anchors")
        for anchor in item["anchors"]:
            if (
                not isinstance(anchor, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", anchor)
                or anchor in anchors
            ):
                _fail("invalid or duplicate anchor")
            anchors.add(anchor)
        contributions[item["id"]] = item
    revisions = [item["revision_id"] for item in contributions.values()]
    if len(set(revisions)) != len(revisions):
        _fail("duplicate revision")
    for item in contributions.values():
        seen = set()
        current = item
        while current["parent_id"] is not None:
            parent = current["parent_id"]
            if (
                parent in seen
                or parent not in contributions
                or contributions[parent]["subject_id"] != item["subject_id"]
            ):
                _fail("invalid parent graph")
            seen.add(parent)
            current = contributions[parent]
    summaries = {}
    for summary in payload["summaries"]:
        _object(summary, ("subject_id", "count", "sum", "histogram"))
        sid = summary["subject_id"]
        _string(sid)
        if sid not in subjects or sid in summaries:
            _fail("invalid summary subject")
        ratings = [
            x["rating"]
            for x in contributions.values()
            if x["subject_id"] == sid and x["kind"] == "review" and x["rating"] is not None
        ]
        histogram = [ratings.count(i) for i in range(1, 6)]
        if (
            type(summary["count"]) is not int
            or type(summary["sum"]) is not int
            or summary["count"] != len(ratings)
            or summary["sum"] != sum(ratings)
            or summary["histogram"] != histogram
            or any(type(x) is not int for x in summary["histogram"])
        ):
            _fail("summary mismatch")
        summaries[sid] = summary
    if set(summaries) != set(subjects):
        _fail("missing subject summary")
    return payload
