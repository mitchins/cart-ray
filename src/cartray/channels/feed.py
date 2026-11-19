"""Deterministic static feed adapters. Generating a feed never publishes it."""

from __future__ import annotations

import csv
from io import StringIO

from .projection import validate

_BASE = (
    "id",
    "title",
    "description",
    "link",
    "image_link",
    "price",
    "availability",
    "condition",
    "item_group_id",
    "brand",
    "color",
    "size",
    "google_product_category",
)


def _write(snapshot, dialect):
    validate(snapshot)
    if snapshot["channel"] != dialect:
        raise ValueError("adapter and channel differ")
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter="\t" if dialect == "google" else ",", lineterminator="\n")
    writer.writerow(_BASE)
    for item in sorted(snapshot["items"], key=lambda row: row["item_id"]):
        if item["published"] is None or item["eligible"] is None:
            raise ValueError("unknown publication or eligibility")
        if item["published"] and item["eligible"]:
            if any(item[key] is None for key in ("landing_url", "image_url", "price", "availability")):
                raise ValueError("eligible item has missing channel data")
            writer.writerow(
                (
                    item["item_id"],
                    item["title"],
                    item["description"],
                    item["landing_url"],
                    item["image_url"],
                    item["price"],
                    item["availability"],
                    item["condition"],
                    item["group_id"] or "",
                    *(item["attributes"].get(key, "") for key in ("brand", "color", "size", "google_product_category")),
                )
            )
    return output.getvalue().encode("utf-8")


def google_feed(snapshot):
    return _write(snapshot, "google")


def meta_feed(snapshot):
    return _write(snapshot, "meta")
