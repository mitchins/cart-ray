#!/usr/bin/env python3
"""Validate the approved EP UAT archive and prepare a non-authoritative browsing catalogue.

This command never reads the private handoff lane and never creates checkout prices or entitlements.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import tarfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath


class TextOnly(HTMLParser):
    """Extract source copy as inert text; scripts, styles and links are never executed."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object"}:
            self.suppressed += 1
        if tag in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "object"} and self.suppressed:
            self.suppressed -= 1
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)

    def text(self):
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def plain(value):
    parser = TextOnly()
    parser.feed(value or "")
    return html.unescape(parser.text())


def read_archive(path):
    """Verify every declared byte and reject unexpected members before reading data."""
    blobs = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if not member.isfile() or name.parts[:1] != ("ep-catalogue-uat",) or ".." in name.parts:
                raise ValueError(f"unsafe archive member: {member.name}")
            relative = str(PurePosixPath(*name.parts[1:]))
            if relative in blobs or member.size > 25_000_000:
                raise ValueError(f"duplicate or oversized archive member: {relative}")
            blobs[relative] = archive.extractfile(member).read()
    manifest = json.loads(blobs["manifest.json"])
    declared = {x["path"]: x for x in manifest["files"]}
    if len(declared) != len(manifest["files"]) or set(blobs) != set(declared) | {"manifest.json"}:
        raise ValueError("archive membership differs from manifest")
    for name, item in declared.items():
        if len(blobs[name]) != item["bytes"] or hashlib.sha256(blobs[name]).hexdigest() != item["sha256"]:
            raise ValueError(f"archive hash mismatch: {name}")
    return blobs, manifest


def prepare(blobs, archive_hash):
    """Map published records to listings and independent, disabled purchase actions."""
    offers = json.loads(blobs["offers.json"])
    identities = json.loads(blobs["legacy-keys.json"])
    commerce_config = json.loads(blobs["catalogue-config.json"])["sourceCommerceOptions"]
    media = json.loads(blobs["media-manifest.json"])["files"]
    relations = json.loads(blobs["media-relations.json"])
    image_alts = {x["key"]: x["source"].get("alt") for x in relations}
    for entry in media:
        if entry["kind"] not in {"public_image", "free_native_brochure"}:
            raise ValueError("unsupported media kind")
        raw = blobs.get(entry["path"])
        if raw is None or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"media mismatch {entry['path']}")
    if len({x["key"] for x in offers}) != len(offers):
        raise ValueError("duplicate offer identities")
    published = {x["key"] for x in identities if x["uatOffer"] and x["sourceStatus"] == "publish"}
    if {x["key"] for x in offers} != published or any(x["sourceStatus"] != "publish" for x in offers):
        raise ValueError("published lifecycle identities do not reconcile")
    media_by_ref = {ref: x for x in media if x["kind"] == "public_image" for ref in x["references"]}
    products = [x for x in offers if x["sourceType"] == "product"]
    variants = [x for x in offers if x["sourceType"] == "product_variation"]
    if len(products) + len(variants) != len(offers):
        raise ValueError("unknown published record type")
    product_keys = {x["key"] for x in products}
    if any(x["parentKey"] not in product_keys for x in variants):
        raise ValueError("variation has no published parent")
    if any(x["urlResolution"].get("canonicalUrl") or x["urlResolution"].get("permalinkUrl") for x in offers):
        raise ValueError("source canonical contract changed; review before proceeding")
    result = []
    for p in products:
        categories = [
            {"id": t["termId"], "name": t["name"], "slug": t["slug"], "parent": t["parent"]}
            for t in p["taxonomy"]
            if t["taxonomy"] == "product_cat"
        ]
        images = []
        for image_id in [p["imageId"], *p["galleryIds"]]:
            if not image_id:
                continue
            reference = "ep:woo:media:" + str(image_id)
            if reference not in media_by_ref:
                raise ValueError(f"missing public image {reference}")
            item = media_by_ref[reference]
            if item["path"] not in blobs or hashlib.sha256(blobs[item["path"]]).hexdigest() != item["sha256"]:
                raise ValueError(f"media mismatch {reference}")
            if item["path"] not in [i["path"] for i in images]:
                images.append(
                    {"path": item["path"], "sha256": item["sha256"], "alt": image_alts.get(reference) or p["title"]}
                )
        options = []
        for child in variants:
            if child["parentKey"] != p["key"]:
                continue
            if not child["variationAttributes"] or child["urlResolution"]["canonicalUrl"] is not None:
                raise ValueError(f"unresolved variation identity {child['key']}")
            options.append(
                {
                    "key": child["key"],
                    "attributes": child["variationAttributes"],
                    "source_price": child["commerce"].get("_price"),
                    "description": plain(child["commerce"].get("_variation_description", "")),
                    "checkout_authorized": False,
                }
            )
        options.sort(key=lambda x: tuple(sorted(x["attributes"].items())))
        source_type = p["productType"]
        action = {"variable": "variant_select", "external": "external", "simple": "browse_only"}.get(
            source_type, "unsupported"
        )
        free_files = [
            x for x in media if x["kind"] == "free_native_brochure" and p["key"] + ":free-native" in x["references"]
        ]
        if free_files and (p["delivery"]["kind"] != "free_native_download" or len(free_files) != 1):
            raise ValueError(f"inconsistent free download for {p['key']}")
        free_file = free_files[0]["path"] if free_files else None
        if free_file:
            action = "free_download"
        cta = p.get("externalCatalogueCta") or {}
        result.append(
            {
                "key": p["key"],
                "source_id": p["legacyWooId"],
                "title": p["title"],
                "source_sku": p["commerce"].get("_sku"),
                "source_slug": p["slug"],
                "canonical_url": None,
                "source_type": source_type,
                "source_visibility": [t["slug"] for t in p["taxonomy"] if t["taxonomy"] == "product_visibility"],
                "action": action,
                "source_delivery_kind": p["delivery"]["kind"],
                "source_workflow_keys": p["workflowKeys"],
                "free_download": free_file,
                "external_label": cta.get("label") if cta.get("activeForSourceProductType") else None,
                "external_url": cta.get("url") if cta.get("activeForSourceProductType") else None,
                "source_price": p["commerce"].get("_price"),
                "checkout_authorized": False,
                "summary": plain(p["excerptHtml"]),
                "description": plain(p["contentHtml"]),
                "categories": categories,
                "images": images,
                "variations": options,
                "default_attributes": p["commerce"].get("_default_attributes") or {},
                "menu_order": p["menuOrder"],
                "review_subject": None,
            }
        )
    result.sort(key=lambda x: (x["menu_order"], x["source_id"]))
    logo = [x for x in media if x.get("sourceRelativeFile") == "2023/06/Effective-policy-logo.jpg"]
    if len(logo) != 1 or logo[0]["kind"] != "public_image":
        raise ValueError("source brand logo unavailable")
    public_media = {entry["path"]: entry["sha256"] for entry in media}
    return {
        "schema": "cartray-merchant-browse-v1",
        "brand_logo": logo[0]["path"],
        "merchant_name": "Effective Policy",
        "source_currency": commerce_config["woocommerce_currency"],
        "source_prices_include_tax": commerce_config["woocommerce_prices_include_tax"],
        "public_media": [{"path": path, "sha256": sha} for path, sha in sorted(public_media.items())],
        "source_archive_sha256": archive_hash,
        "authority": "source-price-is-display-only; no-checkout-or-review-authority",
        "products": result,
        "reconciliation": {
            "products": len(products),
            "variations": len(variants),
            "lifecycle": len(identities),
            "inactive": sum(not x["uatOffer"] for x in identities),
            "media": len(media),
            "source_types": dict(Counter(x["productType"] for x in offers)),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = args.archive.read_bytes()
    if hashlib.sha256(raw).hexdigest() == "824cf47fa8c57af588a767d655db38f351de2f5d191518dbaa28af1ba82b5f3f":
        raise ValueError("superseded handoff archive is not permitted")
    blobs, _ = read_archive(args.archive)
    document = prepare(blobs, hashlib.sha256(raw).hexdigest())
    if args.output.exists():
        raise ValueError("output already exists")
    args.output.mkdir(parents=True)
    for item in document["public_media"]:
        target = args.output / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[item["path"]])
    (args.output / "catalogue.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(document["reconciliation"], indent=2))


if __name__ == "__main__":
    main()
