"""Trusted fulfilment resources and provider-neutral access results.

The adapter registry only resolves a resource *after* an entitlement has been
validated. It never accepts a locator from a customer's request.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MIME = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")


@dataclass(frozen=True)
class Resource:
    merchant: str
    resource_id: str
    version: str
    provider: str
    config_ref: str
    config_digest: str
    filename: str
    mime_type: str

    def __post_init__(self):
        if not all(
            isinstance(x, str) and _KEY.fullmatch(x)
            for x in (self.merchant, self.resource_id, self.version, self.provider)
        ):
            raise ValueError("invalid resource identity")
        if (
            not isinstance(self.config_ref, str)
            or len(self.config_ref) > 256
            or any(not _KEY.fullmatch(part) for part in self.config_ref.split("/"))
        ):
            raise ValueError("invalid resource config reference")
        if not isinstance(self.config_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.config_digest):
            raise ValueError("invalid resource configuration digest")
        if (
            not isinstance(self.filename, str)
            or not 1 <= len(self.filename) <= 180
            or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in self.filename)
            or self.filename in (".", "..")
        ):
            raise ValueError("invalid resource filename")
        if not isinstance(self.mime_type, str) or not _MIME.fullmatch(self.mime_type):
            raise ValueError("invalid resource MIME type")


@dataclass(frozen=True)
class Redirect:
    url: str


@dataclass(frozen=True)
class Stream:
    body: Any
    filename: str
    mime_type: str


@dataclass(frozen=True)
class Unavailable:
    reason: str = "resource unavailable"


AccessResult = Redirect | Stream | Unavailable
Adapter = Callable[[Resource, dict[str, Any]], Awaitable[AccessResult]]


class ProviderRegistry:
    """Static, trusted adapter registry; entitlement checks precede resolution."""

    def __init__(self, adapters: dict[str, Adapter]):
        if not adapters or any(not _KEY.fullmatch(key) or not callable(value) for key, value in adapters.items()):
            raise ValueError("invalid provider registry")
        self._adapters = dict(adapters)

    async def resolve(self, resource: Resource, config: dict[str, Any]) -> AccessResult:
        adapter = self._adapters.get(resource.provider)
        if adapter is None:
            return Unavailable()
        try:
            result = await adapter(resource, config)
        except Exception:
            return Unavailable()
        if not isinstance(result, (Redirect, Stream, Unavailable)):
            return Unavailable()
        return result


async def external_link(resource: Resource, config: dict[str, Any]) -> AccessResult:
    """Return an operator-approved HTTPS target without normalizing its bytes."""
    url, allowed = config.get("url"), config.get("allowed_hosts")
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        or not isinstance(allowed, list)
        or not allowed
    ):
        return Unavailable()
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.hostname.lower() not in allowed
        ):
            return Unavailable()
    except ValueError:
        return Unavailable()
    return Redirect(url)


async def private_object(resource: Resource, config: dict[str, Any]) -> AccessResult:
    """Stream a private object through an injected storage binding, not a public URL."""
    bucket, key = config.get("bucket"), config.get("key")
    if (
        not isinstance(key, str)
        or not key
        or len(key) > 1024
        or any(ord(c) < 32 for c in key)
        or bucket is None
        or not callable(getattr(bucket, "get", None))
    ):
        return Unavailable()
    obj = await bucket.get(key)
    if obj is None or getattr(obj, "body", None) is None:
        return Unavailable()
    return Stream(obj.body, resource.filename, resource.mime_type)


def token_digest(token: str) -> str:
    """Validate and hash a capability before it is ever used for a lookup."""
    import hashlib

    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError("invalid capability")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def authorised_access(database, token, registry: ProviderRegistry, configs, now: int) -> AccessResult:
    """Resolve a non-consuming capability only for an unrevoked confirmed purchase."""
    from cartray.store import _results

    try:
        digest = token_digest(token)
    except ValueError:
        return Unavailable()
    result = (
        await database.prepare(
            """SELECT b.merchant_id,b.resource_id,b.resource_version,r.provider,r.config_ref,
                  r.config_digest,r.filename,r.mime_type
        FROM fulfilment_capabilities c
        JOIN fulfilment_entitlements e USING (merchant_id,environment,order_id,workflow_key,product_key,ordinal)
        JOIN fulfilment_order_bindings b USING (merchant_id,environment,order_id,workflow_key,product_key,ordinal)
        JOIN fulfilment_resources r USING (merchant_id,environment,resource_id,resource_version)
        JOIN checkout_sessions s ON s.order_id=c.order_id
        WHERE c.token_digest=? AND c.revoked_at IS NULL AND e.revoked_at IS NULL
          AND (e.expires_at IS NULL OR e.expires_at > ?) AND s.settlement_state='confirmed'
          AND EXISTS(SELECT 1 FROM outbox o WHERE o.order_id=c.order_id AND o.event_type='OrderConfirmed')"""
        )
        .bind(digest, now)
        .all()
    )
    rows = _results(result)
    if len(rows) != 1:
        return Unavailable()
    row = rows[0]
    try:
        resource = Resource(
            row["merchant_id"],
            row["resource_id"],
            row["resource_version"],
            row["provider"],
            row["config_ref"],
            row["config_digest"],
            row["filename"],
            row["mime_type"],
        )
        config, digest = configs[(resource.merchant, resource.config_ref)]
        if digest != resource.config_digest:
            return Unavailable()
    except (ValueError, KeyError, TypeError):
        return Unavailable()
    return await registry.resolve(resource, config)
