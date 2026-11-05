"""Run synthetic-only hosted lifecycle and stalled-upload probes against a disposable Worker."""

import http.client
import json
import select
import socket
import ssl
import sys
import time
from pathlib import Path

host = sys.argv[1]
assert host.startswith("cartray-r3a-reviews-") and host.endswith(".workers.dev")
token = json.loads(Path(sys.argv[2]).read_text())["REVIEWS_TEST_ADMIN_TOKEN"]
ua = "Mozilla/5.0 synthetic qualification"


def call(path, data=None, admin=False, key=None, raw=False):
    c = http.client.HTTPSConnection(host, timeout=20)
    h = {"User-Agent": ua, "Content-Type": "application/json"}
    if admin:
        h["Authorization"] = "Bearer " + token
    if key:
        h["Idempotency-Key"] = key
    body = None if data is None else (data if raw else json.dumps(data))
    c.request("GET" if data is None else "POST", path, body=body, headers=h)
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, json.loads(b)


subject = {"subject_id": "policy", "kind": "product", "path": "/policy/"}
assert call("/reviews-admin/v1/subjects", subject, True)[0] == 201
assert call("/reviews-admin/v1/subjects", "{", True, raw=True)[0] == 400
assert call("/reviews-admin/v1/subjects", "{", False, raw=True)[0] == 401
summary = "/reviews/v1/subjects/policy/summary"
listing = "/reviews/v1/subjects/policy/contributions"
reviews = []
for guest, rating in [("a", 2), ("b", None)]:
    status, proof = call("/reviews-admin/v1/test-capabilities", {"subject_id": "policy", "test_reviewer": guest}, True)
    assert status == 201
    payload = {
        "subject_id": "policy",
        "author": "Guest",
        "body": "Synthetic " + guest,
        "rating": rating,
        "test_capability": proof["test_capability"],
    }
    status, review = call("/reviews/v1/contributions", payload, key="request-" + guest)
    assert status == 202
    reviews.append(review)
    if guest == "a":
        assert call(summary)[1]["rating"] == {"average": None, "rated_count": 0}
    status, _ = call(
        "/reviews-admin/v1/contributions/" + review["review_id"] + "/approve",
        {"revision_id": review["revision_id"]},
        True,
    )
    assert status == 200
print("after approval:", call(summary), flush=True)
assert call(summary)[1]["contribution_count"] == 2 and call(summary)[1]["rating"] == {"average": 2, "rated_count": 1}
status, _ = call(
    "/reviews-admin/v1/contributions/" + reviews[0]["review_id"] + "/withdraw",
    {"revision_id": reviews[0]["revision_id"]},
    True,
)
assert status == 200
status, result = call(summary)
assert result["rating"] == {"average": None, "rated_count": 0} and result["contribution_count"] == 1
assert "Synthetic a" not in json.dumps(call(listing)[1])
print("after withdrawal:", status, result, flush=True)


def health():
    return call(summary)[0]


for auth in (False, True):
    with socket.create_connection((host, 443), timeout=10) as s:
        with ssl.create_default_context().wrap_socket(s, server_hostname=host) as sock:
            header = ("Authorization: Bearer " + token + "\r\n") if auth else ""
            sock.sendall(
                (
                    f"POST /reviews-admin/v1/subjects HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {ua}\r\n"
                    f"Content-Length: 10\r\nContent-Type: application/json\r\n{header}\r\n{{"
                ).encode()
            )
            start = time.monotonic()
            outcome = "client deadline"
            while time.monotonic() - start < 90:
                ready, _, _ = select.select([sock], [], [], 5)
                if ready:
                    try:
                        data = sock.recv(256)
                        if data:
                            expected = b"400" if auth else b"401"
                            assert data.startswith((b"HTTP/1.1 " + expected, b"HTTP/1.0 " + expected)), repr(data)
                        outcome = repr(data)
                    except (OSError, ssl.SSLError) as e:
                        outcome = type(e).__name__
                    break
                status = health()
                assert status == 200
                print("during", auth, round(time.monotonic() - start, 1), status, flush=True)
            print("termination", auth, round(time.monotonic() - start, 1), outcome, flush=True)
            assert outcome != "client deadline"
    statuses = [health() for _ in range(10)]
    assert statuses == [200] * 10
    print("health after", auth, statuses, flush=True)
