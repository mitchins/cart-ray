import hashlib
import json
import sqlite3

import pytest
from cartray_reviews.snapshot import SnapshotError
from cartray_reviews.storage import SnapshotStore
from test_snapshot import snapshot


def encoded(value):
    data = json.dumps(value).encode()
    return data, hashlib.sha256(data).hexdigest()


def test_replay_conflict_and_stale_revision_keep_published_snapshot():
    store = SnapshotStore(sqlite3.connect(":memory:"))
    store.initialize()
    data, digest = encoded(snapshot())
    args = {"expected_sha256": digest, "store": "ep", "environment": "test"}
    assert store.import_snapshot(data, **args) == "imported"
    assert store.import_snapshot(data, **args) == "unchanged"
    altered = snapshot()
    altered["contributions"][0]["body"] = "Changed"
    changed, changed_hash = encoded(altered)
    with pytest.raises(SnapshotError):
        store.import_snapshot(changed, expected_sha256=changed_hash, store="ep", environment="test")
    assert store.published_bytes(store="ep", environment="test") == data


def test_failed_promotion_rolls_back_snapshot_and_pointer():
    db = sqlite3.connect(":memory:")
    store = SnapshotStore(db)
    store.initialize()
    data, digest = encoded(snapshot())
    store.import_snapshot(data, expected_sha256=digest, store="ep", environment="test")
    db.execute(
        "CREATE TRIGGER block_publication BEFORE UPDATE ON publications BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    db.commit()
    newer = snapshot()
    newer["snapshot_id"] = "two"
    newer["publication_revision"] = 2
    data2, digest2 = encoded(newer)
    with pytest.raises(SnapshotError):
        store.import_snapshot(data2, expected_sha256=digest2, store="ep", environment="test")
    assert store.published_bytes(store="ep", environment="test") == data
    assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


def test_concurrent_revisions_never_regress(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db_path = tmp_path / "reviews.sqlite"
    initial = SnapshotStore(sqlite3.connect(db_path))
    initial.initialize()
    data, digest = encoded(snapshot())
    initial.import_snapshot(data, expected_sha256=digest, store="ep", environment="test")
    initial.connection.close()
    barrier = Barrier(2)

    def promote(revision):
        connection = sqlite3.connect(db_path, timeout=10)
        local_store = SnapshotStore(connection)
        item = snapshot()
        item["snapshot_id"] = f"snapshot-{revision}"
        item["publication_revision"] = revision
        content, content_hash = encoded(item)
        barrier.wait()
        try:
            local_store.import_snapshot(content, expected_sha256=content_hash, store="ep", environment="test")
        except SnapshotError as exc:
            assert str(exc) == "publication revision is stale"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(promote, (2, 3)))
    verify = SnapshotStore(sqlite3.connect(db_path))
    published = json.loads(verify.published_bytes(store="ep", environment="test"))
    assert published["publication_revision"] == 3
