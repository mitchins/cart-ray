"""Exercise test purchaser authority using local Workerd and a real D1 binding."""

import json
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CONFIG = "modules/reviews/consent/tests/wrangler.authority.toml"
MIGRATION = "modules/reviews/consent/migrations/0001_purchaser_context.sql"


def post(url, data, headers=None):
    request = urllib.request.Request(url, data=data.encode(), headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def main():
    with tempfile.TemporaryDirectory(prefix="acorn-purchaser-d1-") as directory:
        subprocess.run(
            [
                "npx",
                "wrangler",
                "d1",
                "execute",
                "CONTEXT_DB",
                "--local",
                "--config",
                CONFIG,
                "--persist-to",
                directory,
                "--file",
                MIGRATION,
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            ["npx", "wrangler", "dev", "--local", "--config", CONFIG, "--persist-to", directory, "--port", str(port)],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(80):
                try:
                    with urllib.request.urlopen(base, timeout=1):
                        break
                except urllib.error.HTTPError:
                    break
                except (OSError, TimeoutError):
                    time.sleep(0.25)
            status, headers, _ = post(base + "/checkout", "{}", {"Origin": "https://acorn.test"})
            assert status == 200
            cookie = headers.get("Set-Cookie")
            assert cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
            request_headers = {"Content-Type": "application/json", "Cookie": cookie}
            assert post(base + "/review-consent/v1/challenge", "{}", request_headers)[0] == 200
            with urllib.request.urlopen(base + "/__test/code", timeout=5) as response:
                code = json.loads(response.read())["code"]
            assert post(base + "/review-consent/v1/verify", json.dumps({"code": code}), request_headers)[0] == 200
            a = post(base + "/review-consent/v1/grant", '{"accept":true}', request_headers)
            b = post(base + "/review-consent/v1/grant", '{"accept":true}', request_headers)
            assert a[0] == b[0] == 200 and json.loads(a[2]) == json.loads(b[2])
            copied = post(
                base + "/review-consent/v1/context",
                "{}",
                {"Content-Type": "application/json", "Cookie": "__Host-acorn-purchase=cs_test_abcdefghijk123456"},
            )
            assert copied[0] == 503
            print("Workerd/D1: opaque cookie, idempotent consent, copied session rejected")
        finally:
            import os
            import signal

            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == "__main__":
    main()
