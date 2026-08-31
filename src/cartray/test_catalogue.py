from __future__ import annotations

from .models import CatalogSource

TEST_CATALOGUE_SOURCES = (
    CatalogSource("TEST-TEMPLATE", "Test template", "cr_test_template", "download", "test-template-v1", 10, True),
    CatalogSource("TEST-BUNDLE", "Test bundle", "cr_test_bundle", "download", "test-bundle-v1", 10, True),
    CatalogSource("TEST-FREE", "Test free resource", "cr_test_free", "download", "test-free-v1", 10, True),
)

TEST_FULFILMENT_EXPANSIONS = {
    "test-template-v1": ("test-resource-template-v1",),
    "test-bundle-v1": ("test-resource-bundle-a-v1", "test-resource-bundle-b-v1"),
    "test-free-v1": ("test-resource-free-v1",),
}
