from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256

from .errors import CheckoutValidationError

PRODUCT_KEY_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")
MAX_METADATA_CHUNK_BYTES = 400
MAX_METADATA_CHUNKS = 32


@dataclass(frozen=True, order=True)
class CanonicalItem:
    product_key: str
    quantity: int

    def __post_init__(self) -> None:
        if not PRODUCT_KEY_RE.fullmatch(self.product_key):
            raise CheckoutValidationError(f"invalid product key: {self.product_key!r}")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or self.quantity < 1:
            raise CheckoutValidationError("quantity must be a positive base-10 integer")


def canonical_items(items: Iterable[CanonicalItem]) -> tuple[CanonicalItem, ...]:
    ordered = tuple(sorted(items, key=lambda item: item.product_key))
    if not ordered:
        raise CheckoutValidationError("a checkout requires at least one item")
    if any(left.product_key == right.product_key for left, right in zip(ordered, ordered[1:])):
        raise CheckoutValidationError("duplicate product keys are forbidden")
    return ordered


def items_digest(items: Iterable[CanonicalItem]) -> str:
    ordered = canonical_items(items)
    payload = "cartray-items-v1\n" + "".join(f"{item.product_key}:{item.quantity}\n" for item in ordered)
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


def request_fingerprint(manifest_version: str, items: Iterable[CanonicalItem]) -> str:
    payload = {
        "schema": 1,
        "manifest_version": manifest_version,
        "items": [{"product_key": item.product_key, "quantity": item.quantity} for item in canonical_items(items)],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + sha256(encoded.encode("utf-8")).hexdigest()


def build_item_chunks(items: Iterable[CanonicalItem]) -> tuple[str, ...]:
    tokens = [f"{item.product_key}:{item.quantity}" for item in canonical_items(items)]
    chunks: list[str] = []
    current = ""
    for token in tokens:
        candidate = token if not current else f"{current},{token}"
        if len(candidate.encode("utf-8")) > MAX_METADATA_CHUNK_BYTES:
            if not current:
                raise CheckoutValidationError("a single item exceeds the metadata chunk limit")
            chunks.append(current)
            current = token
        else:
            current = candidate
    if current:
        chunks.append(current)
    if len(chunks) > MAX_METADATA_CHUNKS:
        raise CheckoutValidationError("item projection exceeds metadata capacity")
    return tuple(chunks)


def projection_metadata(
    *,
    order_id: str,
    catalogue_version: str,
    items: Iterable[CanonicalItem],
    nonce: str,
) -> dict[str, str]:
    ordered = canonical_items(items)
    chunks = build_item_chunks(ordered)
    metadata = {
        "cr_schema": "1",
        "cr_source": "cartray",
        "cr_order_id": order_id,
        "cr_catalogue_version": catalogue_version,
        "cr_item_count": str(len(ordered)),
        "cr_chunk_count": str(len(chunks)),
        "cr_items_digest": items_digest(ordered),
        "cr_nonce": nonce,
    }
    metadata.update({f"cr_items_{index:02d}": chunk for index, chunk in enumerate(chunks, start=1)})
    return metadata


def parse_projection_items(metadata: Mapping[str, str]) -> tuple[CanonicalItem, ...]:
    try:
        chunk_count = int(metadata["cr_chunk_count"])
        item_count = int(metadata["cr_item_count"])
    except (KeyError, TypeError, ValueError) as error:
        raise CheckoutValidationError("projection is missing a valid count") from error
    if not 1 <= chunk_count <= MAX_METADATA_CHUNKS or item_count < 1:
        raise CheckoutValidationError("projection count is outside its permitted range")

    expected_keys = {f"cr_items_{index:02d}" for index in range(1, chunk_count + 1)}
    actual_keys = {key for key in metadata if key.startswith("cr_items_") and key != "cr_items_digest"}
    if actual_keys != expected_keys:
        raise CheckoutValidationError("projection chunks must be contiguous with no extras")

    parsed: list[CanonicalItem] = []
    for key in sorted(expected_keys):
        value = metadata[key]
        if not value or len(value.encode("utf-8")) > MAX_METADATA_CHUNK_BYTES:
            raise CheckoutValidationError("invalid projection chunk")
        for token in value.split(","):
            match = re.fullmatch(r"([A-Z0-9][A-Z0-9_-]{0,63}):([1-9][0-9]*)", token)
            if not match:
                raise CheckoutValidationError("invalid projection item")
            parsed.append(CanonicalItem(match.group(1), int(match.group(2))))

    ordered = canonical_items(parsed)
    if len(ordered) != item_count:
        raise CheckoutValidationError("projection item count does not match its items")
    if metadata.get("cr_items_digest") != items_digest(ordered):
        raise CheckoutValidationError("projection item digest does not match its items")
    return ordered
