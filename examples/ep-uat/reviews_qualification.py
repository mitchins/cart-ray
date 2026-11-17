#!/usr/bin/env python3
"""Qualify an explicit UAT review subject against the real local Reviews Worker/D1."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "modules/reviews"
spec = importlib.util.spec_from_file_location("reviews_workerd", MODULE / "tests/workerd_integration.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class Handler(SimpleHTTPRequestHandler):
    """Local test-only same-origin forwarding of the public Reviews endpoints."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if not self.path.startswith("/reviews/v1/"):
            return super().do_GET()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{worker.PORT}{self.path}", timeout=5) as response:
                content = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        except (OSError, urllib.error.HTTPError):
            self.send_error(503)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ep-uat-reviews-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory, "PYTHONPATH": str(MODULE / "src")}
        subprocess.run([sys.executable, str(MODULE / "tools/install.py")], env=env, cwd=ROOT, check=True)
        subprocess.run(
            [sys.executable, str(MODULE / "tools/import_snapshot.py"), str(worker.FIXTURE),
             "--sha256", hashlib.sha256(worker.FIXTURE.read_bytes()).hexdigest(),
             "--store", "example-store", "--environment", "test"], env=env, cwd=ROOT, check=True
        )
        proc = worker.start("wrangler.test.toml", directory, "read_only")
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(args.site)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stopped = False
        try:
            args.output.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page()
                requests = []
                page.on(
                    "request", lambda request: requests.append(request.url) if "/reviews/v1/" in request.url else None
                )
                base = f"http://127.0.0.1:{server.server_port}"
                page.goto(base + "/products/woo-171/", wait_until="load")
                page.get_by_text("4.5 out of 5 · 2 ratings · 3 contributions").wait_for()
                assert len(requests) == 2
                assert page.locator("#review-list article").count() == 3
                page.screenshot(path=str(args.output / "mapped-native-reviews.png"))
                requests.clear()
                page.goto(base + "/products/woo-131/", wait_until="load")
                assert page.locator("#reviews").count() == 0 and not requests
                worker.stop(proc)
                stopped = True
                page.goto(base + "/products/woo-171/", wait_until="load")
                page.get_by_text("Reviews are temporarily unavailable. Please refresh to try again.").wait_for()
                browser.close()
            print("Synthetic public Reviews: mapped summary/list passed; unmapped silent; outage safe.")
        finally:
            if not stopped:
                worker.stop(proc)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
