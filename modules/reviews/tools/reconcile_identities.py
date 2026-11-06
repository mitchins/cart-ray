"""Apply an explicitly reviewed, byte-pinned identity manifest to local test D1."""

import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path

from cartray_reviews.snapshot import _reject_constant, _unique_pairs
from import_snapshot import literal
from install import execute


def apply_manifest(raw, *, sha256, store, environment):
    """Atomically activate adjudicated root links; a repeated manifest is a no-op."""
    if environment != "test" or not isinstance(raw, bytes) or len(raw) > 100000:
        raise ValueError("invalid test manifest")
    if not re.fullmatch(r"[a-f0-9]{64}", sha256) or hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("manifest digest mismatch")
    document = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "store", "environment", "links"}
        or document["schema"] != "cartray.reviews.identity-links.v1"
        or document["store"] != store
        or document["environment"] != environment
        or not isinstance(document["links"], list)
        or len(document["links"]) > 200
    ):
        raise ValueError("invalid identity manifest")
    lines = []
    for link in document["links"]:
        fields = (
            "subject_id",
            "source_identity",
            "source_contribution_id",
            "source_revision_id",
            "source_snapshot_sha256",
            "native_review_id",
            "native_revision_id",
            "evidence_sha256",
        )
        if not isinstance(link, dict) or set(link) != set(fields):
            raise ValueError("invalid identity link")
        if any(not isinstance(link[k], str) or not link[k] or len(link[k]) > 2048 for k in fields):
            raise ValueError("invalid identity link")
        if any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", link[k])
            for k in (
                "subject_id",
                "source_contribution_id",
                "source_revision_id",
                "native_review_id",
                "native_revision_id",
            )
        ):
            raise ValueError("invalid identity link")
        if any(not re.fullmatch(r"[a-f0-9]{64}", link[k]) for k in ("source_snapshot_sha256", "evidence_sha256")):
            raise ValueError("invalid evidence digest")
        values = tuple(map(literal, (store, environment, *(link[k] for k in fields))))
        st, env, subject, source, legacy, revision, snapshot_digest, native, native_revision, evidence = values
        lines.append(
            "INSERT INTO review_import_guard SELECT CASE WHEN NOT EXISTS("
            f"SELECT 1 FROM review_identity_links WHERE store_key={st} AND environment={env} "
            f"AND ((source_identity={source} AND source_contribution_id={legacy}) OR native_review_id={native}) "
            f"AND NOT (subject_id={subject} AND source_identity={source} AND source_contribution_id={legacy} "
            f"AND source_revision_id={revision} AND source_snapshot_sha256={snapshot_digest} "
            f"AND native_review_id={native} AND native_revision_id={native_revision} "
            f"AND evidence_sha256={evidence})) THEN 1 ELSE 0 END;"
        )
        lines.append(
            "INSERT INTO review_identity_links SELECT " + ",".join(values) + " WHERE NOT EXISTS("
            f"SELECT 1 FROM review_identity_links WHERE store_key={st} AND environment={env} "
            f"AND source_identity={source} AND source_contribution_id={legacy});"
        )
    lines.append("DELETE FROM review_import_guard;")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql") as file:
        file.write("\n".join(lines))
        file.flush()
        execute("--file", file.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--environment", choices=["test"], required=True)
    args = parser.parse_args()
    apply_manifest(args.manifest.read_bytes(), sha256=args.sha256, store=args.store, environment=args.environment)
    print("Reviewed identity manifest applied to local test D1")
