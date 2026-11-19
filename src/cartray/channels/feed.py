"""Deterministic static feed adapters. Generating a feed never publishes it."""

from __future__ import annotations

import csv
from datetime import datetime
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
    "material",
    "pattern",
    "google_product_category",
    "availability_date",
)


def _write(snapshot, dialect):
    validate(snapshot)
    if snapshot["channel"] != dialect:
        raise ValueError("adapter and channel differ")
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter="\t" if dialect == "google" else ",", lineterminator="\n")
    names = ("brand", "color", "size", "material", "pattern")
    if dialect == "google":
        names += ("google_product_category", "availability_date")
    writer.writerow(_BASE[:9] + names)
    emitted = 0
    for item in sorted(snapshot["items"], key=lambda row: row["item_id"]):
        if item["published"] is None or item["eligible"] is None:
            raise ValueError("unknown publication or eligibility")
        if item["published"] and item["eligible"]:
            if dialect == "meta" and any(
                item["attributes"].get(k) for k in ("google_product_category", "availability_date")
            ):
                raise ValueError("unsupported Meta feed attribute")
            if item["attributes"].get("variant_option"):
                raise ValueError("variant_option export is not yet supported")
            if item["price"] is not None and item["price"].startswith("0.00 "):
                raise ValueError("zero-price offers are not supported by this feed adapter")
            if item["group_id"] and not any(
                item["attributes"].get(k) for k in ("color", "size", "material", "pattern")
            ):
                raise ValueError("grouped variant requires a distinguishing attribute")
            if dialect == "meta" and item["availability"] not in ("in stock", "out of stock"):
                raise ValueError("Meta availability is not qualified by this adapter")
            if dialect == "google":
                import re

                date = item["attributes"].get("availability_date")
                if item["availability"] in ("preorder", "backorder") and not date:
                    raise ValueError("Google preorder/backorder requires an availability date")
                if date:
                    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{4})", date):
                        raise ValueError("invalid availability date")
                    try:
                        datetime.strptime(date.replace("Z", "+0000"), "%Y-%m-%dT%H:%M%z")
                    except ValueError as error:
                        raise ValueError("invalid availability date") from error
            if any(item[key] is None for key in ("landing_url", "image_url", "price", "availability")):
                raise ValueError("eligible item has missing channel data")
            emitted += 1
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
                    *(item["attributes"].get(key, "") for key in names),
                )
            )
    if not emitted:
        raise ValueError("refusing to stage an empty feed")
    return output.getvalue().encode("utf-8")


def google_feed(snapshot):
    return _write(snapshot, "google")


def meta_feed(snapshot):
    return _write(snapshot, "meta")
