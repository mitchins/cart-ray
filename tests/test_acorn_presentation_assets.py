from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_basic_acorn_catalogue_discloses_mockups_and_digital_guide():
    spec = importlib.util.spec_from_file_location("acorn_disclosure_build", ROOT / "examples/acorn-store/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.configuration(ROOT / "examples/acorn-store/store.json")
    products = asyncio.run(module.preview_manifest(config))["products"]
    assert all("fictional mock-ups" in item["short_description"] for item in products)
    guide = next(item for item in products if item["product_key"] == "ACORN-GUIDE")
    assert "digital" in guide["short_description"]


def test_acorn_build_copies_only_declared_product_images_and_other_static_assets(tmp_path):
    spec = importlib.util.spec_from_file_location("acorn_asset_build", ROOT / "examples/acorn-store/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    products = source / "assets/products"
    products.mkdir(parents=True)
    (source / "catalogue-presentation.csv").write_text(
        'product_key,short_description,image_key,image_alt,images_json\n'
        'TEST-ITEM,Description,primary,Cover,"[{""image_key"":""extra.png"",""alt"":""Detail""}]"\n'
    )
    (products / "primary.webp").write_bytes(b"RIFF0000WEBP")
    (products / "extra.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (products / "private-draft.webp").write_bytes(b"RIFF0000WEBP")
    (source / "assets/logo.svg").write_text("<svg/>")
    module.HERE = source
    output = tmp_path / "output"
    module.copy_declared_assets(output)
    assert sorted(path.name for path in (output / "assets/products").iterdir()) == ["extra.png", "primary.webp"]
    assert (output / "assets/logo.svg").read_text() == "<svg/>"
