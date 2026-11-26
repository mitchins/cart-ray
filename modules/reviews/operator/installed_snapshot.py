"""Explicit approved-snapshot application to an existing test installation."""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# Reuse the exact local import compiler and public contract, not a second importer.
MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "src"))
sys.path.insert(0, str(MODULE / "tools"))
from import_snapshot import literal, snapshot_statements  # noqa: E402

APPROVAL_FIELDS = {
    "format", "account_id", "database_id", "installation_id", "store_key", "environment",
    "snapshot_sha256", "snapshot_id", "publication_revision", "expected_publication", "approval_ref",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate approval field")
        result[key] = value
    return result


def read_bounded(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("snapshot/approval exceeds the supported import size")
    return data


def publication_condition(value, store, environment):
    scope = f"p.store_key={literal(store)} AND p.environment={literal(environment)}"
    if value is None:
        return f"NOT EXISTS(SELECT 1 FROM review_publications p WHERE {scope})"
    if (not isinstance(value, dict) or set(value) != {"snapshot_id", "revision", "digest"}
            or not isinstance(value["snapshot_id"], str) or not value["snapshot_id"]
            or type(value["revision"]) is not int or value["revision"] < 1
            or not isinstance(value["digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["digest"])):
        raise ValueError("invalid expected publication identity")
    return (
        "EXISTS(SELECT 1 FROM review_publications p JOIN review_snapshots s "
        "USING(store_key,environment,snapshot_id) WHERE " + scope
        + f" AND p.snapshot_id={literal(value['snapshot_id'])} AND p.revision={value['revision']}"
        + f" AND s.digest={literal(value['digest'])})"
    )


def approval_for(installation, raw, expected_digest):
    if (not isinstance(expected_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_digest)
            or hashlib.sha256(raw).hexdigest() != expected_digest):
        raise ValueError("approval SHA-256 mismatch; obtain the approved exact file")
    approval = json.loads(raw, object_pairs_hook=unique_object)
    if (not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS
            or type(approval["format"]) is not int or approval["format"] != 1
            or not isinstance(approval["approval_ref"], str)
            or not re.fullmatch(r"[A-Za-z0-9:/._#-]{1,240}", approval["approval_ref"])):
        raise ValueError("invalid snapshot approval format/reference")
    config = installation.config
    if (not config.get("reviews_enabled") or installation.state.get("environment") != "test"
            or approval["environment"] != "test"):
        raise ValueError("snapshot application requires an installed test-only Reviews instance")
    expected = {
        "account_id": config["account_id"], "database_id": installation.db("reviews"),
        "installation_id": config["namespace"], "store_key": config.get("store_key", "acorn-store"),
    }
    if (any(approval[key] != value for key, value in expected.items())
            or not installation.api or installation.api.account != expected["account_id"]
            or installation.state.get("installation") != expected["installation_id"]):
        raise ValueError("approval target differs from the installed account/database/merchant")
    publication_condition(approval["expected_publication"], approval["store_key"], "test")
    return approval


def save_receipt(directory, name, value):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (name + ".json")
    # Link a complete fsynced temporary file: no overwrite and no partial receipt.
    temporary = directory / ("." + name + "." + os.urandom(8).hex())
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            old = json.loads(path.read_text())
            if {k: v for k, v in old.items() if k != "verified_at"} != {
                    k: v for k, v in value.items() if k != "verified_at"}:
                raise ValueError("existing immutable snapshot receipt differs; inspect retained state") from None
            return old
    finally:
        temporary.unlink(missing_ok=True)
    return value


def application_batch(snapshot, statements, approval):
    """Compile one CAS-guarded, count-checked batch with atomic readback."""
    store, environment = approval["store_key"], "test"
    target = {"snapshot_id": snapshot["snapshot_id"], "revision": snapshot["publication_revision"],
              "digest": approval["snapshot_sha256"]}
    condition = publication_condition(approval["expected_publication"], store, environment)
    retry = publication_condition(target, store, environment)
    guard = f"INSERT INTO review_import_guard SELECT CASE WHEN ({condition}) OR ({retry}) THEN 1 ELSE 0 END;"
    # No chunking or Wrangler file upload: one provider batch including the CAS,
    # original importer guards, publication pointer and final readback.
    scope = f"store_key={literal(store)} AND environment={literal(environment)}"
    snapshot_scope = scope + f" AND snapshot_id={literal(snapshot['snapshot_id'])}"
    readback = (
        "SELECT p.snapshot_id,p.revision,s.digest,"
        f"(SELECT COUNT(*) FROM review_snapshot_subjects WHERE {snapshot_scope}) AS subjects,"
        f"(SELECT COUNT(*) FROM review_contributions WHERE {snapshot_scope} AND kind='review') AS reviews,"
        f"(SELECT COUNT(*) FROM review_contributions WHERE {snapshot_scope} AND kind='reply') AS replies,"
        f"(SELECT COUNT(*) FROM review_subject_aliases WHERE {snapshot_scope}) AS aliases,"
        f"(SELECT COUNT(*) FROM review_import_titles WHERE {snapshot_scope}) AS titles,"
        f"(SELECT COUNT(*) FROM review_snapshot_provenance WHERE {snapshot_scope}) AS provenance "
        "FROM review_publications p JOIN review_snapshots s USING(store_key,environment,snapshot_id) "
        f"WHERE p.store_key={literal(store)} AND p.environment={literal(environment)};"
    )
    expected = target | {"subjects": len(snapshot["subjects"]),
                         "reviews": sum(c["kind"] == "review" for c in snapshot["contributions"]),
                         "replies": sum(c["kind"] == "reply" for c in snapshot["contributions"]),
                         "aliases": sum(len(s.get("aliases", [])) for s in snapshot["subjects"]),
                         "titles": sum(c.get("title") is not None for c in snapshot["contributions"]), "provenance": 1}
    # Counts are checked inside the transaction, not merely after publication.
    count_guard = (
        "INSERT INTO review_import_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM (" + readback.rstrip(";")
        + ") WHERE snapshot_id=" + literal(target["snapshot_id"])
        + f" AND revision={target['revision']} AND digest={literal(target['digest'])}"
        + f" AND subjects={expected['subjects']} AND reviews={expected['reviews']} AND replies={expected['replies']}"
        + f" AND aliases={expected['aliases']} AND titles={expected['titles']} AND provenance=1)"
        + " THEN 1 ELSE 0 END;"
    )
    return [guard, *statements, count_guard, "DELETE FROM review_import_guard;", readback], expected



def apply(installation, snapshot_path, approval_path, expected_approval_sha256):
    """Apply one approved artifact, guarded against target drift and stale publication."""
    approval = approval_for(installation, read_bounded(approval_path, 16_384), expected_approval_sha256)
    data = read_bounded(snapshot_path, 1_000_000)
    snapshot, statements = snapshot_statements(
        data, digest=approval["snapshot_sha256"], store=approval["store_key"], environment="test")
    if (snapshot["snapshot_id"] != approval["snapshot_id"]
            or type(approval["publication_revision"]) is not int
            or snapshot["publication_revision"] != approval["publication_revision"]):
        raise ValueError("snapshot identity/revision differs from approval")
    if not installation.state.get("deployment", {}).get("complete"):
        raise ValueError("installation has no complete deployment; deploy and verify before applying a snapshot")
    installation.assert_resources()
    # Read-only verification of actual deployments, bindings and migration checksums.
    installation.verifier("inspect")
    batch, expected = application_batch(snapshot, statements, approval)
    batches = installation.api.batch(installation.db("reviews"), batch)
    if not batches or batches[-1] != [expected]:
        raise ValueError("snapshot readback differs; do not publish downstream; retry the identical approved artifact")
    receipt = {
        "format": 1, "operation": "apply-approved-test-review-snapshot",
        "approval_sha256": expected_approval_sha256, "approval_ref": approval["approval_ref"],
        "target": {key: approval[key] for key in
                   ("account_id", "database_id", "installation_id", "store_key", "environment")},
        "expected_publication": approval["expected_publication"], "readback": expected,
        "verified_at": int(time.time()),
    }
    return save_receipt(installation.directory / "review-snapshot-receipts", expected_approval_sha256, receipt)
