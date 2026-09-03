from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cartray.catalogue import CsvCatalogueSourceAdapter, FixturePriceResolver, build_catalogue_from_source
from cartray.models import ResolvedPrice
from cartray.presentation import CsvPresentationSourceAdapter, build_presented_catalogue, validate_presentation_assets

ROOT = Path(__file__).parents[1]
SUBSET = ROOT / "catalogue" / "real-test-subset"


def test_real_test_subset_has_the_approved_products_and_valid_local_presentation():
    sources = asyncio.run(CsvCatalogueSourceAdapter(SUBSET / "catalogue.csv").load())
    presentation_sources = asyncio.run(CsvPresentationSourceAdapter(SUBSET / "catalogue-presentation.csv").load())
    validate_presentation_assets(presentation_sources, ROOT / "storefront" / "assets" / "products")
    raw_expansions = json.loads((SUBSET / "fulfilment-expansions.json").read_text())
    expansions = {key: tuple(value) for key, value in raw_expansions.items()}
    catalogue = asyncio.run(
        build_catalogue_from_source(
            CsvCatalogueSourceAdapter(SUBSET / "catalogue.csv"),
            FixturePriceResolver(
                {
                    "cr_test_ep_sil_2026": ResolvedPrice("price_test_ep_sil_2026", 85000, "aud"),
                    "cr_test_ep_lms_training_catalogue": ResolvedPrice(
                        "price_test_ep_lms_training_catalogue", 0, "aud"
                    ),
                }
            ),
            expansions,
        )
    )
    presented = build_presented_catalogue(catalogue, presentation_sources)

    assert {source.product_key for source in sources} == {"EP-SIL-2026", "EP-LMS-TRAINING-CATALOGUE"}
    assert catalogue.public_manifest()["products"] == [
        {
            "product_key": "EP-LMS-TRAINING-CATALOGUE",
            "title": "LMS Training Catalogue",
            "amount_minor": 0,
            "currency": "aud",
            "max_quantity": 1,
        },
        {
            "product_key": "EP-SIL-2026",
            "title": "Module 5a - Supported Independent Living Policy and Procedure Package",
            "amount_minor": 85000,
            "currency": "aud",
            "max_quantity": 1,
        },
    ]
    assert {product["image_url"] for product in presented.public_manifest()["products"]} == {
        "/assets/products/ep-lms-training-catalogue-cover-v1.webp",
        "/assets/products/ep-sil-2026-cover-v1.webp",
    }
