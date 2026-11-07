"""Qualify the source relay in Workerd/D1 with simulated CF and Reviews services.

This is not hosted identity qualification. It exercises the real source checkpoint
SQL and downstream signed message handling in a local Workerd runtime.
"""

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "modules/reviews/tests/fixtures/producer-workerd/wrangler.toml"
MIGRATION = ROOT / "migrations/0005_reviews_subscriber.sql"


def run():
    with tempfile.TemporaryDirectory(prefix="reviews-producer-workerd-") as directory:
        base = ["npx", "wrangler", "d1", "execute", "DB", "--local", "--config", str(CONFIG), "--persist-to", directory]
        subprocess.run(
            base
            + [
                "--command",
                "CREATE TABLE outbox(id INTEGER PRIMARY KEY,event_type TEXT NOT NULL); "
                "INSERT INTO outbox VALUES(9,'OrderConfirmed'),(11,'OrderConfirmed'),(12,'OrderConfirmed');",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=90,
        )
        subprocess.run(base + ["--file", str(MIGRATION)], cwd=ROOT, check=True, capture_output=True, timeout=90)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(
            [
                "npx",
                "wrangler",
                "dev",
                "--local",
                "--config",
                str(CONFIG),
                "--persist-to",
                directory,
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/run", timeout=5) as response:
                        result = json.load(response)
                    break
                except (urllib.error.URLError, TimeoutError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Workerd producer fixture did not become ready") from None
                    time.sleep(0.2)
            assert result == {
                "first": {"last_outbox_id": 12},
                "second": {"last_outbox_id": 12},
                "ids": [11, 12],
                "receipts": ["acknowledged", "acknowledged"],
            }, result
            print(json.dumps({"runtime": "local Workerd/D1", "post_fence_ids": [11, 12], "replay_duplicates": 0}))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == "__main__":
    run()
