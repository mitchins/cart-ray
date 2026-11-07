"""Loopback-only fake Reviews API for manual UI checks; never use real data."""

import json
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.chdir(Path(__file__).resolve().parent.parent)
REVIEWS = {
    "a" * 64: {
        "review_id": "a" * 64,
        "revision_id": "b" * 64,
        "subject_id": "example-policy",
        "subject_name": "Example policy",
        "author": "Taylor",
        "title": "Helpful resource",
        "body": "Clear and easy to use.",
        "rating": 5,
        "state": "pending",
    },
    "c" * 64: {
        "review_id": "c" * 64,
        "revision_id": "d" * 64,
        "subject_id": "example-service",
        "subject_name": "Example service",
        "author": "Sam",
        "title": "Thoughtful support",
        "body": "The team was helpful.",
        "rating": 4,
        "state": "published",
    },
}


class Handler(SimpleHTTPRequestHandler):
    def send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/reviews-admin/v1/pending", "/reviews-admin/v1/published"):
            kind = path.rsplit("/", 1)[-1]
            rows = [
                {k: v for k, v in item.items() if k != "state"} for item in REVIEWS.values() if item["state"] == kind
            ]
            return self.send_json({kind: rows, "next_after": None})
        match = re.fullmatch(r"/reviews-admin/v1/contributions/([a-f0-9]{64})/(revisions|audit)", path)
        if match and match[1] in REVIEWS:
            item = REVIEWS[match[1]]
            if match[2] == "revisions":
                records = [{k: item[k] for k in ("revision_id", "author", "title", "body", "rating")}]
            else:
                records = [
                    {
                        "event_id": "e" * 64,
                        "revision_id": item["revision_id"],
                        "action": "submit",
                        "actor": "staff:synthetic",
                        "occurred_at": 1700000000,
                    }
                ]
            return self.send_json({"review_id": match[1], match[2]: records, "next_after": None})
        if path.startswith("/reviews-admin/"):
            return self.send_json({"error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        match = re.fullmatch(r"/reviews-admin/v1/contributions/([a-f0-9]{64})/(approve|reject|withdraw)", self.path)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, TypeError):
            return self.send_json({"error": "invalid"}, 400)
        if not match or match[1] not in REVIEWS or body != {"revision_id": REVIEWS[match[1]]["revision_id"]}:
            return self.send_json({"error": "conflict"}, 409)
        item = REVIEWS[match[1]]
        expected = "published" if match[2] == "withdraw" else "pending"
        if item["state"] != expected:
            return self.send_json({"error": "conflict"}, 409)
        item["state"] = {"approve": "published", "reject": "rejected", "withdraw": "withdrawn"}[match[2]]
        return self.send_json({"review_id": match[1], "status": item["state"]})


if __name__ == "__main__":
    print("Synthetic fixture only: http://127.0.0.1:48973/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 48973), Handler).serve_forever()
