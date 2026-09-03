from __future__ import annotations

from .catalogue import StaticCatalogueSourceAdapter
from .models import CatalogSource

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
