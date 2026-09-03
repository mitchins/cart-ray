from __future__ import annotations

import csv
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Protocol

from .canonical import PRODUCT_KEY_RE
from .catalogue import Catalogue
from .errors import CatalogueValidationError
from .models import PresentationSource

IMAGE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_PRESENTATION_HEADERS = {"product_key", "short_description", "image_key"}
_WEBP_MAGIC = b"RIFF"
_WEBP_FORMAT = b"WEBP"


class PresentationSourceAdapter(Protocol):
    """Loads normalized public presentation records from one deterministic source."""

    async def load(self) -> tuple[PresentationSource, ...]: ...


@dataclass(frozen=True)
class CsvPresentationSourceAdapter:
    """Loads the CartRay presentation-sidecar CSV format from a local file."""

    path: Path

    async def load(self) -> tuple[PresentationSource, ...]:
        try:
            contents = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise CatalogueValidationError("catalogue presentation CSV is unavailable") from error
        return parse_presentation_csv(contents)


@dataclass(frozen=True)
class PresentedCatalogue:
    """Combines commerce facts with public presentation without changing checkout authority."""

    catalogue: Catalogue
    presentation_version: str
    sources: Mapping[str, PresentationSource]

    def public_manifest(self) -> dict[str, object]:
        commerce_manifest = self.catalogue.public_manifest()
        return {
            "version": commerce_manifest["version"],
            "presentation_version": self.presentation_version,
            "products": [
                {
                    **product,
                    "short_description": self.sources[product["product_key"]].short_description,
                    "image_url": image_url(self.sources[product["product_key"]].image_key),
                }
                for product in commerce_manifest["products"]
            ],
        }


def parse_presentation_csv(contents: str) -> tuple[PresentationSource, ...]:
    """Parses the narrow public presentation interchange without normalizing ambiguous input."""

    if contents.startswith("\ufeff"):
        raise CatalogueValidationError("catalogue presentation CSV must not contain a UTF-8 BOM")
    with StringIO(contents, newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        rows = list(reader)
    if len(headers) != len(_PRESENTATION_HEADERS) or set(headers) != _PRESENTATION_HEADERS:
        raise CatalogueValidationError("catalogue presentation CSV headers do not match the contract")
    if not rows:
        raise CatalogueValidationError("catalogue presentation CSV must contain at least one product")

    sources: list[PresentationSource] = []
    for row in rows:
        if None in row or any(not isinstance(row.get(header), str) or not row[header].strip() for header in headers):
            raise CatalogueValidationError(f"invalid presentation row: {row!r}")
        if any(row[header] != row[header].strip() for header in headers):
            raise CatalogueValidationError(f"presentation values must not have surrounding whitespace: {row!r}")
        sources.append(PresentationSource(row["product_key"], row["short_description"], row["image_key"]))
    return validate_presentation_sources(tuple(sources))


def validate_presentation_sources(sources: tuple[PresentationSource, ...]) -> tuple[PresentationSource, ...]:
    """Enforces the normalized sidecar record contract for every presentation adapter."""

    if not sources:
        raise CatalogueValidationError("catalogue presentation must contain at least one product")
    keys: set[str] = set()
    for source in sources:
        values = (
            (source.product_key, source.short_description, source.image_key)
            if isinstance(source, PresentationSource)
            else ()
        )
        if (
            not isinstance(source, PresentationSource)
            or not all(isinstance(value, str) and value and value == value.strip() for value in values)
            or not PRODUCT_KEY_RE.fullmatch(source.product_key)
            or not IMAGE_KEY_RE.fullmatch(source.image_key)
        ):
            raise CatalogueValidationError("invalid catalogue presentation record")
        if source.product_key in keys:
            raise CatalogueValidationError("duplicate catalogue presentation product keys")
        keys.add(source.product_key)
    return sources


def build_presented_catalogue(
    catalogue: Catalogue, presentation_sources: tuple[PresentationSource, ...]
) -> PresentedCatalogue:
    """Joins a complete sidecar to a commerce catalogue and derives its independent public version."""

    presentation_sources = validate_presentation_sources(presentation_sources)
    sources = {source.product_key: source for source in presentation_sources}
    commerce_keys = set(catalogue.products)
    if set(sources) != commerce_keys:
        raise CatalogueValidationError("catalogue presentation keys must exactly match catalogue product keys")
    payload = {key: sources[key].__dict__ for key in sorted(sources)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return PresentedCatalogue(catalogue, "sha256:" + sha256(encoded.encode("utf-8")).hexdigest(), sources)


def image_url(image_key: str) -> str:
    """Builds the only allowed public image path from a validated immutable asset key."""

    if not IMAGE_KEY_RE.fullmatch(image_key):
        raise CatalogueValidationError("invalid catalogue image key")
    return f"/assets/products/{image_key}.webp"


def validate_presentation_assets(sources: tuple[PresentationSource, ...], directory: Path) -> None:
    """Ensures every sidecar image key resolves to one local static WebP asset."""

    for source in validate_presentation_sources(sources):
        path = directory / f"{source.image_key}.webp"
        try:
            with path.open("rb") as asset:
                header = asset.read(12)
        except OSError as error:
            raise CatalogueValidationError(f"catalogue image asset is unavailable: {source.image_key}") from error
        if len(header) < 12 or header[:4] != _WEBP_MAGIC or header[8:12] != _WEBP_FORMAT:
            raise CatalogueValidationError(f"catalogue image asset is not WebP: {source.image_key}")
