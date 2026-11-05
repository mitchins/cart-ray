import json
import sqlite3
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]


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
