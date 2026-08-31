from __future__ import annotations

import re
from pathlib import Path

from cartray.store import SCHEMA


def _normalise_schema(schema: str) -> str:
    return re.sub(r"\s+", " ", schema.replace(" IF NOT EXISTS", "")).strip()


def test_sqlite_schema_matches_initial_d1_migration():
    migration = (Path(__file__).parents[1] / "migrations" / "0001_commerce_kernel.sql").read_text()

    assert _normalise_schema(SCHEMA) == _normalise_schema(migration)
