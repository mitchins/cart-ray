"""Exercise the actual clean qualification build, including locked vendoring."""

import runpy
from pathlib import Path


def test_clean_qualification_build():
    """A clean archived head must produce a bundle containing its runtime sources."""
    script = Path(__file__).resolve().parents[1] / "tools/clean_qualification_build.py"
    runpy.run_path(str(script), run_name="__main__")
