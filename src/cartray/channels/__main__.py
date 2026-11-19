"""Offline projection and reconciliation tooling; never writes to channel accounts."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .feed import google_feed, meta_feed
from .projection import compare, validate


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)


def _run(argv=None):
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
        try:
            (google_feed if data["channel"] == "google" else meta_feed)(data)
        except ValueError as error:
            print(json.dumps({"valid": True, "locally_exportable": False, "reason": str(error)}))
            return 1
        print(json.dumps({"valid": True, "locally_exportable": True, "items": len(data["items"])}))
    elif args.command == "export":
        data = validate(read_json(args.snapshot))
        content = (google_feed if data["channel"] == "google" else meta_feed)(data)
        output = Path(args.output)
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".channel-feed-", delete=False) as stream:
                name = stream.name
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(name, output)  # Atomic and never overwrites an existing artifact.
        finally:
            if name is not None:
                os.unlink(name)
        print(json.dumps({"path": str(output), "bytes": len(content)}))
    else:
        result = compare(
            read_json(args.source), read_json(args.candidate), read_json(args.exceptions) if args.exceptions else ()
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ready"] else 1
    return 0


def main(argv=None):
    try:
        return _run(argv)
    except (ValueError, OSError, TypeError, RecursionError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
