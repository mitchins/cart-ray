"""Validated merchant presentation paths and document metadata, not commerce authority."""

import json
import re
from urllib.parse import urlsplit

_PATH = re.compile(r"/(?:[A-Za-z0-9_-]+/)+")
_RESERVED = {"cart", "purchase-return", "review", "reviews", "unsubscribe", "checkout", "checkout-status",
             "stripe", "api", "theme", "assets", "shop", "help", "inbox", "checkout-preview", "uat-return"}


def merchant_path(value, *, product=False):
    """Accept explicit, canonical local paths without encoded or ambiguous segments."""
    if (not isinstance(value, str) or len(value) > 512 or not _PATH.fullmatch(value)
            or (product and value.split('/')[1].lower() in _RESERVED)):
        raise ValueError("invalid or reserved merchant path")
    return value


def register_path(value, prefixes):
    """Reject inconsistent casing of shared directory prefixes on any filesystem."""
    merchant_path(value)
    parts = value.strip("/").split("/")
    for index in range(1, len(parts) + 1):
        prefix = "/" + "/".join(parts[:index]) + "/"
        folded = prefix.casefold()
        if folded in prefixes and prefixes[folded] != prefix:
            raise ValueError("conflicting route directory casing")
        prefixes[folded] = prefix


def page_seo(value):
    """Validate a small allowlist; return a safe JSON-LD string separately."""
    required = {"title", "description", "canonical_url", "robots"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {"structured_data"}:
        raise ValueError("invalid page SEO fields")
    for key, limit in (("title", 200), ("description", 500)):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > limit:
            raise ValueError("invalid page SEO text")
    try:
        url = urlsplit(value["canonical_url"])
        port = url.port
    except (TypeError, ValueError) as error:
        raise ValueError("invalid canonical URL") from error
    if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
            or port not in (None, 443) or not re.fullmatch(r"[A-Za-z0-9.-]+", url.hostname)
            or any(ord(c) <= 32 for c in value["canonical_url"])):
        raise ValueError("invalid canonical URL")
    if url.path != "/":
        merchant_path(url.path)
    if value["robots"] not in ("index,follow", "noindex,nofollow"):
        raise ValueError("invalid indexing directive")
    data = value.get("structured_data")
    if data is not None:
        allowed = {"@type", "name", "description", "sku"}
        if (not isinstance(data, dict) or data.keys() - allowed or data.get("@type") != "Product"
                or not isinstance(data.get("name"), str) or not data["name"].strip()
                or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in data.values())):
            raise ValueError("invalid structured data")
        data = json.dumps({"@context": "https://schema.org", **data}, ensure_ascii=True, separators=(",", ":"))
        data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return data
