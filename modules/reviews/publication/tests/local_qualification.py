"""Exercise R5 against an isolated, actual synthetic R4 Workerd/D1 source."""

import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/reviews"
sys.path.insert(0, str(MODULE / "tests"))
from workerd_integration import FIXTURE, PORT, start, stop  # noqa: E402


def get_source(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}" + path, timeout=10) as response:
        return json.load(response)


def run():
    with tempfile.TemporaryDirectory(prefix="r5-local-qualification-") as name:
        directory = Path(name)
        source_dir, publication_dir = directory / "source", directory / "publication"
        source_dir.mkdir()
        publication_dir.mkdir()
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": str(source_dir), "PYTHONPATH": str(MODULE / "src")}
        subprocess.run(
            [sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True, capture_output=True
        )
        subprocess.run(
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
            env=env,
            check=True,
            capture_output=True,
        )
        source = start("wrangler.test.toml", str(source_dir), "read_only")
        publication = None
        proxy = None
        try:
            subject = get_source("/reviews/v1/subject-lookup?alias=example-policy")
            summary = get_source("/reviews/v1/subjects/example-policy/summary")
            page = get_source("/reviews/v1/subjects/example-policy/contributions?limit=20")
            page["request_after"] = None
            source_data = {
                "subject": {
                    "id": subject["subject_id"],
                    "kind": subject["kind"],
                    "canonical_path": subject["canonical_path"],
                    "name": "Synthetic policy",
                },
                "summary": summary,
                "pages": [page],
            }
            source_file = directory / "projection.json"
            source_file.write_text(json.dumps(source_data))
            config = MODULE / "publication/wrangler.test.toml"
            wrangler = str(ROOT / "node_modules/.bin/wrangler")
            subprocess.run(
                [
                    wrangler,
                    "d1",
                    "execute",
                    "cartray-reviews-publication-local-test",
                    "--local",
                    "--persist-to",
                    str(publication_dir),
                    "--config",
                    str(config),
                    "--file",
                    str(MODULE / "publication/schema.sql"),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
            )
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            token = secrets.token_hex(32)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                proxy_port = listener.getsockname()[1]
            proxy = subprocess.Popen(
                [
                    "node",
                    str(MODULE / "publication/tests/source_proxy.mjs"),
                    f"http://127.0.0.1:{PORT}",
                    str(proxy_port),
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(100):
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", proxy_port)) == 0:
                        break
                if proxy.poll() is not None:
                    raise RuntimeError("source proxy exited")
                time.sleep(0.1)
            else:
                raise RuntimeError("source proxy did not start")
            source_origin = f"http://127.0.0.1:{proxy_port}"
            publication = subprocess.Popen(
                [
                    wrangler,
                    "dev",
                    "--config",
                    str(config),
                    "--local",
                    "--port",
                    str(port),
                    "--persist-to",
                    str(publication_dir),
                    "--var",
                    "PUBLICATION_MODE:test",
                    "--var",
                    f"PUBLICATION_TEST_TOKEN:{token}",
                    "--var",
                    f"REVIEWS_PUBLIC_ORIGIN:{source_origin}",
                    "--var",
                    "REVIEWS_SOURCE_SCOPE:example-store:test",
                    "--var",
                    "PUBLICATION_ALLOW_LOOPBACK:test",
                    "--log-level",
                    "error",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(180):
                if publication.poll() is not None:
                    raise RuntimeError("publication worker exited")
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/publication/html", timeout=1)
                except urllib.error.HTTPError as exc:
                    if exc.code == 503:
                        break
                except (OSError, TimeoutError):
                    time.sleep(0.5)
            else:
                raise RuntimeError("publication worker did not start")
            subprocess.run(
                ["node", str(MODULE / "publication/tests/qualify.mjs"), f"http://127.0.0.1:{port}", str(source_file)],
                cwd=ROOT,
                env=os.environ
                | {
                    "PUBLICATION_TEST_TOKEN": token,
                    "REVIEWS_PUBLIC_ORIGIN": source_origin,
                    "REVIEWS_SOURCE_CONTROL": source_origin,
                },
                check=True,
            )
            check = subprocess.run(
                [
                    wrangler,
                    "d1",
                    "execute",
                    "cartray-reviews-publication-local-test",
                    "--local",
                    "--persist-to",
                    str(publication_dir),
                    "--config",
                    str(config),
                    "--json",
                    "--command",
                    "SELECT (SELECT count(*) FROM publication_media_tombstones) media_count, "
                    "(SELECT count(*) FROM publication_tombstones) lineage_count, "
                    "(SELECT state FROM publication_gate WHERE singleton=1) gate_state",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            state = json.loads(check.stdout)[0]["results"][0]
            if state != {"media_count": 1, "lineage_count": 2, "gate_state": "safe"}:
                raise RuntimeError("withdrawal tombstones were not retained across artifacts")
            print("Retained-artifact media and review/reply lineage tombstones verified.", flush=True)
        finally:
            if publication and publication.poll() is None:
                os.killpg(publication.pid, signal.SIGTERM)
                publication.wait(timeout=10)
            if proxy and proxy.poll() is None:
                os.killpg(proxy.pid, signal.SIGTERM)
                proxy.wait(timeout=10)
            stop(source)


if __name__ == "__main__":
    run()
