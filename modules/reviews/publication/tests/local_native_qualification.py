"""Exercise opt-in native revision reads against separate local Reviews and publication D1 databases."""

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
from native_workerd import ADMIN, BASE, call  # noqa: E402
from workerd_integration import PORT, start, stop  # noqa: E402


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.load(response)


def main():
    with tempfile.TemporaryDirectory(prefix="reviews-native-publication-") as temporary:
        root = Path(temporary)
        source_dir, publication_dir = root / "source", root / "publication"
        source_dir.mkdir()
        publication_dir.mkdir()
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": str(source_dir), "PYTHONPATH": str(MODULE / "src")}
        subprocess.run(
            [sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True, capture_output=True
        )
        source = start("wrangler.test.toml", str(source_dir), "collect", ADMIN)
        publication = proxy = None
        try:
            assert (
                call(
                    "/reviews-admin/v1/subjects",
                    {"subject_id": "example-policy", "kind": "product", "path": "/example-policy/"},
                    admin=True,
                )[0]
                == 201
            )
            status, capability = call(
                "/reviews-admin/v1/test-capabilities",
                {"subject_id": "example-policy", "test_reviewer": "guest-a"},
                admin=True,
            )
            assert status == 201
            status, review = call(
                "/reviews/v1/contributions",
                {
                    "subject_id": "example-policy",
                    "author": "Synthetic",
                    "title": None,
                    "body": "Synthetic greenfield review",
                    "rating": 4,
                    "test_capability": capability["test_capability"],
                },
                key="greenfield-test",
            )
            assert status == 202
            assert (
                call(
                    "/reviews-admin/v1/contributions/" + review["review_id"] + "/approve",
                    {"revision_id": review["revision_id"]},
                    admin=True,
                )[0]
                == 200
            )
            subject = get("/reviews/v1/subject-lookup?alias=example-policy")
            page = get("/reviews/v1/subjects/example-policy/contributions?limit=20")
            page["request_after"] = None
            fixture = {
                "subject": {
                    "id": subject["subject_id"],
                    "kind": subject["kind"],
                    "canonical_path": subject["canonical_path"],
                    "name": "Synthetic product",
                },
                "summary": get("/reviews/v1/subjects/example-policy/summary"),
                "pages": [page],
            }
            fixture_path = root / "projection.json"
            fixture_path.write_text(json.dumps(fixture))
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
            proxy_port, gate_port = port(), port()
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
                raise RuntimeError("source proxy unavailable")
            source_origin, token = f"http://127.0.0.1:{proxy_port}", secrets.token_hex(32)
            publication = subprocess.Popen(
                [
                    wrangler,
                    "dev",
                    "--config",
                    str(config),
                    "--local",
                    "--port",
                    str(gate_port),
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
                    "--var",
                    "PUBLICATION_TEST_NATIVE_REVISION_CHECK:enabled",
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
                    urllib.request.urlopen(f"http://127.0.0.1:{gate_port}/publication/html", timeout=1)
                except urllib.error.HTTPError as exc:
                    if exc.code == 503:
                        break
                except (OSError, TimeoutError):
                    time.sleep(0.5)
            else:
                raise RuntimeError("publication worker unavailable")
            subprocess.run(
                [
                    "node",
                    str(MODULE / "publication/tests/qualify_native.mjs"),
                    f"http://127.0.0.1:{gate_port}",
                    BASE,
                    source_origin,
                    str(fixture_path),
                ],
                cwd=ROOT,
                check=True,
                env=os.environ
                | {
                    "PUBLICATION_TEST_TOKEN": token,
                    "REVIEWS_TEST_ADMIN_TOKEN": ADMIN,
                    "REVIEWS_TEST_REVISION": review["revision_id"],
                },
            )
        finally:
            for process in (publication, proxy):
                if process and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
            stop(source)


if __name__ == "__main__":
    main()
