"""Loopback-only synthetic browser gateway backed by local Reviews Workerd/D1.

The signed test staff assertion is injected server-side and never sent to the
browser. This is a development fixture, not an authentication proxy for real use.
"""

import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
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
from workerd_integration import MODULE, ROOT  # noqa: E402
from workerd_integration import PORT as WORKER_PORT  # noqa: E402

CONSOLE = Path(__file__).resolve().parent.parent
PORT = 48974
ORIGIN = f"http://127.0.0.1:{PORT}"
STAFF_PATH = re.compile(
    r"/reviews-admin/v1/(?:pending|published|deliveries|contributions/[a-f0-9]{64}/"
    r"(?:approve|reject|withdraw|revisions|audit|reply|reply-withdraw|reply-revisions|reply-audit))"
)
ASSETS = {"/", "/index.html", "/client.mjs", "/console.mjs", "/styles.css"}


def stop_owned(process):
    """Terminate only the process group created for this fixture."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def run_owned(command, *, env):
    """Keep installer children in an owned group so cancellation can reap them."""
    process = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    try:
        result = process.wait(timeout=300)
        if result:
            raise RuntimeError("local fixture setup failed")
    except BaseException:
        stop_owned(process)
        raise


def owns_listener(process):
    """Refuse a listener unless every process holding the port belongs to our worker group."""
    if process.poll() is not None:
        return False
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-tiTCP:{WORKER_PORT}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("cannot identify local Worker listener") from exc
    if result.returncode not in (0, 1):
        raise RuntimeError("cannot identify local Worker listener")
    try:
        pids = [int(line) for line in result.stdout.splitlines() if line.strip()]
        return bool(pids) and all(os.getpgid(pid) == process.pid for pid in pids)
    except (ValueError, ProcessLookupError):
        return False


def owns_marker(process, subject, version):
    """Require both our process group and an unpredictable marker in our disposable D1."""
    if not owns_listener(process):
        return False
    try:
        with urllib.request.urlopen(f"{BASE}/reviews/v1/subjects/{subject}/summary", timeout=2) as response:
            data = json.load(response)
        return data.get("subject_id") == subject and data.get("snapshot_id") == f"native-{version}"
    except (OSError, TimeoutError, ValueError):
        return False


def require_owned(process, subject, version):
    if not owns_marker(process, subject, version):
        raise RuntimeError("local Worker ownership or disposable D1 identity lost")


def start_owned(directory, subject, version, admin_token=None, extra_vars=None):
    """Start the Worker and accept readiness only from our process and marked database."""
    command = [
        "uv",
        "run",
        "pywrangler",
        "dev",
        "--config",
        str(MODULE / "wrangler.test.toml"),
        "--local",
        "--port",
        str(WORKER_PORT),
        "--persist-to",
        directory,
        "--log-level=error",
        "--var",
        "REVIEWS_MODE:collect",
    ]
    if admin_token:
        command.extend(["--var", f"REVIEWS_TEST_ADMIN_TOKEN:{admin_token}"])
    for name, value in (extra_vars or {}).items():
        command.extend(["--var", f"{name}:{value}"])
    output = None if os.environ.get("REVIEWS_TEST_VERBOSE") else subprocess.DEVNULL
    process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=output, start_new_session=True)
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("worker exited before readiness")
            if owns_marker(process, subject, version):
                return process
            time.sleep(0.25)
        raise RuntimeError("owned worker failed readiness")
    except BaseException:
        stop_owned(process)
        raise


class FixtureServer(ThreadingHTTPServer):
    # A client stalled in HTTP headers must not prevent process teardown.
    daemon_threads = True
    block_on_close = False


class Gateway(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CONSOLE), **kwargs)

    def setup(self):
        super().setup()
        # Prevent idle request headers from occupying a fixture connection forever.
        self.connection.settimeout(3)

    def log_message(self, _format, *_args):
        pass  # Never log request headers or synthetic assertions.

    def safe_origin(self):
        return self.headers.get("Host") == f"127.0.0.1:{PORT}" and (
            self.headers.get("Origin") is None or self.headers.get("Origin") == ORIGIN
        )

    def send_test_verification(self):
        """Verify persisted synthetic state after the browser has withdrawn the review."""
        try:
            require_owned(self.server.worker_process, self.server.marker_subject, self.server.marker_version)
        except RuntimeError:
            self.send_error(503)
            return
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": [AUDIENCE], "sub": "staff-1", "iat": now - 1, "exp": now + 120}
        status, audit = signed_call(
            f"/reviews-admin/v1/contributions/{self.server.synthetic_review_id}/audit", assertion=token(claims)
        )
        try:
            require_owned(self.server.worker_process, self.server.marker_subject, self.server.marker_version)
        except RuntimeError:
            self.send_error(503)
            return
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

    def read_bounded_body(self, length, timeout=2):
        """Read one body with an absolute deadline, including slow byte-at-a-time uploads."""
        deadline = time.monotonic() + timeout
        parts = []
        remaining = length
        try:
            while remaining:
                time_left = deadline - time.monotonic()
                if time_left <= 0:
                    raise TimeoutError("request body deadline")
                self.connection.settimeout(time_left)
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    raise EOFError("incomplete request body")
                parts.append(chunk)
                remaining -= len(chunk)
            return b"".join(parts)
        except (OSError, TimeoutError, EOFError):
            self.close_connection = True
            return None
        finally:
            try:
                self.connection.settimeout(3)
            except OSError:
                pass

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
        if self.command == "POST":
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isdigit() or not 1 <= int(raw_length) <= 16384:
                self.send_error(400)
                return
            data = self.read_bounded_body(int(raw_length))
            if data is None:
                self.send_error(408)
                return
            headers["Content-Type"] = self.headers.get("Content-Type", "")
            if path.path.endswith(("/reply", "/reply-withdraw")):
                key = self.headers.get("Idempotency-Key", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
                    self.send_error(400)
                    return
                headers["Idempotency-Key"] = key
        try:
            require_owned(self.server.worker_process, self.server.marker_subject, self.server.marker_version)
        except RuntimeError:
            self.send_error(503)
            return
        if not summary:
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": [AUDIENCE], "sub": "staff-1", "iat": now - 1, "exp": now + 120}
            headers["Cf-Access-Jwt-Assertion"] = token(claims)
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


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments not in ([], ["--operations"]):
        raise SystemExit("usage: workerd_gateway.py [--operations]")
    permissions = ["reviews:moderate"] + (["reviews:operations"] if "--operations" in arguments else [])
    directory = tempfile.mkdtemp(prefix="reviews-console-workerd-")
    process = None
    gateway = None
    try:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory}
        run_owned([sys.executable, str(MODULE / "tools/install.py")], env=env)
        marker_subject = "fixture-" + secrets.token_hex(16)
        marker_version = secrets.randbelow(1_000_000_000) + 1
        marker_sql = (
            "INSERT INTO review_subjects(store_key,environment,subject_id,kind,canonical_path) "
            f"VALUES('example-store','test','{marker_subject}','product','/{marker_subject}/'); "
            "INSERT INTO native_publication_versions(store_key,environment,subject_id,version) "
            f"VALUES('example-store','test','{marker_subject}',{marker_version});"
            "INSERT INTO acquisition_invitations(store_key,environment,acquisition_id,source_event_id,"
            "recipient_context_hash,subject_set_hash,key_version,token_hash,expires_at) "
            f"VALUES('example-store','test','synthetic-delivery','synthetic-event','{'1' * 64}',"
            f"'{'2' * 64}','v1','{'3' * 64}',4102444800); "
            "INSERT INTO invitation_generations(store_key,environment,acquisition_id,generation_id,request_key,"
            "token_hash,key_version,expected_token_hash,expected_revoked,expected_revocation_epoch,"
            "delivery_identity,state,prepared_at) "
            f"VALUES('example-store','test','synthetic-delivery','{'4' * 64}','synthetic-request',"
            f"'{'5' * 64}','v1','{'3' * 64}',0,0,'{'6' * 64}','uncertain',1700000000);"
        )
        run_owned(
            [
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
                marker_sql,
            ],
            env=env,
        )
        process = start_owned(directory, marker_subject, marker_version, ADMIN)
        try:
            require_owned(process, marker_subject, marker_version)
            status, _ = call(
                "/reviews-admin/v1/subjects",
                {"subject_id": "example-policy", "kind": "product", "path": "/example-policy/"},
                admin=True,
            )
            if status != 201:
                raise RuntimeError("synthetic subject setup failed")
            require_owned(process, marker_subject, marker_version)
            status, issued = call(
                "/reviews-admin/v1/test-capabilities",
                {"subject_id": "example-policy", "test_reviewer": "guest"},
                admin=True,
            )
            if status != 201:
                raise RuntimeError("synthetic reviewer setup failed")
            require_owned(process, marker_subject, marker_version)
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
            stop_owned(process)
            process = None
        jwks = {"keys": [{"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}]}
        options = {
            "REVIEWS_STAFF_MODE": "cloudflare_access",
            "REVIEWS_STAFF_ISSUER": ISSUER,
            "REVIEWS_STAFF_AUDIENCE": AUDIENCE,
            "REVIEWS_STAFF_POLICY": json.dumps({"staff-1": permissions}),
            "REVIEWS_STAFF_TEST_JWKS": json.dumps(jwks),
        }
        process = start_owned(directory, marker_subject, marker_version, extra_vars=options)
        gateway = FixtureServer(("127.0.0.1", PORT), functools.partial(Gateway))
        gateway.synthetic_review_id = review["review_id"]
        gateway.worker_process = process
        gateway.marker_subject = marker_subject
        gateway.marker_version = marker_version
        print(f"Synthetic signed-staff Workerd/D1 gateway ready at {ORIGIN}/", flush=True)
        try:
            gateway.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        # Defer further catchable termination until every owned resource is cleaned up.
        previous_term = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            try:
                if gateway is not None:
                    gateway.server_close()
            finally:
                try:
                    if process is not None:
                        stop_owned(process)
                finally:
                    shutil.rmtree(directory)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)


def terminate(_signum, _frame):
    """Convert SIGTERM into an interrupt so fixture cleanup runs."""
    raise KeyboardInterrupt()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    try:
        main()
    except KeyboardInterrupt:
        pass
