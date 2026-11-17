#!/usr/bin/env python3
"""Verify declared UAT media bytes and decode all declared raster images."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import PIL
from PIL import Image


def qualify(source: Path) -> dict:
    source = source.resolve(strict=True)
    data = json.loads((source / "catalogue.json").read_text())
    seen = set()
    images = pdfs = 0
    for item in data["public_media"]:
        relative = Path(item["path"])
        if (relative.is_absolute() or relative.parts[:1] != ("media",) or ".." in relative.parts
                or str(relative) in seen):
            raise ValueError("invalid or duplicate media path")
        seen.add(str(relative))
        file = source / relative
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(source):
            raise ValueError("missing or unsafe media")
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("media fingerprint mismatch")
        if relative.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            with Image.open(file) as image:
                expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
                if image.format != expected[relative.suffix.lower()]:
                    raise ValueError("media format does not match its extension")
                image.verify()
            # verify() checks structure without decoding pixel data.
            with Image.open(file) as image:
                image.load()
            images += 1
        elif relative.suffix.lower() == ".pdf" and raw.startswith(b"%PDF-"):
            pdfs += 1  # Signature only; not a full PDF rendering or content audit.
        else:
            raise ValueError("unsupported or invalid media")
    return {"source_archive_sha256": data["source_archive_sha256"], "pillow_version": PIL.__version__,
            "verified_raster_images": images, "pdf_signatures": pdfs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = qualify(args.source)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
