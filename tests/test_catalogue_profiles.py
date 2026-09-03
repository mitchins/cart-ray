from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cartray.build_profiles import selected_catalogue_profile

ROOT = Path(__file__).parents[1]


def test_catalogue_profile_selection_is_closed_and_defaults_to_synthetic():
    synthetic = selected_catalogue_profile({})
    real = selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "real-test-subset"})

    assert synthetic.name == "synthetic"
    assert synthetic.price_resolutions == ROOT / "fixtures" / "price-resolutions.json"
    assert real.name == "real-test-subset"
    assert real.preflight_lock == ROOT / "catalogue" / "real-test-subset" / "stripe-test-preflight.lock.json"
    with pytest.raises(ValueError, match="must be one of"):
        selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "../../unreviewed"})


def test_real_test_subset_profile_compiles_from_its_committed_preflight_lock(tmp_path):
    profile = selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "real-test-subset"})
    output = tmp_path / "compiled_catalogue.py"

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "compile_catalogue.py"), *profile.compiler_arguments(output)],
        check=True,
    )

    rendered = output.read_text()
    assert "EP-SIL-2026" in rendered
    assert "EP-LMS-TRAINING-CATALOGUE" in rendered
    assert "sha256:8d3674ae409653d9194f71fcdeb5f7b24f4d8442aac3fd966d0c2612aeba71ee" in rendered


def test_worker_source_sync_materializes_real_profile_without_replacing_synthetic_source(tmp_path):
    from runpy import run_path

    sync = run_path(ROOT / "scripts" / "sync_worker_source.py")
    profile = selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "real-test-subset"})

    sync["_assert_synthetic_source_is_current"]()
    sync["_synchronize"](profile, tmp_path / "cartray")

    selected = (tmp_path / "cartray" / "compiled_catalogue.py").read_text()
    checked_in = (ROOT / "src" / "cartray" / "compiled_catalogue.py").read_text()
    assert "EP-SIL-2026" in selected
    assert "EP-SIL-2026" not in checked_in
