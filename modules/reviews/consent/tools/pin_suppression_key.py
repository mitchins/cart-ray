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


def open_private(path: Path) -> int:
    """Open a private regular file once, without following its final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid() or info.st_nlink != 1):
        os.close(fd)
        raise ValueError("input must be an owned, singly linked, private regular file")
    return fd


def private_text(path: Path, limit: int) -> str:
    with os.fdopen(open_private(path), "rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("private input too large")
    return data.decode("utf-8")


def create_private(path: Path, value: str) -> None:
    # Operate relative to an already-open private directory, avoiding a
    # check/open race and refusing a symlink as the directory or output.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("output directory must be private and owned")
        if path.name in ("", ".", ".."):
            raise ValueError("invalid output name")
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w") as output:
            output.write(value)
    finally:
        os.close(directory)


def reject_snapshot_sidecars(path: Path) -> None:
    """Reject potentially incomplete SQLite files, including committed WALs."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("snapshot directory must be private and owned")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(path.name + suffix, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("snapshot has SQLite sidecars; supply a standalone checkpointed export")
    finally:
        os.close(directory)


def statement(*, key: str, store: str, installation: str, version: str) -> str:
    if len(key) < 32 or not all(IDENTIFIER.fullmatch(value) for value in (store, installation, version)):
        raise ValueError("invalid key or scope")
    message = json.dumps(
        ["reviews-suppression-key-check-v1", store, "test", installation, version], separators=(",", ":")
    ).encode()
    check = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    return (
        "BEGIN;\n"
        "INSERT INTO review_suppression_key_registry "
        "(store_key,environment,installation_id,key_version,key_check) VALUES "
        f"('{store}','test','{installation}','{version}','{check}');\n"
        "INSERT INTO review_suppression_key_current "
        "(store_key,environment,installation_id,key_version) VALUES "
        f"('{store}','test','{installation}','{version}');\nCOMMIT;\n"
    )


def verify_history(*, db: sqlite3.Connection, key: str, store: str, contacts: list[str] | None) -> None:
    """Check every existing v2 identity against authoritative contacts.

    Caller must supply a verified, quiesced snapshot of the actual Reviews D1;
    this check deliberately refuses populated history without matching proof.
    """
    bindings = db.execute(
        "SELECT DISTINCT recipient_context_hash FROM review_automation_recipient_bindings "
        "WHERE store_key=? AND environment='test' "
        "AND (binding_version='v2' OR binding_version GLOB 'v2_*')", (store,)
    ).fetchall()
    suppressions = db.execute(
        "SELECT DISTINCT recipient_context_hash FROM review_recipient_suppressions "
        "WHERE store_key=? AND environment='test'", (store,)
    ).fetchall()
    existing = {row[0] for row in bindings + suppressions}
    if not existing:
        return
    if not isinstance(contacts, list) or not 1 <= len(contacts) <= 10000:
        raise ValueError("populated history requires complete authoritative contacts")
    candidates = set()
    for email in contacts:
        if not isinstance(email, str) or email != email.strip() or email.count("@") != 1:
            raise ValueError("invalid contact")
        local, domain = email.rsplit("@", 1)
        if not local or not domain or "." not in domain or any(char.isspace() for char in email):
            raise ValueError("invalid contact")
        normalized = local + "@" + domain.lower()
        message = json.dumps(
            ["acorn-recipient-v2", store, "test", normalized], separators=(",", ":"), ensure_ascii=False
        )
        candidates.add(hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest())
    if not existing.issubset(candidates):
        raise ValueError("key or authoritative contacts do not cover existing history")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--installation", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--reviews-snapshot", type=Path, required=True)
    parser.add_argument("--contacts-file", type=Path, help="private JSON array from authoritative purchase source")
    args = parser.parse_args()
    key = private_text(args.key_file, 16384).strip()
    contacts = json.loads(private_text(args.contacts_file, 2_000_000)) if args.contacts_file else None
    reject_snapshot_sidecars(args.reviews_snapshot)
    snapshot = open_private(args.reviews_snapshot)
    try:
        # The validated descriptor pins the snapshot despite path replacement.
        with sqlite3.connect(f"file:/dev/fd/{snapshot}?mode=ro&immutable=1", uri=True) as db:
            verify_history(db=db, key=key, store=args.store, contacts=contacts)
        reject_snapshot_sidecars(args.reviews_snapshot)
    finally:
        os.close(snapshot)
    value = statement(
        key=key, store=args.store, installation=args.installation, version=args.version
    )
    create_private(args.output, value)
    print("Private installation statement created; key material was not written.")


if __name__ == "__main__":
    main()
