"""Accountless purchased-resource access, independent of email delivery.

The caller issuing a capability must be a trusted order processor. Resource
locators come only from merchant configuration, never from the customer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from urllib.parse import urlsplit

from .store import _results

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


_RESOURCE_CONFIG_LIMIT = 65536
_RESOURCE_PART_SIZE = 4096
_RESOURCE_PARTS_MAX = 22
_RESOURCE_CONFIG = 'CARTRAY_RESOURCE_CONFIG'


def resource_config_bindings(raw):
    """Pack the existing private JSON into bounded Cloudflare secret bindings."""
    if not isinstance(raw, str) or not raw or len(raw.encode()) > _RESOURCE_CONFIG_LIMIT:
        raise ValueError('private resource configuration exceeds its supported limit')
    data = raw.encode()
    if len(data) <= _RESOURCE_PART_SIZE:
        return {_RESOURCE_CONFIG: raw}
    encoded = base64.b64encode(data).decode('ascii')
    parts = [encoded[offset:offset + _RESOURCE_PART_SIZE] for offset in range(0, len(encoded), _RESOURCE_PART_SIZE)]
    return {_RESOURCE_CONFIG + '_META': json.dumps({'parts': len(parts), 'sha256': hashlib.sha256(data).hexdigest()}),
            **{_RESOURCE_CONFIG + '_' + str(index): part for index, part in enumerate(parts)}}


def _resource_config_parts(env, count):
    parts = []
    for index in range(_RESOURCE_PARTS_MAX + 1):
        value = getattr(env, _RESOURCE_CONFIG + '_' + str(index), None)
        if index >= count:
            if value is not None:
                raise ValueError('unexpected private resource configuration part')
        elif not isinstance(value, str) or not value or len(value.encode()) > _RESOURCE_PART_SIZE:
            raise ValueError('missing or oversized private resource configuration part')
        else:
            parts.append(value)
    return parts


def resource_config_from_environment(env):
    """Decode one complete private binding set; never combine partial revisions."""
    direct = getattr(env, _RESOURCE_CONFIG, None)
    metadata = getattr(env, _RESOURCE_CONFIG + '_META', None)
    if direct is not None:
        if metadata is not None or any(getattr(env, _RESOURCE_CONFIG + '_' + str(i), None) is not None
                                       for i in range(_RESOURCE_PARTS_MAX + 1)):
            raise ValueError('ambiguous private resource configuration')
        if not isinstance(direct, str) or not direct or len(direct.encode()) > _RESOURCE_CONFIG_LIMIT:
            raise ValueError('invalid private resource configuration')
        return direct
    if not isinstance(metadata, str) or len(metadata.encode()) > _RESOURCE_PART_SIZE:
        raise ValueError('missing private resource configuration')
    manifest = json.loads(metadata)
    if (not isinstance(manifest, dict) or set(manifest) != {'parts', 'sha256'}
            or type(manifest['parts']) is not int or not 1 <= manifest['parts'] <= _RESOURCE_PARTS_MAX
            or not isinstance(manifest['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', manifest['sha256'])):
        raise ValueError('invalid private resource configuration manifest')
    parts = _resource_config_parts(env, manifest['parts'])
    data = base64.b64decode(''.join(parts), validate=True)
    if len(data) > _RESOURCE_CONFIG_LIMIT or hashlib.sha256(data).hexdigest() != manifest['sha256']:
        raise ValueError('private resource configuration integrity mismatch')
    return data.decode('utf-8')


def _capability(secret: str, merchant: str, environment: str, order_id: str) -> str:
    if not isinstance(secret, str) or len(secret.encode()) < 32:
        raise ValueError("order access key is unavailable")
    if any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
           for value in (merchant, environment, order_id)):
        raise ValueError("invalid order scope")
    material = json.dumps(["order-access-v1", merchant, environment, order_id], separators=(",", ":"))
    value = hmac.new(secret.encode(), material.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def issuer_token(secret: str) -> str:
    """Derive a scoped internal caller credential without another stored secret."""
    if not isinstance(secret, str) or len(secret.encode()) < 32:
        raise ValueError("order access key is unavailable")
    return hmac.new(secret.encode(), b"cartray-order-access-issuer-v1", hashlib.sha256).hexdigest()


def _digest(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("invalid order capability")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def issue_order_access(db, secret: str, merchant: str, environment: str, order_id: str) -> str:
    """Idempotently issue only for a confirmed purchase; never reactivate revocation."""
    token = _capability(secret, merchant, environment, order_id)
    digest = _digest(token)
    await db.prepare("""INSERT OR IGNORE INTO order_access(merchant,environment,order_id,token_digest)
        SELECT ?,?,?,? WHERE EXISTS (
          SELECT 1 FROM checkout_sessions s JOIN outbox o ON o.order_id=s.order_id
          WHERE s.order_id=? AND s.settlement_state='confirmed' AND o.event_type='OrderConfirmed')
        AND EXISTS (SELECT 1 FROM post_purchase_installation WHERE singleton=1 AND merchant_id=?
                    AND environment=?)""") \
        .bind(merchant, environment, order_id, digest, order_id, merchant, environment).run()
    result = await db.prepare("""SELECT order_id FROM order_access WHERE merchant=? AND environment=?
        AND order_id=? AND token_digest=? AND revoked_at IS NULL""").bind(merchant, environment, order_id, digest).all()
    if len(_results(result)) != 1:
        raise ValueError("order access unavailable")
    return token


async def purchased_resources(db, token: str, merchant: str, environment: str) -> list[dict]:
    """Return only resources bound to this confirmed order, without private locators."""
    digest = _digest(token)
    result = await db.prepare("""SELECT i.product_key,i.quantity,i.fulfilment_resources_json
        FROM order_access a JOIN checkout_sessions s ON s.order_id=a.order_id
        JOIN outbox o ON o.order_id=a.order_id AND o.event_type='OrderConfirmed'
        JOIN order_items i ON i.order_id=a.order_id
        WHERE a.token_digest=? AND a.merchant=? AND a.environment=?
          AND a.revoked_at IS NULL AND s.settlement_state='confirmed'
        ORDER BY i.product_key""").bind(digest, merchant, environment).all()
    rows = _results(result)
    if not rows:
        raise ValueError("order access unavailable")
    items = []
    for row in rows:
        resources = json.loads(row["fulfilment_resources_json"])
        if not isinstance(resources, list) or any(
            not isinstance(resource, str) or not resource or resource != resource.strip() for resource in resources
        ):
            raise ValueError("invalid purchased resources")
        items.append({"product_key": row["product_key"], "quantity": row["quantity"], "resources": resources})
    return items


def external_url(config: dict) -> str:
    """Only return merchant-configured HTTPS URLs; reject credentials and unsafe bytes."""
    value = config.get("url") if isinstance(config, dict) else None
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("resource unavailable")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("resource unavailable")
    return value


def validate_resource_configuration(resources, bucket_names):
    """Validate merchant resource mappings without reading or revealing objects."""
    if not isinstance(resources, dict):
        raise ValueError("fulfilment resources must be an object")
    for resource_id, config in resources.items():
        if (not isinstance(resource_id, str) or not resource_id or resource_id != resource_id.strip()
                or not isinstance(config, dict)):
            raise ValueError("invalid fulfilment resource")
        provider = config.get("provider")
        if provider == "external":
            external_url(config)
        elif provider == "private":
            if (config.get("binding") not in bucket_names or not isinstance(config.get("key"), str)
                    or not config["key"] or len(config["key"]) > 1024
                    or any(ord(char) < 32 for char in config["key"])):
                raise ValueError("invalid private resource")
        else:
            raise ValueError("unsupported fulfilment provider")
        filename = config.get("filename", "download")
        mime = config.get("mime_type", "application/octet-stream")
        if (not isinstance(filename, str) or not filename or len(filename) > 180
                or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in filename)
                or not isinstance(mime, str) or len(mime) > 127
                or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime)):
            raise ValueError("invalid resource filename or MIME type")


async def resolve_configured_resource(resource_id, resources, bindings):
    """Resolve a resource selected from an already-authorized order snapshot."""
    config = resources.get(resource_id) if isinstance(resources, dict) else None
    if not isinstance(config, dict):
        return {"status": "unavailable"}
    if config.get("provider") == "external":
        try:
            return {"status": "redirect", "url": external_url(config)}
        except ValueError:
            return {"status": "unavailable"}
    if config.get("provider") == "private":
        name, key = config.get("binding"), config.get("key")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
                or not isinstance(key, str) or not key or len(key) > 1024):
            return {"status": "unavailable"}
        bucket = getattr(bindings, name, None)
        if not callable(getattr(bucket, "get", None)):
            return {"status": "unavailable"}
        try:
            obj = await bucket.get(key)
        except Exception:
            return {"status": "unavailable"}
        if obj is None or getattr(obj, "body", None) is None:
            return {"status": "unavailable"}
        return {"status": "file", "body": obj.body, "filename": config.get("filename", "download"),
                "mime_type": config.get("mime_type", "application/octet-stream")}
    return {"status": "unavailable"}
