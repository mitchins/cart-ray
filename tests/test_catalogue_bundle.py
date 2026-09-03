from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from cartray.catalogue import CsvCatalogueSourceAdapter, FixturePriceResolver
from cartray.catalogue_bundle import (
    build_runtime_catalogue,
    build_runtime_presented_catalogue,
    compile_catalogue_bundle,
    render_compiled_catalogue_module,
)
from cartray.compiled_catalogue import COMPILED_CATALOGUE
from cartray.errors import CatalogueValidationError
from cartray.presentation import CsvPresentationSourceAdapter

ROOT = Path(__file__).parents[1]


def test_compiled_bundle_is_the_current_deterministic_rendering_of_the_source_files():
    root = ROOT / "fixtures"
    bundle = asyncio.run(
        compile_catalogue_bundle(
            CsvCatalogueSourceAdapter(root / "catalogue.csv"),
            FixturePriceResolver.from_json(root / "price-resolutions.json"),
            {key: tuple(value) for key, value in json.loads((root / "fulfilment-expansions.json").read_text()).items()},
            CsvPresentationSourceAdapter(root / "catalogue-presentation.csv"),
        )
    )

    assert bundle == COMPILED_CATALOGUE
    assert render_compiled_catalogue_module(bundle) == (ROOT / "src" / "cartray" / "compiled_catalogue.py").read_text()


def test_runtime_catalogue_and_public_manifest_are_built_only_from_the_compiled_bundle():
    resolver = FixturePriceResolver.from_json(ROOT / "fixtures" / "price-resolutions.json")
    catalogue = asyncio.run(build_runtime_catalogue(COMPILED_CATALOGUE, resolver))
    presented = asyncio.run(build_runtime_presented_catalogue(COMPILED_CATALOGUE, resolver))

    assert presented.catalogue == catalogue
    assert presented.presentation_version == COMPILED_CATALOGUE.expected_presentation_version
    assert COMPILED_CATALOGUE.bundle_version not in json.dumps(presented.public_manifest())
    assert "fulfilment_resources" not in json.dumps(presented.public_manifest())


def test_compiled_bundle_rejects_presentation_drift_before_publication():
    resolver = FixturePriceResolver.from_json(ROOT / "fixtures" / "price-resolutions.json")
    stale = replace(COMPILED_CATALOGUE, expected_presentation_version="sha256:stale")

    with pytest.raises(CatalogueValidationError, match="compiled presentation"):
        asyncio.run(build_runtime_presented_catalogue(stale, resolver))


def test_compiler_check_rejects_a_stale_generated_module(tmp_path):
    output = tmp_path / "compiled_catalogue.py"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "compile_catalogue.py"),
        "--catalogue",
        str(ROOT / "fixtures" / "catalogue.csv"),
        "--price-resolutions",
        str(ROOT / "fixtures" / "price-resolutions.json"),
        "--fulfilment-expansions",
        str(ROOT / "fixtures" / "fulfilment-expansions.json"),
        "--presentation",
        str(ROOT / "fixtures" / "catalogue-presentation.csv"),
        "--product-assets",
        str(ROOT / "storefront" / "assets" / "products"),
        "--output",
        str(output),
    ]
    subprocess.run(command, check=True)
    subprocess.run([*command, "--check"], check=True)
    output.write_text("stale")

    checked = subprocess.run([*command, "--check"], capture_output=True, text=True)

    assert checked.returncode != 0
    assert checked.stderr == "compiled catalogue bundle is stale\n"
