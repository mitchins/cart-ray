"""Create a restricted, explicitly incomplete WordPress migration draft.

This adapter never connects to WordPress, approves a source, or writes to D1.
"""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime

from cartray_reviews.snapshot import _reject_constant, _unique_pairs, load_snapshot


def _redact_email(value, email):
    """Conservatively replace every literal source-author email occurrence."""
    if not isinstance(value, str) or not isinstance(email, str) or "@" not in email:
        return value
    email = email.strip()
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", email):
        return value
    return re.sub(re.escape(email), "[email removed]", value, flags=re.IGNORECASE)


def build_draft(
    raw: bytes, mapping: dict, *, store: str, environment: str, expected_source: str, redact_source_emails: bool = False
):
    """Map a candidate export using explicit subject mappings; reject ambiguity."""
    if type(redact_source_emails) is not bool:
        raise ValueError("invalid redaction policy")
    if not isinstance(raw, bytes) or len(raw) > 10_000_000:
        raise ValueError("candidate export exceeds size limit")
    source = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    if source.get("schema") != "restricted.review-candidate.v1" or source.get("complete") is not False:
        raise ValueError("expected an explicitly incomplete candidate export")
    if not expected_source or source.get("site") != expected_source:
        raise ValueError("candidate source identity mismatch")
    comments = source["comments"]
    if (
        not isinstance(comments, list)
        or len(comments) > 10_000
        or not isinstance(source["commentmeta"], list)
        or len(source["commentmeta"]) > 100_000
    ):
        raise ValueError("candidate export exceeds row limits")
    metadata = defaultdict(lambda: defaultdict(list))
    for row in source["commentmeta"]:
        metadata[str(row["comment_id"])][row["meta_key"]].append(row["meta_value"])
    roots = [row for row in comments if row["comment_type"] == "review" and row["comment_approved"] == "1"]
    root_ids = {str(row["comment_ID"]) for row in roots}
    replies = [row for row in comments if str(row["comment_parent"]) in root_ids and row["comment_approved"] == "1"]
    if len(root_ids) != len(roots):
        raise ValueError("duplicate review source identity")
    selected = roots + replies
    if len(selected) > 200:
        raise ValueError("approved candidate exceeds the draft limit")
    selected_ids = {str(row["comment_ID"]) for row in selected}
    if len(selected_ids) != len(selected):
        raise ValueError("duplicate approved source identity")
    if any(row["comment_approved"] == "1" and str(row["comment_ID"]) not in selected_ids for row in comments):
        raise ValueError("unmapped approved source record")
    archive_counts = defaultdict(int)
    privacy_redactions = []
    contributions = []
    used_subjects = {}
    for row in roots + replies:
        source_id = str(row["comment_ID"])
        subject = mapping.get(str(row["comment_post_ID"]))
        if not isinstance(subject, dict) or set(subject) != {"id", "kind", "path"}:
            raise ValueError("an explicit subject mapping is required for every source post")
        prior = used_subjects.setdefault(subject["id"], subject)
        if prior != subject:
            raise ValueError("conflicting subject mapping")
        parent = str(row["comment_parent"])
        if parent != "0" and parent not in root_ids:
            raise ValueError("unsupported or orphaned reply")
        if not row["comment_author"].strip() or not row["comment_content"].strip():
            raise ValueError("blank author or content requires review")
        if "<" in row["comment_content"] or ">" in row["comment_content"]:
            raise ValueError("source HTML requires an explicit sanitization disposition")
        fields = metadata[source_id]
        supported = {
            "rating",
            "verified",
            "ivole_review_votes",
            "title",
            "cr_rev_title",
            "ivole_media_count",
            "ivole_review_unreg_downvoters",
            "ivole_review_unreg_upvoters",
            "flag_count",
            "ivole_order_locl",
        }
        if any(key not in supported and any(value.strip() for value in values) for key, values in fields.items()):
            raise ValueError("unknown populated source metadata requires a disposition")
        for key in ("rating", "verified", "ivole_review_votes"):
            if len(fields[key]) > 1:
                raise ValueError("ambiguous source metadata")
        ratings = fields["rating"]
        rating = int(ratings[0]) if ratings and ratings[0] else None
        if rating is not None and not 1 <= rating <= 5:
            raise ValueError("invalid source rating")
        if parent != "0" and rating is not None:
            raise ValueError("rated reply requires a disposition")
        if fields["verified"] not in ([], ["0"], ["1"]):
            raise ValueError("unknown verification assertion")
        titles = [v for key in ("title", "cr_rev_title") for v in fields[key] if v.strip()]
        for key in ("ivole_review_unreg_downvoters", "ivole_review_unreg_upvoters", "flag_count", "ivole_order_locl"):
            archive_counts[key] += sum(bool(value.strip()) for value in fields[key])
        if len(set(titles)) > 1:
            raise ValueError("conflicting source titles")
        if any(value not in ("0", "") for value in fields["ivole_media_count"]):
            raise ValueError("source media requires a mapped disposition")
        gmt = row["comment_date_gmt"]
        try:
            utc = datetime.strptime(gmt, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ValueError("missing or invalid source GMT") from exc
        net = int(fields["ivole_review_votes"][0]) if fields["ivole_review_votes"] else None
        contribution = {
            "id": "wp-" + source_id,
            "subject_id": subject["id"],
            "parent_id": None if parent == "0" else "wp-" + parent,
            "kind": "review" if parent == "0" else "reply",
            "author": row["comment_author"],
            "body": row["comment_content"],
            "title": titles[0] if titles else None,
            "rating": rating,
            "date": {
                "original": utc,
                "precision": "second",
                "timezone_provenance": "source_utc",
                "utc": utc,
                "source_local_raw": row["comment_date"],
                "source_utc_raw": gmt,
            },
            "verification": "legacy_woo_verified_assertion" if fields["verified"] == ["1"] else "unverified",
            "anchors": ["review-wp-" + source_id, "comment-" + source_id],
            "votes": {"up": None, "down": None, "net": net},
        }
        if redact_source_emails:
            for field in ("author", "body", "title"):
                original = contribution[field]
                redacted = _redact_email(original, row.get("comment_author_email"))
                if redacted != original:
                    contribution[field] = redacted
                    privacy_redactions.append({"source_id": source_id, "field": field, "reason": "source_email"})
        public_revision = json.dumps(contribution, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        contribution["revision_id"] = "wp-revision-" + hashlib.sha256(public_revision).hexdigest()
        contributions.append(contribution)
    for item in contributions:
        if item["parent_id"]:
            parents = [other for other in contributions if other["id"] == item["parent_id"]]
            if len(parents) != 1 or parents[0]["subject_id"] != item["subject_id"]:
                raise ValueError("cross-subject or missing parent")
    summaries = []
    for subject in used_subjects.values():
        ratings = [
            item["rating"]
            for item in contributions
            if item["subject_id"] == subject["id"] and item["kind"] == "review" and item["rating"] is not None
        ]
        summaries.append(
            {
                "subject_id": subject["id"],
                "count": len(ratings),
                "sum": sum(ratings),
                "histogram": [ratings.count(i) for i in range(1, 6)],
            }
        )
    digest = hashlib.sha256(raw).hexdigest()
    snapshot = {
        "schema": "cartray.reviews.snapshot.v2",
        "snapshot_id": "draft-" + digest[:24],
        "store": store,
        "environment": environment,
        "source": {
            "identity": "wordpress:" + expected_source,
            "extracted_at": source["captured_utc"],
            "sha256": digest,
            "complete": False,
        },
        "publication_revision": 1,
        "subjects": sorted(used_subjects.values(), key=lambda x: x["id"]),
        "contributions": sorted(contributions, key=lambda x: x["id"]),
        "summaries": summaries,
    }

    # Validate shape on a disposable copy; the returned artifact stays incomplete.
    validation_copy = json.loads(json.dumps(snapshot))
    validation_copy["source"]["complete"] = True
    encoded = json.dumps(validation_copy).encode()
    load_snapshot(encoded, expected_sha256=hashlib.sha256(encoded).hexdigest(), store=store, environment=environment)
    all_source_ids = {str(comment["comment_ID"]) for comment in comments}
    return {
        "snapshot": snapshot,
        "report": {
            "privacy_redactions": privacy_redactions,
            "restricted_archive_only_field_counts": dict(archive_counts),
            "source_status_counts": dict(Counter(str(row["comment_approved"]) for row in comments)),
            "metadata_without_source_comment": sum(
                1 for row in source["commentmeta"] if str(row["comment_id"]) not in all_source_ids
            ),
            "complete": False,
        },
    }
