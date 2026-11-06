"""Exercise an explicitly qualified source snapshot in isolated local test D1.

The source export, snapshot and output stay local and private. Only aggregate
results are printed. The test Worker uses synthetic admin capabilities and no
commerce, production or email bindings.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from cartray_reviews.snapshot import load_snapshot
from workerd_integration import MODULE, PORT, ROOT, start, stop


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def call(path, *, payload=None, admin=None, key=None):
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = "Bearer " + admin
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--exercise-subject", required=True)
    args = parser.parse_args()

    def read_bounded(path):
        with path.open("rb") as file:
            data = file.read(10_000_001)
        require(len(data) <= 10_000_000, "input exceeds limit")
        return data

    snapshot_bytes, source_bytes = read_bounded(args.snapshot), read_bounded(args.source)
    snapshot = load_snapshot(snapshot_bytes, expected_sha256=args.sha256, store=args.store, environment="test")
    require(snapshot["source"]["identity"].startswith("wordpress-review-comment-graph:"), "wrong source scope")
    require(snapshot["source"]["sha256"] == hashlib.sha256(source_bytes).hexdigest(), "source digest mismatch")
    source = json.loads(source_bytes)
    emails = {
        row.get("comment_author_email", "").strip().casefold()
        for row in source["comments"]
        if isinstance(row.get("comment_author_email"), str) and "@" in row["comment_author_email"]
    }
    admin = os.urandom(32).hex()
    subjects = {item["id"]: item for item in snapshot["subjects"]}
    require(args.exercise_subject in subjects, "unknown exercise subject")
    expected = {item["id"]: item for item in snapshot["contributions"]}
    roots = {key for key, value in expected.items() if value["parent_id"] is None}
    replies = set(expected) - roots
    with tempfile.TemporaryDirectory(prefix="reviews-real-graph-test-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory, "PYTHONPATH": str(MODULE / "src")}
        subprocess.run(
            [sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True, capture_output=True
        )
        importer = [
            sys.executable,
            str(MODULE / "tools/import_snapshot.py"),
            str(args.snapshot),
            "--sha256",
            args.sha256,
            "--store",
            args.store,
            "--environment",
            "test",
        ]
        for _ in range(2):
            subprocess.run(importer, cwd=ROOT, env=env, check=True, capture_output=True)
        command = [
            str(ROOT / "node_modules/.bin/wrangler"),
            "d1",
            "execute",
            "cartray-reviews-local-test",
            "--config",
            str(MODULE / "wrangler.test.toml"),
            "--local",
            "--persist-to",
            directory,
            "--json",
            "--command",
            "SELECT (SELECT COUNT(*) FROM review_snapshots) snapshots, "
            "(SELECT COUNT(*) FROM review_contributions) contributions, "
            "(SELECT COUNT(*) FROM native_audit) audit, "
            "(SELECT COUNT(*) FROM native_effect_outbox) outbox, "
            "(SELECT COUNT(*) FROM acquisition_invitations) invitations",
        ]
        result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
        counts = json.loads(result.stdout)[0]["results"][0]
        require(
            counts == {"snapshots": 1, "contributions": len(expected), "audit": 0, "outbox": 0, "invitations": 0},
            "import idempotency or side-effect failure",
        )
        proc = start("wrangler.test.toml", directory, "collect", admin, {"REVIEWS_STORE_KEY": args.store})
        try:
            observed, wire_text = {}, []

            def walk(item):
                identity = item["id"]
                require(identity in expected and identity not in observed, "unexpected public contribution")
                original = expected[identity]
                require(
                    item.get("source")
                    == {"kind": "historical", "attribution": snapshot["source"]["public_attribution"]},
                    "public source attribution mismatch",
                )
                for field in ("author", "body", "title", "rating", "date", "verification", "anchors", "votes"):
                    require(item.get(field) == original.get(field), "public projection mismatch")
                observed[identity] = item
                for reply in item["replies"]:
                    require(expected[reply["id"]]["parent_id"] == identity, "reply parent mismatch")
                    walk(reply)

            for subject in subjects:
                for alias in subjects[subject].get("aliases", []) + [subject, subjects[subject]["path"]]:
                    status, resolved = call("/reviews/v1/subject-lookup?" + urlencode({"alias": alias}))
                    require(
                        status == 200
                        and resolved
                        == {
                            "subject_id": subject,
                            "kind": subjects[subject]["kind"],
                            "canonical_path": subjects[subject]["path"],
                        },
                        "subject alias lookup mismatch",
                    )
                status, summary = call(f"/reviews/v1/subjects/{subject}/summary")
                require(status == 200, "summary unavailable")
                status, page = call(f"/reviews/v1/subjects/{subject}/contributions?limit=50")
                require(status == 200 and not page["has_more"], "contribution list unavailable or exceeds test bound")
                wire_text.append(json.dumps(page, ensure_ascii=False).casefold())
                for item in page["contributions"]:
                    walk(item)
                expected_roots = [
                    item for item in expected.values() if item["subject_id"] == subject and item["parent_id"] is None
                ]
                ratings = [item["rating"] for item in expected_roots if item["rating"] is not None]
                require(
                    summary["contribution_count"] == len(expected_roots)
                    and summary["rating"]["rated_count"] == len(ratings)
                    and summary["rating"]["average"] == (sum(ratings) / len(ratings) if ratings else None),
                    "historical aggregate mismatch",
                )
            require(set(observed) == set(expected), "historical contribution coverage mismatch")
            joined = "\n".join(wire_text)
            require(not any(email in joined for email in emails), "source email leaked to public response")
            require(snapshot["source"]["identity"].casefold() not in joined, "private source identity leaked")
            subject = subjects[args.exercise_subject]
            route = f"/reviews/v1/subjects/{subject['id']}/summary"
            before = call(route)[1]
            status, _ = call(
                "/reviews-admin/v1/subjects",
                payload={"subject_id": subject["id"], "kind": subject["kind"], "path": subject["path"]},
                admin=admin,
            )
            require(status == 201, "native subject registration failed")
            status, proof = call(
                "/reviews-admin/v1/test-capabilities",
                payload={"subject_id": subject["id"], "test_reviewer": "synthetic-r4"},
                admin=admin,
            )
            require(status == 201, "synthetic capability failed")
            status, review = call(
                "/reviews/v1/contributions",
                payload={
                    "subject_id": subject["id"],
                    "author": "Synthetic",
                    "body": "Synthetic test contribution",
                    "rating": 2,
                    "test_capability": proof["test_capability"],
                },
                key="r4-synthetic",
            )
            require(status == 202, "synthetic review failed")
            pending = call(route)[1]
            listing_route = f"/reviews/v1/subjects/{subject['id']}/contributions?limit=50"
            pending_list = json.dumps(call(listing_route)[1])
            require(
                pending["rating"] == before["rating"]
                and pending["contribution_count"] == before["contribution_count"]
                and review["review_id"] not in pending_list
                and "Synthetic test contribution" not in pending_list,
                "pending review affected public output",
            )
            status, _ = call(
                "/reviews-admin/v1/contributions/" + review["review_id"] + "/approve",
                payload={"revision_id": review["revision_id"]},
                admin=admin,
            )
            require(status == 200, "synthetic approval failed")
            after = call(route)[1]
            rated = before["rating"]["rated_count"]
            prior = before["rating"]["average"] or 0
            require(
                after["contribution_count"] == before["contribution_count"] + 1
                and after["rating"] == {"rated_count": rated + 1, "average": (prior * rated + 2) / (rated + 1)},
                "combined aggregate mismatch",
            )
            mixed_page = call(listing_route)[1]
            native_item = next(
                (item for item in mixed_page["contributions"] if item["id"] == "n-" + review["review_id"]), None
            )
            require(
                native_item is not None and native_item.get("source") == {"kind": "native", "attribution": None},
                "native source attribution mismatch",
            )
            require(
                all(
                    item.get("source")
                    == {"kind": "historical", "attribution": snapshot["source"]["public_attribution"]}
                    for item in mixed_page["contributions"]
                    if item["id"].startswith("h-")
                ),
                "mixed historical attribution mismatch",
            )
            status, _ = call(
                "/reviews-admin/v1/contributions/" + review["review_id"] + "/withdraw",
                payload={"revision_id": review["revision_id"]},
                admin=admin,
            )
            withdrawn = call(route)[1]
            withdrawn_list = json.dumps(call(listing_route)[1])
            require(
                status == 200
                and withdrawn["rating"] == before["rating"]
                and withdrawn["contribution_count"] == before["contribution_count"]
                and review["review_id"] not in withdrawn_list
                and "Synthetic test contribution" not in withdrawn_list,
                "withdrawal public output mismatch",
            )
            print(
                "Local isolated test: historical roots",
                len(roots),
                "replies",
                len(replies),
                "rated",
                sum(expected[x]["rating"] is not None for x in roots),
                "unrated",
                sum(expected[x]["rating"] is None for x in roots),
            )
            print("Idempotent import, source parity, privacy and native lifecycle passed.")
        finally:
            stop(proc)


if __name__ == "__main__":
    main()
