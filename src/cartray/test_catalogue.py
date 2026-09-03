from __future__ import annotations

from .catalogue import StaticCatalogueSourceAdapter
from .models import CatalogSource, PresentationSource
from .presentation import PresentationSourceAdapter

TEST_CATALOGUE_SOURCES = (
    CatalogSource(
        "TEST-TEMPLATE", "Test Digital Template", "cr_test_template", "download", "test-template-v1", 1, True
    ),
    CatalogSource("TEST-BUNDLE", "Test Resource Bundle", "cr_test_bundle", "bundle", "test-bundle-v1", 1, True),
    CatalogSource("TEST-FREE", "Test Free Resource", "cr_test_free", "download", "test-free-v1", 1, True),
    CatalogSource(
        "TEST-SUPPORT-HOURS",
        "Test Support Hours",
        "cr_test_support_hours",
        "service",
        "test-support-hours-v1",
        5,
        True,
    ),
)

TEST_FULFILMENT_EXPANSIONS = {
    "test-template-v1": ("TEST-RESOURCE-TEMPLATE",),
    "test-bundle-v1": ("TEST-RESOURCE-A", "TEST-RESOURCE-B"),
    "test-free-v1": ("TEST-RESOURCE-FREE",),
    "test-support-hours-v1": ("TEST-RESOURCE-SUPPORT-HOURS",),
}

TEST_CATALOGUE_SOURCE_ADAPTER = StaticCatalogueSourceAdapter(TEST_CATALOGUE_SOURCES)

TEST_PRESENTATION_SOURCES = (
    PresentationSource(
        "TEST-TEMPLATE", "A synthetic downloadable template for storefront testing.", "test-template-cover-v1"
    ),
    PresentationSource(
        "TEST-BUNDLE", "A synthetic bundle that represents multiple downloadable resources.", "test-bundle-cover-v1"
    ),
    PresentationSource(
        "TEST-FREE", "A synthetic no-cost resource for native free Checkout testing.", "test-free-cover-v1"
    ),
    PresentationSource(
        "TEST-SUPPORT-HOURS",
        "A synthetic service product with a quantity limit of five hours.",
        "test-support-hours-cover-v1",
    ),
)


class StaticPresentationSourceAdapter:
    """Adapts immutable test presentation records without a runtime dependency."""

    async def load(self) -> tuple[PresentationSource, ...]:
        return TEST_PRESENTATION_SOURCES


TEST_PRESENTATION_SOURCE_ADAPTER: PresentationSourceAdapter = StaticPresentationSourceAdapter()
