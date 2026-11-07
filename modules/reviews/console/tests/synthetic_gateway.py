"""Loopback-only fake Reviews API for manual UI checks; never use real data."""

import json
import os
import re
import secrets
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
REPLIES = {}
REPLY_REQUESTS = {}
REPLY_AUDIT = {}


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
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/reviews-admin/v1/deliveries":
            state = parse_qs(parsed.query).get("state", [""])[0]
            if state not in ("uncertain", "dispatching", "failed"):
                return self.send_json({"error": "invalid"}, 400)
            records = (
                [{"generation_id": "8" * 64, "delivery_identity": "9" * 64, "state": state, "prepared_at": 1700000000}]
                if state == "uncertain"
                else []
            )
            return self.send_json({"state": state, "deliveries": records, "next_after": None})
        if path in ("/reviews-admin/v1/pending", "/reviews-admin/v1/published"):
            kind = path.rsplit("/", 1)[-1]
            rows = [
                {k: v for k, v in item.items() if k != "state"} for item in REVIEWS.values() if item["state"] == kind
            ]
            return self.send_json({kind: rows, "next_after": None})
        match = re.fullmatch(
            r"/reviews-admin/v1/contributions/([a-f0-9]{64})/(revisions|audit|reply-revisions|reply-audit)", path
        )
        if match and match[1] in REVIEWS:
            item = REVIEWS[match[1]]
            if match[2] in ("reply-revisions", "reply-audit"):
                reply = REPLIES.get(match[1])
                head = (
                    {k: reply[k] for k in ("reply_id", "reply_revision_id", "parent_revision_id", "state")}
                    if reply
                    else None
                )
                key = "reply_revisions" if match[2] == "reply-revisions" else "reply_audit"
                records = (
                    (reply["revisions"] if reply else []) if key == "reply_revisions" else REPLY_AUDIT.get(match[1], [])
                )
                return self.send_json(
                    {
                        "review_id": match[1],
                        "subject_id": item["subject_id"],
                        "state": item["state"],
                        "current_revision_id": item["revision_id"],
                        "reply": head,
                        key: records,
                        "next_after": None,
                    }
                )
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
        match = re.fullmatch(
            r"/reviews-admin/v1/contributions/([a-f0-9]{64})/(approve|reject|withdraw|reply|reply-withdraw)", self.path
        )
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, TypeError):
            return self.send_json({"error": "invalid"}, 400)
        if not match or match[1] not in REVIEWS:
            return self.send_json({"error": "conflict"}, 409)
        item = REVIEWS[match[1]]
        if match[2] in ("reply", "reply-withdraw"):
            key = self.headers.get("Idempotency-Key", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
                return self.send_json({"error": "invalid"}, 400)
            fingerprint = json.dumps([match[1], match[2], body], sort_keys=True)
            if key in REPLY_REQUESTS:
                previous, result = REPLY_REQUESTS[key]
                return self.send_json(result) if previous == fingerprint else self.send_json({"error": "conflict"}, 409)
            old = REPLIES.get(match[1])
            if (
                item["state"] != "published"
                or body.get("parent_revision_id") != item["revision_id"]
                or body.get("expected_reply_revision_id") != (old["reply_revision_id"] if old else None)
                or (old and old["state"] != "published")
            ):
                return self.send_json({"error": "conflict"}, 409)
            if match[2] == "reply":
                revision = secrets.token_hex(32)
                row = {
                    "reply_revision_id": revision,
                    "author": body.get("author", ""),
                    "body": body.get("body", ""),
                    "created_at": 1700000000,
                }
                reply = {
                    "reply_id": old["reply_id"] if old else secrets.token_hex(32),
                    "reply_revision_id": revision,
                    "parent_revision_id": item["revision_id"],
                    "state": "published",
                    "revisions": (old["revisions"] if old else []) + [row],
                }
                REPLIES[match[1]] = reply
                action = "edited" if old else "created"
            elif old:
                old["state"] = "withdrawn"
                reply = old
                action = "withdrawn"
            else:
                return self.send_json({"error": "conflict"}, 409)
            result = {k: reply[k] for k in ("reply_id", "reply_revision_id", "state")}
            REPLY_REQUESTS[key] = (fingerprint, result)
            REPLY_AUDIT.setdefault(match[1], []).append(
                {
                    "event_id": secrets.token_hex(32),
                    "reply_id": reply["reply_id"],
                    "reply_revision_id": reply["reply_revision_id"],
                    "actor": "staff:synthetic",
                    "action": action,
                    "occurred_at": 1700000000,
                }
            )
            return self.send_json(result)
        if body != {"revision_id": item["revision_id"]}:
            return self.send_json({"error": "conflict"}, 409)
        expected = "published" if match[2] == "withdraw" else "pending"
        if item["state"] != expected:
            return self.send_json({"error": "conflict"}, 409)
        item["state"] = {"approve": "published", "reject": "rejected", "withdraw": "withdrawn"}[match[2]]
        return self.send_json({"review_id": match[1], "status": item["state"]})


if __name__ == "__main__":
    print("Synthetic fixture only: http://127.0.0.1:48973/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 48973), Handler).serve_forever()
