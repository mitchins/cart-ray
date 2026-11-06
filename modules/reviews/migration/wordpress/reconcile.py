"""Account for every row in a restricted WordPress review-graph candidate.

This reports dispositions; it does not approve prose or mark a snapshot complete.
"""

import hashlib
import json
import re
from collections import Counter, defaultdict

from cartray_reviews.snapshot import _reject_constant, _unique_pairs, load_snapshot

PUBLIC_FIELDS = {
    "rating": "rating",
    "title": "title",
    "cr_rev_title": "title",
    "verified": "legacy_verification_claim",
    "ivole_review_votes": "net_votes",
}
RESTRICTED_FIELDS = {
    "ivole_review_unreg_upvoters",
    "ivole_review_unreg_downvoters",
    "flag_count",
    "ivole_order_locl",
    "ivole_media_count",
}


def _expected_text(value, email, redact):
    if not redact or not isinstance(value, str) or not isinstance(email, str) or "@" not in email:
        return value
    email = email.strip()
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", email):
        return value
    return re.sub(re.escape(email), "[email removed]", value, flags=re.IGNORECASE)


def reconcile(raw, draft, mapping, *, expected_source, redact_source_emails=False):
    """Validate source/draft coverage and return a private, non-approval report."""
    if type(redact_source_emails) is not bool:
        raise ValueError("invalid redaction policy")
    if not isinstance(raw, bytes) or len(raw) > 10_000_000:
        raise ValueError("invalid source bytes")
    source = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    if (
        source.get("schema") != "restricted.review-candidate.v1"
        or source.get("complete") is not False
        or source.get("site") != expected_source
    ):
        raise ValueError("invalid source identity")
    snapshot = draft["snapshot"]
    if (
        snapshot["source"]["sha256"] != hashlib.sha256(raw).hexdigest()
        or snapshot["source"]["identity"] != "wordpress:" + expected_source
        or snapshot["source"]["extracted_at"] != source["captured_utc"]
        or snapshot["source"]["complete"] is not False
    ):
        raise ValueError("invalid draft provenance")
    validation_copy = json.loads(json.dumps(snapshot))
    validation_copy["source"]["complete"] = True
    encoded = json.dumps(validation_copy).encode()
    if len(encoded) > 90_000:
        raise ValueError("draft exceeds import response budget")
    load_snapshot(
        encoded,
        expected_sha256=hashlib.sha256(encoded).hexdigest(),
        store=snapshot["store"],
        environment=snapshot["environment"],
    )
    records = source["comments"]
    if not isinstance(records, list) or len(records) > 10000:
        raise ValueError("invalid source records")
    by_id = {str(row["comment_ID"]): row for row in records}
    if len(by_id) != len(records):
        raise ValueError("duplicate source identity")
    subjects = source.get("subjects")
    if not isinstance(subjects, list) or len(subjects) > 10000:
        raise ValueError("invalid source subjects")
    subjects_by_id = {str(row["ID"]): row for row in subjects}
    if len(subjects_by_id) != len(subjects):
        raise ValueError("duplicate source subject")
    if any(str(row["comment_post_ID"]) not in subjects_by_id for row in records):
        raise ValueError("missing source subject requires disposition")
    public = {item["id"]: item for item in snapshot["contributions"]}
    if len(public) != len(snapshot["contributions"]):
        raise ValueError("duplicate draft identity")
    public_text = "\n".join(
        str(item.get(field) or "") for item in public.values() for field in ("author", "title", "body")
    ).casefold()
    source_emails = {
        row.get("comment_author_email", "").strip().casefold()
        for row in records
        if isinstance(row.get("comment_author_email"), str) and "@" in row["comment_author_email"]
    }
    if any(email in public_text for email in source_emails):
        raise ValueError("source email present in candidate public content")
    expected = set()
    expected_subjects = {}
    redactions = []
    record_dispositions = []
    for source_id, row in by_id.items():
        kind, status, parent = row["comment_type"], str(row["comment_approved"]), str(row["comment_parent"])
        if kind not in ("review", "comment") or status not in ("1", "0", "spam", "trash"):
            raise ValueError("unclassified source record")
        if status == "1":
            if subjects_by_id[str(row["comment_post_ID"])]["post_status"] != "publish":
                raise ValueError("nonpublic source subject requires disposition")
            protection = subjects_by_id[str(row["comment_post_ID"])].get("password_protected")
            if not ((type(protection) is int and protection == 0) or (type(protection) is str and protection == "0")):
                raise ValueError("password-protected or unknown source subject requires disposition")
            if kind == "review" and parent == "0":
                disposition = "candidate_review"
            elif (
                kind == "comment"
                and parent in by_id
                and by_id[parent]["comment_type"] == "review"
                and str(by_id[parent]["comment_approved"]) == "1"
            ):
                disposition = "candidate_reply"
            else:
                raise ValueError("unclassified approved record")
            item_id = "wp-" + source_id
            expected.add(item_id)
            item = public.get(item_id)
            subject = mapping.get(str(row["comment_post_ID"]))
            author = _expected_text(row["comment_author"], row.get("comment_author_email"), redact_source_emails)
            body = _expected_text(row["comment_content"], row.get("comment_author_email"), redact_source_emails)
            for field, actual, expected_text in (
                ("author", row["comment_author"], author),
                ("body", row["comment_content"], body),
            ):
                if actual != expected_text:
                    redactions.append({"source_id": source_id, "field": field, "reason": "source_email"})
            if (
                item is None
                or not isinstance(subject, dict)
                or item["subject_id"] != subject.get("id")
                or item["author"] != author
                or item["body"] != body
                or item["parent_id"] != (None if parent == "0" else "wp-" + parent)
                or item["date"].get("source_local_raw") != row["comment_date"]
                or item["date"].get("source_utc_raw") != row["comment_date_gmt"]
            ):
                raise ValueError("draft does not match source")
            prior = expected_subjects.setdefault(subject["id"], subject)
            if prior != subject:
                raise ValueError("conflicting subject mapping")
        else:
            disposition = {"0": "restricted_pending", "spam": "restricted_spam", "trash": "restricted_trash"}[status]
        record_dispositions.append(
            {
                "source_id": source_id,
                "subject_id": str(row["comment_post_ID"]),
                "parent_id": parent,
                "status": status,
                "disposition": disposition,
            }
        )
    if set(public) != expected:
        raise ValueError("source/draft coverage mismatch")
    actual_subjects = {item["id"]: item for item in snapshot["subjects"]}
    if actual_subjects != expected_subjects:
        raise ValueError("draft subject mapping mismatch")
    metadata = source["commentmeta"]
    if not isinstance(metadata, list) or len(metadata) > 100000:
        raise ValueError("invalid metadata")
    seen, dispositions = set(), []
    values = defaultdict(lambda: defaultdict(list))
    for row in metadata:
        meta_id, source_id, key = str(row["meta_id"]), str(row["comment_id"]), row["meta_key"]
        if meta_id in seen or source_id not in by_id:
            raise ValueError("duplicate or orphaned metadata")
        seen.add(meta_id)
        values[source_id][key].append(row["meta_value"])
        if "wp-" + source_id not in expected:
            disposition = "restricted_nonpublic_record"
        elif key in PUBLIC_FIELDS:
            disposition = PUBLIC_FIELDS[key]
        elif key in RESTRICTED_FIELDS:
            disposition = "restricted_archive_only"
        else:
            raise ValueError("unclassified approved metadata")
        dispositions.append({"meta_id": meta_id, "source_id": source_id, "key": key, "disposition": disposition})
    for item_id in expected:
        source_id = item_id[3:]
        row, item, fields = by_id[source_id], public[item_id], values[source_id]
        for key in ("rating", "verified", "ivole_review_votes"):
            if len(fields[key]) > 1:
                raise ValueError("ambiguous approved metadata")
        if fields["verified"] not in ([], ["0"], ["1"]):
            raise ValueError("unknown source verification")
        if any(value not in ("0", "") for value in fields["ivole_media_count"]):
            raise ValueError("approved source media requires explicit disposition")
        rating = int(fields["rating"][0]) if fields["rating"] and fields["rating"][0] else None
        net = int(fields["ivole_review_votes"][0]) if fields["ivole_review_votes"] else None
        titles = {value for key in ("title", "cr_rev_title") for value in fields[key] if value.strip()}
        if len(titles) > 1:
            raise ValueError("ambiguous approved title")
        title = next(iter(titles), None)
        redacted_title = _expected_text(title, row.get("comment_author_email"), redact_source_emails)
        if redacted_title != title:
            redactions.append({"source_id": source_id, "field": "title", "reason": "source_email"})
        title = redacted_title
        verification = "legacy_woo_verified_assertion" if fields["verified"] == ["1"] else "unverified"
        utc = row["comment_date_gmt"].replace(" ", "T") + "Z"
        if (
            item["rating"] != rating
            or item.get("title") != title
            or item["verification"] != verification
            or item["votes"] != {"up": None, "down": None, "net": net}
            or item["date"]["original"] != utc
            or item["date"]["utc"] != utc
            or item["date"]["precision"] != "second"
            or item["date"]["timezone_provenance"] != "source_utc"
            or item["anchors"] != ["review-" + item_id, "comment-" + source_id]
        ):
            raise ValueError("draft metadata does not match source")
        revision_payload = {key: value for key, value in item.items() if key != "revision_id"}
        revision_bytes = json.dumps(revision_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        if item["revision_id"] != "wp-revision-" + hashlib.sha256(revision_bytes).hexdigest():
            raise ValueError("draft revision does not match content")
    public_subject_ids = {
        row["subject_id"] for row in record_dispositions if row["disposition"].startswith("candidate_")
    }
    return {
        "schema": "restricted.review-reconciliation.v1",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "scope": "selected_wordpress_review_comment_graph",
        "complete": False,
        "record_dispositions": sorted(record_dispositions, key=lambda x: int(x["source_id"])),
        "metadata_dispositions": sorted(dispositions, key=lambda x: int(x["meta_id"])),
        "privacy_redactions": sorted(redactions, key=lambda x: (int(x["source_id"]), x["field"])),
        "subject_dispositions": [
            {
                "source_id": subject_id,
                "post_type": subject["post_type"],
                "post_status": subject["post_status"],
                "disposition": "candidate_mapped" if subject_id in public_subject_ids else "restricted_only",
            }
            for subject_id, subject in sorted(subjects_by_id.items(), key=lambda x: int(x[0]))
        ],
        "record_counts": dict(Counter(row["disposition"] for row in record_dispositions)),
    }


def main():
    """Write a restricted, explicitly incomplete draft and row-level report."""
    import argparse
    import os
    from pathlib import Path

    from adapter import build_draft

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--redact-source-emails", action="store_true")
    args = parser.parse_args()

    def read_bounded(path, limit):
        with path.open("rb") as file:
            data = file.read(limit + 1)
        if len(data) > limit:
            raise ValueError("input exceeds size limit")
        return data

    raw = read_bounded(args.source, 10_000_000)
    mapping_raw = read_bounded(args.mapping, 1_000_000)
    mapping = json.loads(mapping_raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    if not isinstance(mapping, dict):
        raise ValueError("mapping must be an object")
    draft = build_draft(
        raw,
        mapping,
        store=args.store,
        environment="test",
        expected_source=args.expected_source,
        redact_source_emails=args.redact_source_emails,
    )
    report = reconcile(
        raw, draft, mapping, expected_source=args.expected_source, redact_source_emails=args.redact_source_emails
    )
    args.output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, value in (("draft.json", draft), ("reconciliation.json", report)):
        target = args.output_directory / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
    print("Restricted incomplete draft and reconciliation report created; no publication approved.")


if __name__ == "__main__":
    main()
