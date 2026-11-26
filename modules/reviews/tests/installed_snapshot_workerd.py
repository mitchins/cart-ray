"""Prove generated historical-import batches on an actual local Workerd D1 binding."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
ROOT = MODULE.parents[1]
sys.path.insert(0, str(MODULE / "src"))
sys.path.insert(0, str(MODULE / "tools"))
import import_snapshot as importer  # noqa: E402

sys.path.insert(0, str(MODULE / "operator"))
import installed_snapshot  # noqa: E402


def statements(source):
    """Retain complete trigger definitions while separating migration statements."""
    buffer = ""
    output = []
    for line in source.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            output.append(buffer)
            buffer = ""
    if buffer.strip():
        raise AssertionError("incomplete migration statement")
    return output


def publication(value):
    """Identify the exact preceding publication pinned by operator approval."""
    return {"snapshot_id": value["snapshot_id"], "revision": value["publication_revision"],
            "digest": hashlib.sha256(json.dumps(value).encode()).hexdigest()}


def batch(value, previous=None):
    raw = json.dumps(value).encode()
    snapshot, sql = importer.snapshot_statements(
        raw,
        digest=hashlib.sha256(raw).hexdigest(),
        store="example-store",
        environment="test",
    )
    assert snapshot["snapshot_id"] == value["snapshot_id"]
    approval = {"store_key": "example-store", "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
                "expected_publication": previous}
    application, expected = installed_snapshot.application_batch(snapshot, sql, approval)
    return {"sql": application, "expected": expected}


def main():
    original = json.loads((MODULE / "tests/fixtures/approved-synthetic.json").read_bytes())
    newer = original | {"snapshot_id": "two", "publication_revision": 2}
    conflict = json.loads(json.dumps(original))
    conflict["contributions"][0]["body"] = "Conflicting bytes under an existing snapshot identity"
    conflicting_revision = original | {"snapshot_id": "other-two", "publication_revision": 2}
    late_failure = original | {"snapshot_id": "three", "publication_revision": 3}
    silent_skip = original | {"snapshot_id": "ignored-three", "publication_revision": 3}
    migrations = json.loads((MODULE / "module.json").read_text())["migrations"]
    payload = {
        "migrations": [statements((MODULE / path).read_text()) for path in migrations],
        "original": batch(original),
        "newer": batch(newer, publication(original)),
        "wrong_previous": batch(newer),
        "conflict": batch(conflict, publication(original)),
        "same_revision": batch(conflicting_revision, publication(newer)),
        "late_failure": batch(late_failure, publication(newer)),
        "silent_skip": batch(silent_skip, publication(newer)),
    }
    result = subprocess.run(
        ["node", str(MODULE / "tests/installed_snapshot_workerd.mjs")],
        input=json.dumps(payload), cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    print(result.stdout.strip())


if __name__ == "__main__":
    main()
