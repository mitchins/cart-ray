"""Exercise the local-only native write/moderate/read lifecycle on Workerd/D1."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from workerd_integration import FIXTURE, MODULE, PORT, ROOT, start, stop

ADMIN = "local-test-secret-0123456789-abcdef0123456789"
BASE = f"http://127.0.0.1:{PORT}"


def call(path, payload=None, *, admin=False, key=None):
    """Call the isolated local test Worker."""
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = "Bearer " + ADMIN
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"non_json_error": body.decode("utf-8", errors="replace")[:2000]}


def main():
    """Prove a guest submission remains private until moderation and withdrawal."""
    with tempfile.TemporaryDirectory(prefix="reviews-native-d1-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory}
        subprocess.run([sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True)

        def query(sql):
            """Read only synthetic test-state aggregates from local D1."""
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
                sql,
            ]
            return json.loads(subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True).stdout)[0][
                "results"
            ]

        proc = start("wrangler.test.toml", directory, "collect", ADMIN)
        try:
            subject = {"subject_id": "example-policy", "kind": "product", "path": "/example-policy/"}
            assert call("/reviews-admin/v1/subjects", subject)[0] == 401

            def incomplete_request(*, authorized):
                """Reject an intentionally stalled client without accepting arbitrary errors."""
                started = time.monotonic()
                with socket.create_connection(("127.0.0.1", PORT), timeout=5) as stalled:
                    stalled.settimeout(5)
                    auth = ("Authorization: Bearer " + ADMIN + "\r\n").encode() if authorized else b""
                    stalled.sendall(
                        b"POST /reviews-admin/v1/subjects HTTP/1.1\r\nHost: localhost\r\n"
                        b"Content-Type: application/json\r\nContent-Length: 10\r\n" + auth + b"\r\n{"
                    )
                    try:
                        response = stalled.recv(1024)
                        while response and b"\r\n" not in response:
                            remaining = 5 - (time.monotonic() - started)
                            if remaining <= 0:
                                raise TimeoutError("stalled request exceeded deadline")
                            stalled.settimeout(remaining)
                            chunk = stalled.recv(1024)
                            if not chunk:
                                break
                            response += chunk
                    except ConnectionResetError:
                        response = b""
                    expected = b"400" if authorized else b"401"
                    assert not response or response.startswith((b"HTTP/1.1 " + expected, b"HTTP/1.0 " + expected)), (
                        response
                    )
                assert time.monotonic() - started < 5

            proxy_diagnostic = os.environ.get("REVIEWS_PROXY_STALL_DIAGNOSTIC") == "1"
            if proxy_diagnostic:
                incomplete_request(authorized=False)
            created = call("/reviews-admin/v1/subjects", subject, admin=True)
            assert created[0] == 201, created
            assert query("SELECT COUNT(*) n FROM native_audit")[0]["n"] == 0
            summary_path = "/reviews/v1/subjects/example-policy/summary"
            list_path = "/reviews/v1/subjects/example-policy/contributions"
            if proxy_diagnostic:
                for _ in range(10):
                    assert proc.poll() is None and call(summary_path)[0] == 200
                    time.sleep(1)
                incomplete_request(authorized=True)
                assert query("SELECT COUNT(*) n FROM review_subjects")[0]["n"] == 1
                assert query("SELECT COUNT(*) n FROM native_audit")[0]["n"] == 0
                for _ in range(10):
                    assert proc.poll() is None and call(summary_path)[0] == 200
                    time.sleep(1)
            assert call(summary_path)[1]["rating"] == {"average": None, "rated_count": 0}
            status, issued = call(
                "/reviews-admin/v1/test-capabilities",
                {"subject_id": "example-policy", "test_reviewer": "guest-a"},
                admin=True,
            )
            assert status == 201
            submission = {
                "subject_id": "example-policy",
                "author": "Example",
                "title": "Private canary title",
                "body": "Private canary text",
                "rating": 2,
                "test_capability": issued["test_capability"],
            }
            status, first = call("/reviews/v1/contributions", submission, key="request-a")
            assert status == 202, (status, first)
            assert call("/reviews-admin/v1/pending")[0] == 401
            pending_response = call("/reviews-admin/v1/pending", admin=True)[1]
            assert pending_response["pending"][0]["body"] == "Private canary text"
            assert issued["test_capability"] not in json.dumps(pending_response)
            admin_request = urllib.request.Request(
                BASE + "/reviews-admin/v1/pending", headers={"Authorization": "Bearer " + ADMIN}
            )
            with urllib.request.urlopen(admin_request, timeout=10) as response:
                assert response.headers.get("Cache-Control") == "no-store"
            with urllib.request.urlopen(BASE + summary_path, timeout=10) as response:
                assert response.headers.get("Cache-Control") == "no-store"
            assert call("/reviews/v1/contributions", submission, key="request-a") == (202, first)
            assert call("/reviews/v1/contributions", submission | {"body": "changed"}, key="request-a")[0] == 409
            assert "Private canary" not in json.dumps(call(list_path)[1])
            assert call(summary_path)[1]["rating"]["rated_count"] == 0
            moderation = "/reviews-admin/v1/contributions/" + first["review_id"] + "/approve"
            assert call(moderation, {"revision_id": first["revision_id"]})[0] == 401
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(
                    pool.map(lambda _: call(moderation, {"revision_id": first["revision_id"]}, admin=True)[0], range(2))
                )
            assert all(value in (200, 409) for value in outcomes) and 200 in outcomes
            assert call(summary_path)[1]["rating"] == {"average": 2, "rated_count": 1}
            status, issued2 = call(
                "/reviews-admin/v1/test-capabilities",
                {"subject_id": "example-policy", "test_reviewer": "guest-b"},
                admin=True,
            )
            assert status == 201
            assert (
                call(
                    "/reviews/v1/contributions",
                    submission | {"test_capability": issued2["test_capability"]},
                    key="request-a",
                )[0]
                == 409
            )
            second = submission | {
                "rating": None,
                "body": "Unrated",
                "title": None,
                "test_capability": issued2["test_capability"],
            }
            status, record2 = call("/reviews/v1/contributions", second, key="request-b")
            assert status == 202, (status, record2)
            assert (
                call(
                    "/reviews-admin/v1/contributions/" + record2["review_id"] + "/approve",
                    {"revision_id": record2["revision_id"]},
                    admin=True,
                )[0]
                == 200
            )
            result = call(summary_path)[1]
            assert result["contribution_count"] == 2 and result["rating"]["rated_count"] == 1
            assert (
                call(
                    "/reviews-admin/v1/contributions/" + first["review_id"] + "/withdraw",
                    {"revision_id": first["revision_id"]},
                    admin=True,
                )[0]
                == 200
            )
            result = call(summary_path)[1]
            assert result["rating"] == {"average": None, "rated_count": 0}
            assert "Private canary" not in json.dumps(call(list_path)[1])
            assert query("SELECT COUNT(*) n FROM native_audit WHERE action='approve'")[0]["n"] == 2
            assert query("SELECT COUNT(*) n FROM native_effect_outbox")[0]["n"] == 3
            assert (
                query("SELECT version FROM native_publication_versions WHERE subject_id='example-policy'")[0]["version"]
                == 3
            )
            assert query(
                "SELECT contribution_count,rated_count,rating_sum FROM native_publication_versions "
                "WHERE subject_id='example-policy'"
            )[0] == {
                "contribution_count": 1,
                "rated_count": 0,
                "rating_sum": 0,
            }
            conflict_import = subprocess.run(
                [
                    sys.executable,
                    str(MODULE / "tools/import_snapshot.py"),
                    str(FIXTURE),
                    "--sha256",
                    hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                    "--store",
                    "example-store",
                    "--environment",
                    "test",
                ],
                cwd=ROOT,
                env=env | {"PYTHONPATH": str(MODULE / "src")},
                capture_output=True,
            )
            assert conflict_import.returncode != 0
            assert call(summary_path)[1]["contribution_count"] == 1

            race_subject = {"subject_id": "race-policy", "kind": "product", "path": "/race/"}
            assert call("/reviews-admin/v1/subjects", race_subject, admin=True)[0] == 201
            proofs = [
                call(
                    "/reviews-admin/v1/test-capabilities",
                    {"subject_id": "race-policy", "test_reviewer": "same-guest"},
                    admin=True,
                )[1]["test_capability"]
                for _ in range(2)
            ]
            race_payloads = [
                submission | {"subject_id": "race-policy", "title": None, "body": None, "test_capability": token}
                for token in proofs
            ]
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(lambda i: call("/reviews/v1/contributions", race_payloads[i], key=f"race-{i}"), range(2))
                )
            assert sorted(status for status, _ in results) == [202, 409]
            winning = next(body for status, body in results if status == 202)
            hashes = [hashlib.sha256(proof.encode()).hexdigest() for proof in proofs]
            used = query(
                "SELECT COUNT(*) n FROM native_capabilities WHERE used_at IS NOT NULL AND token_hash IN "
                f"('{hashes[0]}','{hashes[1]}')"
            )[0]["n"]
            assert used == 1
            race_base = "/reviews-admin/v1/contributions/" + winning["review_id"] + "/"
            with ThreadPoolExecutor(max_workers=2) as pool:
                decisions = list(
                    pool.map(
                        lambda action: call(race_base + action, {"revision_id": winning["revision_id"]}, admin=True),
                        ("approve", "reject"),
                    )
                )
            if sorted(status for status, _ in decisions) != [200, 409]:
                state = query("SELECT state FROM native_reviews WHERE subject_id='race-policy'")
                version = query("SELECT * FROM native_publication_versions WHERE subject_id='race-policy'")
                audit = query("SELECT action FROM native_audit WHERE review_id='" + winning["review_id"] + "'")
                outbox = query("SELECT COUNT(*) n FROM native_effect_outbox WHERE subject_id='race-policy'")
                raise AssertionError((decisions, state, version, audit, outbox))
            assert (
                query("SELECT version FROM native_publication_versions WHERE subject_id='race-policy'")[0]["version"]
                == 1
            )
            assert query("SELECT COUNT(*) n FROM native_effect_outbox WHERE subject_id='race-policy'")[0]["n"] == 1
            assert call(race_base + "withdraw", {"revision_id": "0" * 64}, admin=True)[0] == 409
            scoped = call(
                "/reviews-admin/v1/test-capabilities",
                {"subject_id": "race-policy", "test_reviewer": "scoped"},
                admin=True,
            )[1]["test_capability"]
            assert (
                call("/reviews/v1/contributions", submission | {"test_capability": scoped}, key="wrong-scope")[0] == 409
            )
            expired = call(
                "/reviews-admin/v1/test-capabilities",
                {"subject_id": "race-policy", "test_reviewer": "expired"},
                admin=True,
            )[1]["test_capability"]
            expired_hash = hashlib.sha256(expired.encode()).hexdigest()
            query(f"UPDATE native_capabilities SET expires_at=0 WHERE token_hash='{expired_hash}'")
            assert (
                call(
                    "/reviews/v1/contributions",
                    submission | {"subject_id": "race-policy", "test_capability": expired},
                    key="expired",
                )[0]
                == 409
            )
            assert (
                call(
                    "/reviews-admin/v1/subjects",
                    {"subject_id": "totals", "kind": "product", "path": "/totals/"},
                    admin=True,
                )[0]
                == 201
            )
            records = []
            for index, rating in enumerate((1, 5)):
                proof = call(
                    "/reviews-admin/v1/test-capabilities",
                    {"subject_id": "totals", "test_reviewer": f"totals-{index}"},
                    admin=True,
                )[1]["test_capability"]
                status, record = call(
                    "/reviews/v1/contributions",
                    submission | {"subject_id": "totals", "rating": rating, "test_capability": proof},
                    key=f"totals-{index}",
                )
                assert status == 202
                records.append(record)
            with ThreadPoolExecutor(max_workers=2) as pool:
                approvals = list(
                    pool.map(
                        lambda item: call(
                            "/reviews-admin/v1/contributions/" + item["review_id"] + "/approve",
                            {"revision_id": item["revision_id"]},
                            admin=True,
                        )[0],
                        records,
                    )
                )
            assert approvals == [200, 200]
            totals = call("/reviews/v1/subjects/totals/summary")[1]
            assert totals["rating"] == {"average": 3, "rated_count": 2} and totals["contribution_count"] == 2
            assert query(
                "SELECT contribution_count,rated_count,rating_sum FROM native_publication_versions "
                "WHERE subject_id='totals'"
            )[0] == {"contribution_count": 2, "rated_count": 2, "rating_sum": 6}

            assert (
                call(
                    "/reviews-admin/v1/subjects",
                    {"subject_id": "broken", "kind": "product", "path": "/broken/"},
                    admin=True,
                )[0]
                == 201
            )
            proof = call(
                "/reviews-admin/v1/test-capabilities", {"subject_id": "broken", "test_reviewer": "broken"}, admin=True
            )[1]["test_capability"]
            status, pending = call(
                "/reviews/v1/contributions",
                submission | {"subject_id": "broken", "test_capability": proof},
                key="broken",
            )
            assert status == 202
            query("DELETE FROM native_publication_versions WHERE subject_id='broken'")
            assert (
                call(
                    "/reviews-admin/v1/contributions/" + pending["review_id"] + "/approve",
                    {"revision_id": pending["revision_id"]},
                    admin=True,
                )[0]
                == 409
            )
            assert query("SELECT state FROM native_reviews WHERE subject_id='broken'")[0]["state"] == "pending"
            assert call("/reviews/v1/contributions", submission | {"test_capability": "0" * 64}, key="forged")[0] == 409
            assert any(call("/reviews/v1/contributions", {}, key="flood")[0] == 429 for _ in range(65))
            print(
                "Native lifecycle: pending → 2-star approved → unrated approved → withdrawal; summary",
                json.dumps(result),
            )
        finally:
            stop(proc)
        proc = start("wrangler.test.toml", directory, "read_only", ADMIN)
        try:
            assert call(summary_path)[0] == 200
            assert call("/reviews/v1/contributions", submission, key="read-only")[0] == 405
            denied = call("/reviews-admin/v1/pending", admin=True)
            assert denied[0] == 404, denied
        finally:
            stop(proc)
        proc = start("wrangler.test.toml", directory, "collect", ADMIN, {"REVIEWS_ENVIRONMENT": "live"})
        try:
            assert call(summary_path)[0] == 503
            assert call("/reviews/v1/contributions", submission, key="live")[0] == 503
        finally:
            stop(proc)
        proc = start("wrangler.test.toml", directory, "collect", ADMIN, {"REVIEWS_STORE_KEY": "other-store"})
        try:
            assert call(summary_path)[0] == 404
            assert call(moderation, {"revision_id": first["revision_id"]}, admin=True)[0] == 409
        finally:
            stop(proc)


if __name__ == "__main__":
    main()
