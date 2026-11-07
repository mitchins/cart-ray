#!/usr/bin/env python3
"""Prepare a private, one-time test installation statement; never rotate in place."""
import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from pathlib import Path

IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def statement(*, key: str, store: str, installation: str, version: str) -> str:
    if len(key) < 32 or not all(IDENTIFIER.fullmatch(value) for value in (store, installation, version)):
        raise ValueError("invalid key or scope")
    message = json.dumps(
        ["reviews-suppression-key-check-v1", store, "test", installation, version], separators=(",", ":")
    ).encode()
    check = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return (
        "INSERT INTO review_suppression_key_registry "
        "(store_key,environment,installation_id,key_version,key_check) VALUES "
        f"('{store}','test','{installation}','{version}','{check}');\n"
    )


def verify_history(*, db: sqlite3.Connection, key: str, store: str, known_email: str | None) -> None:
    """Check a known authoritative contact against the existing v2 history.

    Caller must supply a verified, quiesced snapshot of the actual Reviews D1;
    this check deliberately refuses populated history without matching proof.
    """
    bindings = db.execute(
        "SELECT COUNT(*) FROM review_automation_recipient_bindings "
        "WHERE store_key=? AND environment='test' AND binding_version='v2'", (store,)
    ).fetchone()[0]
    suppressions = db.execute(
        "SELECT COUNT(*) FROM review_recipient_suppressions WHERE store_key=? AND environment='test'", (store,)
    ).fetchone()[0]
    if bindings == 0 and suppressions == 0:
        return
    if not known_email or known_email != known_email.strip() or known_email.count("@") != 1:
        raise ValueError("populated history requires an authoritative known contact")
    local, domain = known_email.rsplit("@", 1)
    if not local or not domain or "." not in domain or any(char.isspace() for char in known_email):
        raise ValueError("invalid known contact")
    normalized = local + "@" + domain.lower()
    message = json.dumps(["acorn-recipient-v2", store, "test", normalized], separators=(",", ":"), ensure_ascii=False)
    candidate = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    match = db.execute(
        "SELECT 1 FROM review_automation_recipient_bindings WHERE store_key=? AND environment='test' "
        "AND binding_version='v2' AND recipient_context_hash=? UNION ALL "
        "SELECT 1 FROM review_recipient_suppressions WHERE store_key=? AND environment='test' "
        "AND recipient_context_hash=? LIMIT 1", (store, candidate, store, candidate)
    ).fetchone()
    if not match:
        raise ValueError("key does not match verified existing contact")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--installation", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--reviews-snapshot", type=Path, required=True)
    parser.add_argument("--known-contact-file", type=Path)
    args = parser.parse_args()
    if stat.S_IMODE(args.key_file.stat().st_mode) != 0o600:
        raise SystemExit("key file must have mode 0600")
    if stat.S_IMODE(args.reviews_snapshot.stat().st_mode) != 0o600:
        raise SystemExit("verified Reviews snapshot must have mode 0600")
    if args.known_contact_file and stat.S_IMODE(args.known_contact_file.stat().st_mode) != 0o600:
        raise SystemExit("known contact file must have mode 0600")
    key = args.key_file.read_text().strip()
    known_email = args.known_contact_file.read_text().strip() if args.known_contact_file else None
    with sqlite3.connect(args.reviews_snapshot.resolve().as_uri() + "?mode=ro", uri=True) as db:
        verify_history(db=db, key=key, store=args.store, known_email=known_email)
    value = statement(
        key=key, store=args.store, installation=args.installation, version=args.version
    )
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(value)
    print("Private installation statement created; key material was not written.")


if __name__ == "__main__":
    main()
