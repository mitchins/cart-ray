#!/usr/bin/env python3
"""Local preview server. Binds loopback only; never a production hosting service."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript", ".js": "text/javascript"}

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).parent / "dist")
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()
    if not (args.directory / "index.html").is_file():
        parser.error("build the preview first")
    print(f"CartRay preview: http://127.0.0.1:{args.port}", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(args.directory))).serve_forever()
    except KeyboardInterrupt:
        pass
