"""Exercise the actual clean qualification build, including locked vendoring."""

import os
import runpy
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("REVIEWS_QUALIFICATION_BUILD") != "1", reason="commit-only qualification")
def test_clean_qualification_build(monkeypatch):
    """A clean archived head must produce a bundle containing its runtime sources."""
    script = Path(__file__).resolve().parents[1] / "tools/clean_qualification_build.py"
    monkeypatch.setattr("sys.argv", [str(script)])
    runpy.run_path(str(script), run_name="__main__")
