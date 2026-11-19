"""Offline projection and reconciliation tooling; never writes to channel accounts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .feed import google_feed, meta_feed
from .projection import compare, validate


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate, export or compare merchant channel snapshots offline")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate")
    check.add_argument("snapshot")
    export = sub.add_parser("export")
    export.add_argument("snapshot")
    export.add_argument("output")
    diff = sub.add_parser("compare")
    diff.add_argument("source")
    diff.add_argument("candidate")
    diff.add_argument("--exceptions")
    args = parser.parse_args(argv)
    if args.command == "validate":
        data = validate(read_json(args.snapshot))
        print(json.dumps({"valid": True, "items": len(data["items"])}))
    elif args.command == "export":
        data = validate(read_json(args.snapshot))
        content = (google_feed if data["channel"] == "google" else meta_feed)(data)
        output = Path(args.output)
        with output.open("xb") as stream:
            stream.write(content)
        print(json.dumps({"path": str(output), "bytes": len(content)}))
    else:
        result = compare(
            read_json(args.source), read_json(args.candidate), read_json(args.exceptions) if args.exceptions else ()
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ready"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
