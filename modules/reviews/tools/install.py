"""Explicit, local-only Reviews D1 installer with checksum-verified migrations."""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "modules/reviews"
CONFIG = MODULE / "wrangler.test.toml"


def execute(*arguments):
    """Execute a local D1 command and return Wrangler's structured result."""
    command = [
        str(ROOT / "node_modules/.bin/wrangler"),
        "d1",
        "execute",
        "cartray-reviews-local-test",
        "--config",
        str(CONFIG),
        "--local",
        "--json",
        *arguments,
    ]
    if os.environ.get("REVIEWS_LOCAL_PERSIST_DIR"):
        command.extend(["--persist-to", os.environ["REVIEWS_LOCAL_PERSIST_DIR"]])
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def install():
    """Apply only new migrations; refuse changed or unknown applied versions."""
    migrations = json.loads((MODULE / "module.json").read_text())["migrations"]
    execute(
        "--command",
        "CREATE TABLE IF NOT EXISTS reviews_migration_ledger (version TEXT PRIMARY KEY, checksum TEXT NOT NULL)",
    )
    rows = execute("--command", "SELECT version, checksum FROM reviews_migration_ledger ORDER BY version")[0]["results"]
    recorded = {row["version"]: row["checksum"] for row in rows}
    if not recorded:
        existing = execute(
            "--command",
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('review_subjects','review_snapshots','review_contributions','review_publications')",
        )[0]["results"]
        if existing:
            raise ValueError("unmanaged Reviews tables already exist")
    known = {Path(name).name for name in migrations}
    if set(recorded) - known:
        raise ValueError("unknown applied Reviews migration")
    applied_order = [Path(name).name for name in migrations if Path(name).name in recorded]
    if applied_order != [Path(name).name for name in migrations[: len(applied_order)]]:
        raise ValueError("Reviews migration ledger is not a contiguous prefix")
    for name in migrations:
        path = MODULE / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        version = path.name
        if version in recorded:
            if recorded[version] != digest:
                raise ValueError(f"migration checksum mismatch: {version}")
            continue
        sql = path.read_text() + f"\nINSERT INTO reviews_migration_ledger VALUES ('{version}', '{digest}');\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql") as file:
            file.write(sql)
            file.flush()
            execute("--file", file.name)
    print("Reviews local D1 migrations applied or already current")


if __name__ == "__main__":
    install()
