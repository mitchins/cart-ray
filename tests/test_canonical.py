import pytest

from cartray.canonical import (
    CanonicalItem,
    build_item_chunks,
    items_digest,
    parse_projection_items,
    projection_metadata,
)
from cartray.errors import CheckoutValidationError


def test_digest_is_deterministic_over_canonical_order():
    items = (CanonicalItem("TEST-B", 2), CanonicalItem("TEST-A", 1))
    assert items_digest(items) == items_digest(tuple(reversed(items)))


def test_projection_round_trips_and_binds_its_digest():
    items = (CanonicalItem("TEST-B", 2), CanonicalItem("TEST-A", 1))
    metadata = projection_metadata(order_id="cr_123", catalogue_version="sha256:manifest", items=items, nonce="nonce")
    assert parse_projection_items(metadata) == (CanonicalItem("TEST-A", 1), CanonicalItem("TEST-B", 2))


def test_projection_rejects_missing_chunk_and_duplicate_items():
    items = tuple(CanonicalItem(f"TEST-{index:02d}", 1) for index in range(50))
    chunks = build_item_chunks(items)
    assert len(chunks) > 1
    metadata = projection_metadata(order_id="cr_123", catalogue_version="sha256:manifest", items=items, nonce="nonce")
    del metadata["cr_items_02"]
    with pytest.raises(CheckoutValidationError, match="contiguous"):
        parse_projection_items(metadata)

    duplicate = projection_metadata(
        order_id="cr_123",
        catalogue_version="sha256:manifest",
        items=(CanonicalItem("TEST-A", 1),),
        nonce="n",
    )
    duplicate["cr_items_01"] = "TEST-A:1,TEST-A:1"
    duplicate["cr_item_count"] = "2"
    duplicate["cr_items_digest"] = items_digest((CanonicalItem("TEST-A", 1),))
    with pytest.raises(CheckoutValidationError, match="duplicate"):
        parse_projection_items(duplicate)
