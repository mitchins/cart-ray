"""Run the opt-in Reviews Worker against a disposable local D1 database."""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "modules/reviews"
FIXTURE = MODULE / "tests/fixtures/approved-synthetic.json"
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    PORT = listener.getsockname()[1]
URL = f"http://127.0.0.1:{PORT}/reviews/v1/subjects/example-policy/"


def request(path):
    """Fetch one local endpoint, preserving status for negative checks."""
    try:
        with urllib.request.urlopen(URL + path, timeout=3) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def start(config, persist, mode=None, admin_token=None, extra_vars=None):
    """Start the pinned local worker and wait for its HTTP listener."""
    command = [
        "uv",
        "run",
        "pywrangler",
        "dev",
        "--config",
        str(MODULE / config),
        "--local",
        "--port",
        str(PORT),
        "--persist-to",
        persist,
        "--log-level=error",
    ]
    if mode:
        command.extend(["--var", f"REVIEWS_MODE:{mode}"])
    if admin_token:
        command.extend(["--var", f"REVIEWS_TEST_ADMIN_TOKEN:{admin_token}"])
    for name, value in (extra_vars or {}).items():
        command.extend(["--var", f"{name}:{value}"])
    output = None if os.environ.get("REVIEWS_TEST_VERBOSE") else subprocess.DEVNULL
    proc = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=output, start_new_session=True)
    for _ in range(180):
        if proc.poll() is not None:
            raise RuntimeError("worker exited before readiness")
        try:
            status, _ = request("summary")
            if status in (200, 404, 503):
                return proc
        except (OSError, TimeoutError):
            time.sleep(1)
    os.killpg(proc.pid, signal.SIGTERM)
    raise RuntimeError("worker failed to start")


def stop(proc):
    """Stop the local wrapper and its child process group."""
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    for _ in range(100):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", PORT)) != 0:
                return
        time.sleep(0.1)
    raise RuntimeError("worker port did not close")


def main():
    """Install twice, import twice, and assert read-only and disabled behavior."""
    with tempfile.TemporaryDirectory(prefix="cartray-reviews-d1-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory, "PYTHONPATH": str(MODULE / "src")}
        installer = [sys.executable, str(MODULE / "tools/install.py")]
        for _ in range(2):
            subprocess.run(installer, env=env, cwd=ROOT, check=True)
        proc = start("wrangler.test.toml", directory, "read_only")
        try:
            assert request("summary")[0] == 404
        finally:
            stop(proc)
        digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        importer = [
            sys.executable,
            str(MODULE / "tools/import_snapshot.py"),
            str(FIXTURE),
            "--sha256",
            digest,
            "--store",
            "example-store",
            "--environment",
            "test",
        ]
        for _ in range(2):
            subprocess.run(importer, env=env, cwd=ROOT, check=True)
        conflicting = json.loads(FIXTURE.read_bytes())
        conflicting["contributions"][0]["body"] = "changed without a new identity"
        conflict_path = Path(directory) / "conflict.json"
        conflict_path.write_text(json.dumps(conflicting))
        conflict = subprocess.run(
            [
                sys.executable,
                str(MODULE / "tools/import_snapshot.py"),
                str(conflict_path),
                "--sha256",
                hashlib.sha256(conflict_path.read_bytes()).hexdigest(),
                "--store",
                "example-store",
                "--environment",
                "test",
            ],
            env=env,
            cwd=ROOT,
            capture_output=True,
        )
        assert conflict.returncode != 0
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
            "--command",
        ]
        checksum = hashlib.sha256((MODULE / "migrations/0001_native_subjects.sql").read_bytes()).hexdigest()
        subprocess.run(
            [
                *command,
                "UPDATE reviews_migration_ledger SET checksum='invalid' WHERE version='0001_native_subjects.sql'",
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        assert subprocess.run(installer, env=env, cwd=ROOT, capture_output=True).returncode != 0
        subprocess.run(
            [
                *command,
                f"UPDATE reviews_migration_ledger SET checksum='{checksum}' WHERE version='0001_native_subjects.sql'",
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        canary = (
            "INSERT INTO review_contributions SELECT store_key,environment,snapshot_id,'hidden-canary',"
            "'private-revision',subject_id,NULL,'review','pending','PRIVATE_AUTHOR_CANARY',"
            "'PRIVATE_BODY_CANARY',rating,source_date,sort_day,verification,anchors_json,votes_json "
            "FROM review_contributions WHERE contribution_id='r';"
        )
        subprocess.run([*command, canary], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        other_store = (
            "INSERT INTO review_contributions SELECT 'other-store',environment,snapshot_id,'foreign-canary',"
            "'foreign-revision',subject_id,NULL,'review','published','PRIVATE_OTHER_STORE',"
            "'PRIVATE_OTHER_STORE',rating,source_date,sort_day,verification,anchors_json,votes_json "
            "FROM review_contributions WHERE contribution_id='r';"
        )
        subprocess.run([*command, other_store], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        measured = subprocess.run(
            [
                *command[:-1],
                "--json",
                "--command",
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM review_contributions WHERE store_key='example-store' "
                "AND environment='test' AND snapshot_id='one' AND subject_id='example-policy' "
                "AND status='published' AND parent_id IS NULL",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert any("review_feed_idx" in row["detail"] for row in json.loads(measured.stdout)[0]["results"])
        proc = start("wrangler.test.toml", directory, "read_only")
        try:
            status, body = request("summary")
            summary = json.loads(body)
            assert status == 200 and summary == {
                "subject_id": "example-policy",
                "snapshot_id": "one",
                "rating": {"average": 4.5, "rated_count": 2},
                "contribution_count": 3,
            }
            status, body = request("contributions")
            result = json.loads(body)
            assert status == 200 and len(result["contributions"]) == 3
            assert sum(len(item["replies"]) for item in result["contributions"]) == 1
            assert sum(item["rating"] is None for item in result["contributions"]) == 1
            assert "PRIVATE_" not in body
            assert [item["id"] for item in result["contributions"]] == ["u", "r", "r2"]
            status, first = request("contributions?limit=1")
            page = json.loads(first)
            assert status == 200 and page["next_after"] == "u"
            status, second = request("contributions?limit=1&after=u&snapshot=one")
            assert status == 200 and json.loads(second)["contributions"][0]["id"] == "r"
            assert request("contributions?after=u&snapshot=other")[0] == 400
            assert request("contributions?limit=51")[0] == 400
            subprocess.run(
                [
                    *command,
                    "CREATE TRIGGER test_reject BEFORE UPDATE ON review_publications "
                    "BEGIN SELECT RAISE(ABORT, 'test rollback'); END;",
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            newer = json.loads(FIXTURE.read_bytes())
            newer["snapshot_id"] = "two"
            newer["publication_revision"] = 2
            changed = Path(directory) / "newer.json"
            changed.write_text(json.dumps(newer))
            failed = subprocess.run(
                [
                    sys.executable,
                    str(MODULE / "tools/import_snapshot.py"),
                    str(changed),
                    "--sha256",
                    hashlib.sha256(changed.read_bytes()).hexdigest(),
                    "--store",
                    "example-store",
                    "--environment",
                    "test",
                ],
                env=env,
                cwd=ROOT,
                capture_output=True,
            )
            assert failed.returncode != 0
            subprocess.run([*command, "DROP TRIGGER test_reject;"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            assert json.loads(request("summary")[1])["snapshot_id"] == "one"
            verify = subprocess.run(
                [
                    *command[:-1],
                    "--json",
                    "--command",
                    "SELECT COUNT(*) n FROM review_snapshots WHERE snapshot_id='two'",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            assert json.loads(verify.stdout)[0]["results"][0]["n"] == 0
            # An imported and a native contribution can share one canonical subject.
            # Seed the approved native revision into D1 directly; lifecycle transition
            # and aggregate maintenance are independently tested by native_workerd.
            subprocess.run(
                [
                    *command,
                    "INSERT INTO review_subjects VALUES "
                    "('example-store','test','example-policy','product','/product/');"
                    "INSERT INTO native_publication_versions VALUES ('example-store','test','example-policy',1,1,1,2);"
                    "INSERT INTO native_reviews VALUES ('example-store','test','native-one','example-policy',"
                    "'private-reviewer','published','native-revision',1735776000);"
                    "INSERT INTO native_revisions VALUES ('example-store','test','native-revision','native-one',"
                    "'Native',NULL,'Native content',2);",
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            combined = json.loads(request("summary")[1])
            assert combined["contribution_count"] == 4
            assert combined["rating"] == {"average": 11 / 3, "rated_count": 3}
            combined_list = json.loads(request("contributions")[1])
            assert len(combined_list["contributions"]) == 4
            assert any(x["id"] == "h-r" and len(x["replies"]) == 1 for x in combined_list["contributions"])
            assert "private-reviewer" not in json.dumps(combined_list)
            manifest = {
                "schema": "cartray.reviews.identity-links.v1",
                "store": "example-store",
                "environment": "test",
                "links": [
                    {
                        "subject_id": "example-policy",
                        "source_identity": "synthetic-native",
                        "source_contribution_id": "r",
                        "source_revision_id": "r1",
                        "source_snapshot_sha256": digest,
                        "native_review_id": "native-one",
                        "native_revision_id": "native-revision",
                        "evidence_sha256": "a" * 64,
                    }
                ],
            }
            manifest_file = Path(directory) / "identity-manifest.json"
            manifest_file.write_text(json.dumps(manifest))
            link_cmd = [
                sys.executable,
                str(MODULE / "tools/reconcile_identities.py"),
                str(manifest_file),
                "--sha256",
                hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
                "--store",
                "example-store",
                "--environment",
                "test",
            ]
            for _ in range(2):
                subprocess.run(link_cmd, env=env, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            linked_summary = json.loads(request("summary")[1])
            assert linked_summary["contribution_count"] == 3
            assert linked_summary["rating"] == {"average": 3, "rated_count": 2}
            linked_list = json.loads(request("contributions")[1])["contributions"]
            linked = next(item for item in linked_list if item["id"] == "n-native-one")
            assert linked["historical_provenance"]["anchors"] == ["review-r"]
            assert linked["replies"][0]["id"] == "h-a"
            conflicting = json.loads(FIXTURE.read_bytes())
            conflicting["snapshot_id"], conflicting["publication_revision"] = "linked-conflict", 2
            conflicting["contributions"][0]["anchors"] = ["changed-anchor"]
            conflict_file = Path(directory) / "linked-conflict.json"
            conflict_file.write_text(json.dumps(conflicting))
            failed = subprocess.run(
                [
                    sys.executable,
                    str(MODULE / "tools/import_snapshot.py"),
                    str(conflict_file),
                    "--sha256",
                    hashlib.sha256(conflict_file.read_bytes()).hexdigest(),
                    "--store",
                    "example-store",
                    "--environment",
                    "test",
                ],
                env=env,
                cwd=ROOT,
                capture_output=True,
            )
            assert failed.returncode != 0
            assert json.loads(request("summary")[1])["snapshot_id"] == linked_summary["snapshot_id"]
            subprocess.run(
                [
                    *command,
                    "UPDATE native_reviews SET state='withdrawn' WHERE review_id='native-one';"
                    "UPDATE native_publication_versions SET version=version+1, contribution_count=0, "
                    "rated_count=0, rating_sum=0 WHERE subject_id='example-policy';",
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            withdrawn = json.loads(request("contributions")[1])["contributions"]
            assert all(item["id"] not in ("n-native-one", "h-r") for item in withdrawn)
            print("Workerd combined summary:", json.dumps(combined))
            print("Workerd summary:", json.dumps(summary))
            print("Workerd list: three reviews, one unrated, one attached reply")
        finally:
            stop(proc)
        proc = start("wrangler.disabled.test.toml", directory)
        try:
            assert request("summary")[0] == 404
            print("Disabled Worker without D1 binding: 404")
        finally:
            stop(proc)


if __name__ == "__main__":
    main()
