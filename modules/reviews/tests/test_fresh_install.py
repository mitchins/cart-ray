import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]


def test_rate_maintenance_upgrades_database_without_modifying_applied_migration():
    """An existing 0007 ledger entry remains valid when 0008 is installed."""
    descriptor = json.loads((MODULE / "module.json").read_text())
    old = MODULE / "migrations/0007_acquisition_rate_limits.sql"
    assert (
        hashlib.sha256(old.read_bytes()).hexdigest()
        == "30447c391008899b18c3e54bde6ca4837de0439690710aae8979ddf9397ac397"
    )
    index = descriptor["migrations"].index("migrations/0007_acquisition_rate_limits.sql")
    assert descriptor["migrations"][index + 1] == "migrations/0008_acquisition_rate_maintenance.sql"
    connection = sqlite3.connect(":memory:")
    connection.executescript(old.read_text())
    connection.execute("INSERT INTO acquisition_rate_backstop VALUES ('store','test',1,1)")
    connection.executescript((MODULE / descriptor["migrations"][index + 1]).read_text())
    assert connection.execute("SELECT count FROM acquisition_rate_backstop").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM acquisition_rate_maintenance").fetchone() == (0,)


def test_fresh_install_is_explicit_disabled_and_has_no_import_prerequisite():
    """A fresh native database installs without legacy data or implicit activation."""
    descriptor = json.loads((MODULE / "module.json").read_text())
    assert descriptor["installation"] == "explicit"
    assert descriptor["default_mode"] == "disabled"
    assert "verified_acquisition_only" in descriptor["supported_modes"]
    assert any("reviews-internal/v1/settlements" in route for route in descriptor["routes"])
    assert descriptor["subscriptions"] == descriptor["scheduled_tasks"] == []
    connection = sqlite3.connect(":memory:")
    migration = (MODULE / descriptor["migrations"][0]).read_text()
    connection.executescript(migration)
    connection.executescript(migration)
    assert connection.execute("SELECT COUNT(*) FROM review_subjects").fetchone()[0] == 0
    connection.execute(
        "INSERT INTO review_subjects VALUES (?, ?, ?, ?, ?)", ("new-shop", "test", "product-1", "product", "/item/")
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM review_subjects WHERE store_key=? AND environment=?", ("new-shop", "live")
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO review_subjects VALUES (?, ?, ?, ?, ?)", ("new-shop", "test", "product-1", "product", "/item/")
        )


def test_scope_constraints_and_invalid_configuration():
    """Enforce environment and kind constraints while keeping scopes separate."""
    connection = sqlite3.connect(":memory:")
    connection.executescript((MODULE / "migrations/0001_native_subjects.sql").read_text())
    for store, environment in (("shop-a", "test"), ("shop-a", "live"), ("shop-b", "test")):
        connection.execute(
            "INSERT INTO review_subjects VALUES (?, ?, ?, ?, ?)", (store, environment, "same", "product", "/item/")
        )
    assert connection.execute("SELECT COUNT(*) FROM review_subjects").fetchone()[0] == 3
    for values in (
        ("shop", "wrong", "x", "product", "/"),
        ("shop", "test", "x", "invalid", "/"),
        ("shop", "test", "", "product", "/"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO review_subjects VALUES (?, ?, ?, ?, ?)", values)


def test_core_import_does_not_load_reviews_package():
    """Core checkout imports successfully without Reviews on its module path."""
    import os
    import subprocess
    import sys

    repository = MODULE.parents[1]
    env = os.environ | {"PYTHONPATH": str(repository / "src")}
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import cartray.worker, sys; assert not any(n.startswith('cartray_reviews') for n in sys.modules)",
        ],
        cwd=repository,
        env=env,
        check=True,
    )


def test_generation_migration_preserves_pending_delivery():
    descriptor = json.loads((MODULE / "module.json").read_text())
    connection = sqlite3.connect(":memory:")
    for name in descriptor["migrations"]:
        if name.endswith("0010_invitation_delivery_claim.sql"):
            break
        connection.executescript((MODULE / name).read_text())
    connection.execute(
        "INSERT INTO acquisition_invitations "
        "(store_key,environment,acquisition_id,source_event_id,recipient_context_hash,subject_set_hash,"
        "key_version,token_hash,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("store", "test", "purchase", "event", "a" * 64, "b" * 64, "v1", "c" * 64, 0),
    )
    connection.execute(
        "INSERT INTO invitation_generations "
        "(store_key,environment,acquisition_id,generation_id,request_key,token_hash,key_version,"
        "expected_token_hash,expected_revoked,expected_revocation_epoch,delivery_identity,state,prepared_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'prepared',?)",
        ("store", "test", "purchase", "d" * 64, "request", "e" * 64, "v1", "c" * 64, 0, 0, "f" * 64, 1),
    )
    connection.commit()
    connection.executescript((MODULE / "migrations/0010_invitation_delivery_claim.sql").read_text())
    assert connection.execute("SELECT state,delivery_identity FROM invitation_generations").fetchone() == (
        "prepared",
        "f" * 64,
    )
    connection.execute("UPDATE invitation_generations SET state='dispatching'")
    assert connection.execute("SELECT state FROM invitation_generations").fetchone() == ("dispatching",)
