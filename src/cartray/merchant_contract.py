"""Validated merchant presentation paths and document metadata, not commerce authority."""

import ipaddress
import json
import re
from html import escape
from urllib.parse import urlsplit

_PATH = re.compile(r"/(?:[A-Za-z0-9_-]+/)+")
_RESERVED = {"cart", "purchase-return", "review", "reviews", "unsubscribe", "checkout", "checkout-status",
             "stripe", "api", "theme", "assets", "shop", "help", "inbox", "checkout-preview", "uat-return",
             "_headers", "_redirects", "_worker"}


def merchant_path(value, *, product=False):
    """Accept explicit, canonical local paths without encoded or ambiguous segments."""
    if (not isinstance(value, str) or len(value) > 512 or not _PATH.fullmatch(value)
            or any(len(part) > 255 for part in value.strip("/").split("/"))
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


def product_route_groups(paths, subjects=None):
    """Allow a shared canonical product page only for one explicit native subject."""
    subjects = {} if subjects is None else subjects
    if not isinstance(subjects, dict):
        raise ValueError('review subject mappings must be an object')
    groups, prefixes = {}, {}
    for key, path in paths.items():
        merchant_path(path, product=True)
        register_path(path, prefixes)
        groups.setdefault(path, []).append(key)
    for keys in groups.values():
        if len(keys) > 1:
            identities = [subjects.get(key) for key in keys]
            if (not all(isinstance(value, str) and re.fullmatch(r'[a-z0-9-]{1,128}', value)
                        for value in identities) or len(set(identities)) != 1):
                raise ValueError('shared product paths require one explicit identical review subject')
    return groups


def page_seo(value):
    """Validate a small allowlist; return a safe JSON-LD string separately."""
    required = {"title", "canonical_url", "robots"}
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - {"description", "structured_data"}):
        raise ValueError("invalid page SEO fields")
    if not isinstance(value["title"], str) or not value["title"].strip() or len(value["title"]) > 200:
        raise ValueError("invalid page SEO text")
    description = value.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > 16384):
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
    host = url.hostname.removesuffix(".")
    if (not host or len(host) > 253 or any(len(label) > 63 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in host.split("."))):
        raise ValueError("invalid canonical URL host")
    if re.fullmatch(r"[0-9.]+", host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError as error:
            raise ValueError("invalid canonical URL host") from error
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


def seo_head(value, *, force_noindex=False):
    """Render the validated metadata only; absent descriptions remain absent."""
    structured = page_seo(value)
    robots = "noindex,nofollow" if force_noindex else value["robots"]
    result = f'<meta name="robots" content="{robots}">'
    result += f'<title>{escape(value["title"])}</title>'
    if value.get("description") is not None:
        result += f'<meta name="description" content="{escape(value["description"], quote=True)}">'
    result += f'<link rel="canonical" href="{escape(value["canonical_url"], quote=True)}">'
    if structured:
        result += f'<script type="application/ld+json">{structured}</script>'
    return result
