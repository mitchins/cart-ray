"""Validate explicit source-link replacements for the offline UAT build."""

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def _public_url(value, *, allow_http=False):
    scheme = r"https?" if allow_http else r"https"
    if (not isinstance(value, str) or len(value) > 2048 or not re.fullmatch(
            scheme + r"://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~:/-]*)?(?:\?[A-Za-z0-9._~=&-]+)?(?:#[A-Za-z0-9_-]+)?", value)):
        return False
    parsed = urlsplit(value)
    labels = (parsed.hostname or "").split(".")
    if len(labels) < 2 or labels[-1].lower() in {"local", "internal", "localhost"}:
        return False
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            return False
    except ValueError:
        pass
    return (all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?", x) for x in labels)
            and "//" not in parsed.path and not any(x in (".", "..") for x in parsed.path.split("/")))


def _safe_target(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    if re.fullmatch(r"mailto:[A-Za-z0-9.!#$&'*+^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value):
        return True
    if re.fullmatch(r"/(?:[A-Za-z0-9_.~-]+/)*[A-Za-z0-9_.~-]*(?:#[A-Za-z0-9_-]+)?", value):
        path = urlsplit(value).path
        if any(part in (".", "..") for part in path.split("/")):
            return False
    elif _public_url(value):
        parsed = urlsplit(value)
        path = parsed.path
        if any(label.lower() in {"checkout", "payment", "payments", "pay"}
               for label in parsed.hostname.split(".")):
            return False
        if re.search(r"(?:^|[?&])(?:add-to-cart|buy|checkout|payment|order)=", parsed.query, re.I):
            return False
    else:
        return False
    # The operator must additionally review destinations for transactional
    # behaviour; syntax alone cannot establish what a remote page does.
    return not any(part.lower() in {"checkout", "cart", "purchase", "payment", "pay", "buy", "order", "orders",
                                    "subscribe", "unsubscribe"}
                   for part in path.split("/"))


def validate_link_policy(value):
    if value is None:
        return {}
    if (not isinstance(value, dict) or set(value) != {"version", "links"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("invalid source link policy")
    links = value["links"]
    if not isinstance(links, dict) or len(links) > 1000:
        raise ValueError("invalid source link policy mappings")
    for source, target in links.items():
        if not (_public_url(source, allow_http=True) or _safe_target(source)):
            raise ValueError("invalid source link URL")
        if not _safe_target(target):
            raise ValueError("invalid approved link destination")
    return dict(links)


def read_link_policy(path):
    if path is None:
        return {}
    def unique_keys(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate source link policy key")
        return result

    return validate_link_policy(json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys))
