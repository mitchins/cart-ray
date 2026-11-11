from __future__ import annotations

import csv
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Protocol

from .canonical import PRODUCT_KEY_RE
from .catalogue import Catalogue
from .errors import CatalogueValidationError
from .models import PresentationSource

IMAGE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}(?:\.(?:webp|png|jpe?g|svg))?$")
_PRESENTATION_HEADERS = {"product_key", "short_description", "image_key"}
MAX_PRODUCT_IMAGES = 8
_OPTIONAL_HEADERS = {"image_alt", "images_json"}
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
class StaticPresentationSourceAdapter:
    """Adapts immutable bundled presentation records without a runtime data dependency."""

    sources: tuple[PresentationSource, ...]

    async def load(self) -> tuple[PresentationSource, ...]:
        return self.sources


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
                    "images": [
                        {"url": image_url(key), "alt": alt or product["title"]}
                        for key, alt in presentation_images(self.sources[product["product_key"]])
                    ],
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
    if (
        len(headers) != len(set(headers))
        or not _PRESENTATION_HEADERS <= set(headers)
        or set(headers) - _PRESENTATION_HEADERS - _OPTIONAL_HEADERS
    ):
        raise CatalogueValidationError("catalogue presentation CSV headers do not match the contract")
    if not rows:
        raise CatalogueValidationError("catalogue presentation CSV must contain at least one product")

    sources: list[PresentationSource] = []
    for row in rows:
        if None in row or any(
            not isinstance(row.get(header), str) or (header in _PRESENTATION_HEADERS and not row[header].strip())
            for header in headers
        ):
            raise CatalogueValidationError(f"invalid presentation row: {row!r}")
        if any(row[header] != row[header].strip() for header in headers):
            raise CatalogueValidationError(f"presentation values must not have surrounding whitespace: {row!r}")
        sources.append(
            PresentationSource(
                row["product_key"],
                row["short_description"],
                row["image_key"],
                row.get("image_alt", ""),
                row.get("images_json", "") or "[]",
            )
        )
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
        presentation_images(source)
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
    payload = {
        key: {
            name: value
            for name, value in sources[key].__dict__.items()
            if name not in {"image_alt", "images_json", "asset_hashes_json"}
        }
        for key in sorted(sources)
    }
    for key in sorted(sources):
        if sources[key].image_alt or presentation_images(sources[key])[1:]:
            payload[key]["images"] = presentation_images(sources[key])
        if sources[key].asset_hashes_json != "{}":
            payload[key]["asset_hashes"] = json.loads(sources[key].asset_hashes_json)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return PresentedCatalogue(catalogue, "sha256:" + sha256(encoded.encode("utf-8")).hexdigest(), sources)


def image_url(image_key: str) -> str:
    """Builds the only allowed public image path from a validated immutable asset key."""

    if not IMAGE_KEY_RE.fullmatch(image_key):
        raise CatalogueValidationError("invalid catalogue image key")
    filename = image_key if "." in image_key else f"{image_key}.webp"
    return f"/assets/products/{filename}"


def presentation_source_payload(source: PresentationSource) -> dict[str, str]:
    """Preserves old compiled bundle bytes for records without gallery metadata."""

    return {
        key: value
        for key, value in source.__dict__.items()
        if not (
            key == "image_alt"
            and value == ""
            or key == "images_json"
            and value == "[]"
            or key == "asset_hashes_json"
            and value == "{}"
        )
    }


def presentation_images(source: PresentationSource) -> tuple[tuple[str, str], ...]:
    """Returns primary then ordered extras; metadata never supplies paths or commerce facts."""

    if not isinstance(source.image_alt, str) or source.image_alt != source.image_alt.strip():
        raise CatalogueValidationError("invalid catalogue image alt text")
    try:
        hashes = json.loads(source.asset_hashes_json)
        if not isinstance(hashes, dict) or any(
            not isinstance(key, str)
            or not IMAGE_KEY_RE.fullmatch(key)
            or not isinstance(value, str)
            or not re.fullmatch(r"[a-f0-9]{64}", value)
            for key, value in hashes.items()
        ):
            raise ValueError("invalid asset hashes")
        extras = json.loads(source.images_json)
    except (TypeError, ValueError) as error:
        raise CatalogueValidationError("invalid catalogue images JSON") from error
    if not isinstance(extras, list) or len(extras) >= MAX_PRODUCT_IMAGES:
        raise CatalogueValidationError("catalogue images must contain at most 7 extras")
    images = [(source.image_key, source.image_alt)]
    seen = {image_url(source.image_key)}
    for extra in extras:
        if (
            not isinstance(extra, dict)
            or set(extra) != {"image_key", "alt"}
            or not isinstance(extra["image_key"], str)
            or not IMAGE_KEY_RE.fullmatch(extra["image_key"])
            or not isinstance(extra["alt"], str)
            or not extra["alt"]
            or extra["alt"] != extra["alt"].strip()
        ):
            raise CatalogueValidationError("invalid catalogue gallery image")
        if image_url(extra["image_key"]) in seen:
            raise CatalogueValidationError("duplicate catalogue gallery image")
        seen.add(image_url(extra["image_key"]))
        images.append((extra["image_key"], extra["alt"]))
    return tuple(images)


def validate_presentation_assets(sources: tuple[PresentationSource, ...], directory: Path) -> None:
    """Ensures every primary and gallery key resolves to one allowed local static asset."""

    root = directory.resolve()
    for source in validate_presentation_sources(sources):
        for key, _alt in presentation_images(source):
            path = directory / image_url(key).rsplit("/", 1)[1]
            if not path.resolve().is_relative_to(root):
                raise CatalogueValidationError(f"catalogue image asset escapes asset directory: {key}")
            try:
                with path.open("rb") as asset:
                    header = asset.read(4096)
            except OSError as error:
                raise CatalogueValidationError(f"catalogue image asset is unavailable: {key}") from error
            extension = path.suffix
            valid = {
                ".webp": len(header) >= 12 and header[:4] == _WEBP_MAGIC and header[8:12] == _WEBP_FORMAT,
                ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
                ".jpg": header.startswith(b"\xff\xd8\xff"),
                ".jpeg": header.startswith(b"\xff\xd8\xff"),
                ".svg": b"<svg" in header and b"<!DOCTYPE" not in header.upper(),
            }[extension]
            if not valid:
                label = "WebP" if extension == ".webp" else extension[1:]
                raise CatalogueValidationError(f"catalogue image asset is not {label}: {key}")


def fingerprint_presentation_assets(
    sources: tuple[PresentationSource, ...], directory: Path
) -> tuple[PresentationSource, ...]:
    """Captures reviewed asset bytes in compiled presentation metadata, never checkout facts."""

    validate_presentation_assets(sources, directory)
    return tuple(
        replace(
            source,
            asset_hashes_json=json.dumps(
                {
                    key: sha256((directory / image_url(key).rsplit("/", 1)[1]).read_bytes()).hexdigest()
                    for key, _alt in presentation_images(source)
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for source in sources
    )
