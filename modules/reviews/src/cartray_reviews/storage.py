"""Transactional local staging store for immutable approved public snapshots.

This SQLite adapter is a migration rehearsal, not a production D1 adapter.
"""

import hashlib
import sqlite3

from cartray_reviews.snapshot import SnapshotError, load_snapshot


class SnapshotStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
              store TEXT NOT NULL, environment TEXT NOT NULL, snapshot_id TEXT NOT NULL,
              revision INTEGER NOT NULL, digest TEXT NOT NULL, source_digest TEXT NOT NULL,
              contents BLOB NOT NULL,
              PRIMARY KEY (store, environment, snapshot_id),
              UNIQUE (store, environment, revision)
            );
            CREATE TABLE IF NOT EXISTS publications (
              store TEXT NOT NULL, environment TEXT NOT NULL, snapshot_id TEXT NOT NULL,
              PRIMARY KEY (store, environment),
              FOREIGN KEY (store, environment, snapshot_id)
                REFERENCES snapshots(store, environment, snapshot_id)
            );
            """
        )

    def import_snapshot(self, data: bytes, *, expected_sha256: str, store: str, environment: str):
        """Validate first, then atomically store and promote, or do nothing on failure.

        Caller supplies the digest of an independently approved artifact. Revision
        collisions or reusing a snapshot identity with changed bytes are conflicts.
        """
        value = load_snapshot(data, expected_sha256=expected_sha256, store=store, environment=environment)
        snapshot_id = value["snapshot_id"]
        revision = value["publication_revision"]
        if self.connection.in_transaction:
            raise SnapshotError("import requires a connection without an active transaction")
        self.connection.execute("BEGIN IMMEDIATE")
        with self.connection:
            previous = self.connection.execute(
                "SELECT digest FROM snapshots WHERE store=? AND environment=? AND snapshot_id=?",
                (store, environment, snapshot_id),
            ).fetchone()
            if previous and previous[0] != expected_sha256:
                raise SnapshotError("snapshot identity conflict")
            current = self.connection.execute(
                "SELECT s.revision, s.snapshot_id FROM publications p JOIN snapshots s "
                "ON s.store=p.store AND s.environment=p.environment AND s.snapshot_id=p.snapshot_id "
                "WHERE p.store=? AND p.environment=?",
                (store, environment),
            ).fetchone()
            if current and revision < current[0]:
                raise SnapshotError("publication revision is stale")
            if current and revision == current[0] and snapshot_id != current[1]:
                raise SnapshotError("publication revision conflict")
            try:
                self.connection.execute(
                    "INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (store, environment, snapshot_id, revision, expected_sha256, value["source"]["sha256"], data),
                )
                saved = self.connection.execute(
                    "SELECT digest FROM snapshots WHERE store=? AND environment=? AND snapshot_id=?",
                    (store, environment, snapshot_id),
                ).fetchone()
                if not saved or saved[0] != expected_sha256:
                    raise SnapshotError("publication revision conflict")
                self.connection.execute(
                    "INSERT INTO publications VALUES (?, ?, ?) ON CONFLICT(store, environment) "
                    "DO UPDATE SET snapshot_id=excluded.snapshot_id",
                    (store, environment, snapshot_id),
                )
            except sqlite3.IntegrityError as exc:
                raise SnapshotError("snapshot conflict") from exc
        return "unchanged" if previous else "imported"

    def published_bytes(self, *, store: str, environment: str):
        row = self.connection.execute(
            "SELECT s.contents, s.digest FROM publications p JOIN snapshots s "
            "ON s.store=p.store AND s.environment=p.environment AND s.snapshot_id=p.snapshot_id "
            "WHERE p.store=? AND p.environment=?",
            (store, environment),
        ).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[0]).hexdigest() != row[1]:
            raise SnapshotError("stored snapshot digest mismatch")
        return row[0]
