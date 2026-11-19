"""Validate and compare explicit channel snapshots without activating a feed."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,149}\Z")
_PRICE = re.compile(r"(?:0|[1-9][0-9]{0,8})\.[0-9]{2} [A-Z]{3}\Z")
_FIELDS = (
    "item_id",
    "merchant_product_id",
    "title",
    "description",
    "landing_url",
    "image_url",
    "price",
    "availability",
    "group_id",
    "published",
    "eligible",
    "canonical_url",
    "condition",
    "attributes",
)


def _text(value, name, limit=1024):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"invalid {name}")
    return value


def _url(value, name):
    _text(value, name, 2000)
    if any(c.isspace() for c in value) or "\\" in value:
        raise ValueError(f"invalid {name}")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError(f"invalid {name}")
    except ValueError as error:
        raise ValueError(f"invalid {name}") from error
    return value


def canonical_digest(snapshot):
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def validate(snapshot):
    """Fail closed on incomplete identity and eligibility; never infer purchase authority."""
    required = {
        "schema",
        "merchant",
        "channel",
        "account",
        "catalogue",
        "source",
        "captured_at",
        "coverage",
        "items",
        "content_language",
        "feed_label",
    }
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != required
        or (type(snapshot["schema"]) is not int or snapshot["schema"] != 1)
    ):
        raise ValueError("invalid snapshot envelope")
    for key in ("merchant", "account", "catalogue", "source"):
        if not isinstance(snapshot[key], str) or not _ID.fullmatch(snapshot[key]):
            raise ValueError(f"invalid {key}")
    if snapshot["channel"] not in ("google", "meta"):
        raise ValueError("unsupported channel")
    for key in ("content_language", "feed_label"):
        if not isinstance(snapshot[key], str) or not _ID.fullmatch(snapshot[key]):
            raise ValueError(f"invalid {key}")
    _text(snapshot["captured_at"], "capture time", 64)
    if snapshot["coverage"] != "complete":
        raise ValueError("snapshot coverage must be complete")
    if not isinstance(snapshot["items"], list) or len(snapshot["items"]) > 100000:
        raise ValueError("invalid items")
    keys = set()
    item_fields = {
        "item_id",
        "merchant_product_id",
        "landing_url",
        "canonical_url",
        "image_url",
        "title",
        "description",
        "price",
        "availability",
        "group_id",
        "published",
        "eligible",
        "condition",
        "attributes",
    }
    for item in snapshot["items"]:
        if not isinstance(item, dict) or set(item) != item_fields:
            raise ValueError("invalid item fields")
        for key in ("item_id", "merchant_product_id"):
            _text(item[key], key, 150)
            if item[key] != item[key].strip() or item[key].startswith(("=", "+", "-", "@")):
                raise ValueError(f"invalid {key}")
        if snapshot["channel"] == "google" and (
            len(item["item_id"]) > 50 or (isinstance(item["group_id"], str) and len(item["group_id"]) > 50)
        ):
            raise ValueError("Google item and group IDs must not exceed 50 characters")
        if item["item_id"] in keys:
            raise ValueError("duplicate external item identity")
        keys.add(item["item_id"])
        for key in ("landing_url", "canonical_url", "image_url"):
            if item[key] is not None:
                _url(item[key], key)
        for key, limit in (("title", 150), ("description", 5000)):
            _text(item[key], key, limit)
            if item[key].lstrip().startswith(("=", "+", "-", "@")):
                raise ValueError(f"unsafe spreadsheet formula in {key}")
        attributes = item["attributes"]
        allowed = {
            "brand",
            "color",
            "size",
            "material",
            "pattern",
            "variant_option",
            "google_product_category",
            "availability_date",
        }
        if not isinstance(attributes, dict) or set(attributes) - allowed:
            raise ValueError("invalid channel attributes")
        for name, value in attributes.items():
            _text(value, name, 250)
            if value.lstrip().startswith(("=", "+", "-", "@")):
                raise ValueError("unsafe spreadsheet formula in attribute")
        if item["price"] is not None and (not isinstance(item["price"], str) or not _PRICE.fullmatch(item["price"])):
            raise ValueError("invalid price")
        if item["availability"] not in (None, "in stock", "out of stock", "preorder", "backorder"):
            raise ValueError("invalid availability")
        if item["condition"] not in ("new", "used", "refurbished"):
            raise ValueError("invalid condition")
        if item["group_id"] is not None and (
            not isinstance(item["group_id"], str)
            or not item["group_id"]
            or len(item["group_id"]) > 150
            or item["group_id"] != item["group_id"].strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in item["group_id"])
            or item["group_id"].startswith(("=", "+", "-", "@"))
        ):
            raise ValueError("invalid variant group")
        if any(value is not None and type(value) is not bool for value in (item["published"], item["eligible"])):
            raise ValueError("publication and channel eligibility must be explicit")
    return snapshot


def compare(source, candidate, exceptions=()):
    """Return all differences; waivers must bind both snapshot digests and a reviewer."""
    validate(source)
    validate(candidate)
    if any(
        source[k] != candidate[k]
        for k in ("merchant", "channel", "account", "catalogue", "content_language", "feed_label")
    ):
        raise ValueError("snapshot scope mismatch")
    sd, cd = canonical_digest(source), canonical_digest(candidate)
    approved = set()
    for entry in exceptions:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"source_digest", "candidate_digest", "item_id", "field", "reviewer", "reason"}
            or entry["source_digest"] != sd
            or entry["candidate_digest"] != cd
        ):
            raise ValueError("invalid snapshot-bound exception")
        _text(entry["item_id"], "exception item", 150)
        _text(entry["field"], "exception field", 150)
        if (entry["item_id"], entry["field"]) in approved:
            raise ValueError("duplicate exception")
        _text(entry["reviewer"], "exception reviewer", 150)
        _text(entry["reason"], "exception reason", 500)
        approved.add((entry["item_id"], entry["field"]))
    old = {v["item_id"]: v for v in source["items"]}
    new = {v["item_id"]: v for v in candidate["items"]}
    differences = []
    for key in sorted(old.keys() | new.keys()):
        if key not in old or key not in new:
            field = "added" if key not in old else "removed"
            differences.append(
                {
                    "item_id": key,
                    "field": field,
                    "old": old.get(key),
                    "new": new.get(key),
                    "approved": (key, field) in approved,
                }
            )
        else:
            for field in _FIELDS:
                missing = field in (
                    "landing_url",
                    "canonical_url",
                    "image_url",
                    "price",
                    "availability",
                    "published",
                    "eligible",
                ) and (old[key][field] is None or new[key][field] is None)
                both_excluded = (old[key]["published"] is False or old[key]["eligible"] is False) and (
                    new[key]["published"] is False or new[key]["eligible"] is False
                )
                if both_excluded and old[key][field] is None and new[key][field] is None:
                    missing = False
                if old[key][field] != new[key][field] or missing:
                    differences.append(
                        {
                            "item_id": key,
                            "field": field,
                            "unverified": missing,
                            "old": old[key][field],
                            "new": new[key][field],
                            "approved": (key, field) in approved,
                        }
                    )
    used = {(row["item_id"], row["field"]) for row in differences}
    if approved - used:
        raise ValueError("exception does not match a difference")
    from .feed import google_feed, meta_feed

    try:
        (google_feed if candidate["channel"] == "google" else meta_feed)(candidate)
        local_error = None
    except ValueError as error:
        local_error = str(error)
    return {
        "candidate_locally_exportable": local_error is None,
        "candidate_export_error": local_error,
        "source_digest": sd,
        "candidate_digest": cd,
        "differences": differences,
        "ready": local_error is None
        and all(row["approved"] and not row.get("unverified", False) for row in differences),
    }
