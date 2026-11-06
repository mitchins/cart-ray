"""Loopback-only synthetic browser gateway backed by local Reviews Workerd/D1.

The signed test staff assertion is injected server-side and never sent to the
browser. This is a development fixture, not an authentication proxy for real use.
"""

import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2] / "tests"
sys.path.insert(0, str(TESTS))
from native_workerd import ADMIN, BASE, call  # noqa: E402
from staff_workerd import signed_call  # noqa: E402
from test_staff_identity import AUDIENCE, ISSUER, KEY, token  # noqa: E402
from workerd_integration import MODULE, ROOT, start, stop  # noqa: E402

CONSOLE = Path(__file__).resolve().parent.parent
PORT = 48974
ORIGIN = f"http://127.0.0.1:{PORT}"
STAFF_PATH = re.compile(
    r"/reviews-admin/v1/(?:pending|published|contributions/[a-f0-9]{64}/(?:approve|reject|withdraw|revisions|audit))"
)
ASSETS = {"/", "/index.html", "/client.mjs", "/console.mjs", "/styles.css"}


class Gateway(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CONSOLE), **kwargs)

    def log_message(self, _format, *_args):
        pass  # Never log request headers or synthetic assertions.

    def safe_origin(self):
        return self.headers.get("Host") == f"127.0.0.1:{PORT}" and (
            self.headers.get("Origin") is None or self.headers.get("Origin") == ORIGIN
        )

    def send_test_verification(self):
        """Verify persisted synthetic state after the browser has withdrawn the review."""
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": [AUDIENCE], "sub": "staff-1", "iat": now - 1, "exp": now + 120}
        status, audit = signed_call(
            f"/reviews-admin/v1/contributions/{self.server.synthetic_review_id}/audit", assertion=token(claims)
        )
        summary_status, summary = call("/reviews/v1/subjects/example-policy/summary")
        actor = "staff:" + hashlib.sha256((ISSUER + "\x00staff-1").encode()).hexdigest()
        events = audit.get("audit", [])
        counts = {
            action: sum(event.get("action") == action for event in events)
            for action in ("submitted", "approve", "withdraw")
        }
        staff_events = [event for event in events if event.get("action") in ("approve", "withdraw")]
        actor_matches = len(staff_events) == 2 and all(event.get("actor") == actor for event in staff_events)
        verified = (
            status == summary_status == 200
            and audit.get("state") == "withdrawn"
            and summary.get("rating") == {"average": None, "rated_count": 0}
            and summary.get("contribution_count") == 0
            and counts == {"submitted": 1, "approve": 1, "withdraw": 1}
            and actor_matches
        )
        body = json.dumps(
            {
                "verified": verified,
                "state": audit.get("state"),
                "summary": summary,
                "audit_counts": counts,
                "staff_actor_matches": actor_matches,
            }
        ).encode()
        self.send_response(200 if verified else 409)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def proxy(self):
        path = urllib.parse.urlsplit(self.path)
        summary = path.path == "/__test/summary" and self.command == "GET" and not path.query
        allowed = STAFF_PATH.fullmatch(path.path)
        if not self.safe_origin() or not (allowed or summary):
            self.send_error(404)
            return
        if self.command == "POST" and (not allowed or self.headers.get("Origin") != ORIGIN):
            self.send_error(403)
            return
        data = None
        headers = {}
        if not summary:
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": [AUDIENCE], "sub": "staff-1", "iat": now - 1, "exp": now + 120}
            headers["Cf-Access-Jwt-Assertion"] = token(claims)
        if self.command == "POST":
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit() or not 1 <= int(raw_length) <= 16384:
                self.send_error(400)
                return
            data = self.rfile.read(int(raw_length))
            headers["Content-Type"] = self.headers.get("Content-Type", "")
        worker_path = "/reviews/v1/subjects/example-policy/summary" if summary else self.path
        request = urllib.request.Request(BASE + worker_path, data=data, headers=headers, method=self.command)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, TimeoutError):
            self.send_error(503)
            return
        with response:
            body = response.read(128_001)
            if len(body) > 128_000:
                self.send_error(502)
                return
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/__test/verify" and self.safe_origin() and not urllib.parse.urlsplit(self.path).query:
            return self.send_test_verification()
        if path in ASSETS and self.safe_origin():
            return super().do_GET()
        return self.proxy()

    def do_POST(self):
        return self.proxy()


def main():
    with tempfile.TemporaryDirectory(prefix="reviews-console-workerd-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory}
        subprocess.run([sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True)
        process = start("wrangler.test.toml", directory, "collect", ADMIN)
        try:
            status, _ = call(
                "/reviews-admin/v1/subjects",
                {"subject_id": "example-policy", "kind": "product", "path": "/example-policy/"},
                admin=True,
            )
            if status != 201:
                raise RuntimeError("synthetic subject setup failed")
            status, issued = call(
                "/reviews-admin/v1/test-capabilities",
                {"subject_id": "example-policy", "test_reviewer": "guest"},
                admin=True,
            )
            if status != 201:
                raise RuntimeError("synthetic reviewer setup failed")
            status, review = call(
                "/reviews/v1/contributions",
                {
                    "subject_id": "example-policy",
                    "author": "Synthetic customer",
                    "title": "Useful policy",
                    "body": "A synthetic review backed by Workerd and D1.",
                    "rating": 4,
                    "test_capability": issued["test_capability"],
                },
                key="console-browser-synthetic",
            )
            if status != 202:
                raise RuntimeError("synthetic review setup failed")
        finally:
            stop(process)
        jwks = {"keys": [{"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}]}
        options = {
            "REVIEWS_STAFF_MODE": "cloudflare_access",
            "REVIEWS_STAFF_ISSUER": ISSUER,
            "REVIEWS_STAFF_AUDIENCE": AUDIENCE,
            "REVIEWS_STAFF_POLICY": json.dumps({"staff-1": ["reviews:moderate"]}),
            "REVIEWS_STAFF_TEST_JWKS": json.dumps(jwks),
        }
        process = start("wrangler.test.toml", directory, "collect", None, options)
        try:
            gateway = ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(Gateway))
            gateway.synthetic_review_id = review["review_id"]
            print(f"Synthetic signed-staff Workerd/D1 gateway ready at {ORIGIN}/", flush=True)
            try:
                try:
                    gateway.serve_forever()
                except KeyboardInterrupt:
                    pass
            finally:
                gateway.server_close()
        finally:
            stop(process)


if __name__ == "__main__":
    main()
