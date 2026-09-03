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
from cartray.preflight import preflight_lock_output
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


def test_preflight_locked_bundle_rejects_a_runtime_price_change():
    root = ROOT / "fixtures"
    resolver = FixturePriceResolver.from_json(root / "price-resolutions.json")
    unlocked = asyncio.run(
        compile_catalogue_bundle(
            CsvCatalogueSourceAdapter(root / "catalogue.csv"),
            resolver,
            {key: tuple(value) for key, value in json.loads((root / "fulfilment-expansions.json").read_text()).items()},
            CsvPresentationSourceAdapter(root / "catalogue-presentation.csv"),
        )
    )
    reviewed_version = asyncio.run(build_runtime_catalogue(unlocked, resolver)).version
    locked = replace(unlocked, expected_catalogue_version=reviewed_version)
    changed = FixturePriceResolver(
        {
            **resolver.prices,
            "cr_test_template": replace(resolver.prices["cr_test_template"], amount_minor=2600),
        }
    )

    assert asyncio.run(build_runtime_catalogue(locked, resolver)).version == locked.expected_catalogue_version
    with pytest.raises(CatalogueValidationError, match="runtime catalogue does not match reviewed preflight"):
        asyncio.run(build_runtime_catalogue(locked, changed))


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


def test_compiler_accepts_an_exact_preflight_lock(tmp_path):
    root = ROOT / "fixtures"
    resolver = FixturePriceResolver.from_json(root / "price-resolutions.json")
    bundle = asyncio.run(
        compile_catalogue_bundle(
            CsvCatalogueSourceAdapter(root / "catalogue.csv"),
            resolver,
            {key: tuple(value) for key, value in json.loads((root / "fulfilment-expansions.json").read_text()).items()},
            CsvPresentationSourceAdapter(root / "catalogue-presentation.csv"),
        )
    )
    lock_path = tmp_path / "catalogue-preflight.lock.json"
    lock_path.write_text(json.dumps(preflight_lock_output(asyncio.run(build_runtime_catalogue(bundle, resolver)))))
    output = tmp_path / "compiled_catalogue.py"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "compile_catalogue.py"),
        "--catalogue",
        str(root / "catalogue.csv"),
        "--preflight-lock",
        str(lock_path),
        "--fulfilment-expansions",
        str(root / "fulfilment-expansions.json"),
        "--presentation",
        str(root / "catalogue-presentation.csv"),
        "--product-assets",
        str(ROOT / "storefront" / "assets" / "products"),
        "--output",
        str(output),
    ]

    subprocess.run(command, check=True)

    assert 'expected_catalogue_version=_PAYLOAD["expected_catalogue_version"]' in output.read_text()
