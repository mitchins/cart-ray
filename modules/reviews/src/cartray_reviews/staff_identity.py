"""Independent staff assertion validation; no guest or checkout credentials.

The caller supplies a trusted policy and a JWKS loader pinned to its issuer.
No test administrator token or caller-supplied actor is accepted here.
"""

import base64
import hashlib
import hmac
import json
import re
from urllib.parse import urlparse

_SUBJECT = re.compile(r"[A-Za-z0-9._:@-]{1,200}\Z")
_KID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_ALLOWED_PERMISSIONS = frozenset({"reviews:moderate", "reviews:subjects", "reviews:operations"})


class StaffAuthenticationError(ValueError):
    """A staff identity, assertion or policy was not accepted."""


def _decode(value, maximum):
    if not isinstance(value, str) or not value or len(value) > maximum or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise StaffAuthenticationError("invalid assertion")
    try:
        data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise StaffAuthenticationError("invalid assertion") from exc
    if base64.urlsafe_b64encode(data).decode().rstrip("=") != value:
        raise StaffAuthenticationError("invalid assertion")
    return data


def _object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StaffAuthenticationError("ambiguous assertion")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise StaffAuthenticationError("invalid assertion") from exc
    if not isinstance(value, dict):
        raise StaffAuthenticationError("invalid assertion")
    return value


def _issuer(value):
    if not isinstance(value, str) or len(value) > 255:
        raise StaffAuthenticationError("invalid staff policy")
    try:
        url = urlparse(value)
        port = url.port
    except ValueError as exc:
        raise StaffAuthenticationError("invalid staff policy") from exc
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".cloudflareaccess.com")
        or url.username
        or url.password
        or port is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or value != f"https://{url.hostname}"
    ):
        raise StaffAuthenticationError("invalid staff policy")
    return value


def _verify_signature(key, signature, signed):
    if (
        key.get("kty") != "RSA"
        or key.get("alg") not in (None, "RS256")
        or key.get("use") not in (None, "sig")
        or ("key_ops" in key and key["key_ops"] != ["verify"])
    ):
        raise StaffAuthenticationError("invalid signing key")
    modulus = _decode(key.get("n"), 700)
    exponent = _decode(key.get("e"), 12)
    n, e = int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")
    if (
        not 2048 <= n.bit_length() <= 4096
        or modulus[0] == 0
        or exponent[0] == 0
        or e != 65537
        or len(signature) != len(modulus)
    ):
        raise StaffAuthenticationError("invalid signing key")
    sig_value = int.from_bytes(signature, "big")
    if sig_value >= n:
        raise StaffAuthenticationError("invalid signature")
    encoded = pow(sig_value, e, n).to_bytes(len(modulus), "big")
    digest = _SHA256_PREFIX + hashlib.sha256(signed).digest()
    padding_length = len(modulus) - len(digest) - 3
    if padding_length < 8 or not hmac.compare_digest(
        encoded, b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest
    ):
        raise StaffAuthenticationError("invalid signature")


async def authenticate_staff(assertion, *, issuer, audience, permissions, required_permission, now, load_jwks):
    """Validate Cloudflare Access RS256 assertion and authorize an explicit permission.

    load_jwks receives the fixed, policy-derived cert URL; it must enforce HTTPS,
    response bounds and a network deadline. An outage fails closed. Keys are loaded
    on each call, so removal and rotation take effect without stale local caches.
    """
    issuer = _issuer(issuer)
    if not isinstance(audience, str) or not audience or len(audience) > 256:
        raise StaffAuthenticationError("invalid staff policy")
    if required_permission not in _ALLOWED_PERMISSIONS or not isinstance(permissions, dict):
        raise StaffAuthenticationError("invalid staff policy")
    if not isinstance(now, int) or isinstance(now, bool) or not isinstance(assertion, str) or len(assertion) > 8192:
        raise StaffAuthenticationError("invalid assertion")
    parts = assertion.split(".")
    if len(parts) != 3:
        raise StaffAuthenticationError("invalid assertion")
    header = _object(_decode(parts[0], 2048))
    claims = _object(_decode(parts[1], 6000))
    signature = _decode(parts[2], 1024)
    if (
        set(header) - {"alg", "kid", "typ"}
        or header.get("alg") != "RS256"
        or not _KID.fullmatch(str(header.get("kid", "")))
    ):
        raise StaffAuthenticationError("invalid assertion header")
    subject = claims.get("sub")
    aud = claims.get("aud")
    exp, issued, not_before = claims.get("exp"), claims.get("iat"), claims.get("nbf", 0)
    if (
        claims.get("iss") != issuer
        or not isinstance(subject, str)
        or not _SUBJECT.fullmatch(subject)
        or not (aud == audience or isinstance(aud, list) and audience in aud and all(isinstance(x, str) for x in aud))
        or any(not isinstance(x, int) or isinstance(x, bool) for x in (exp, issued, not_before))
        or exp <= now
        or issued > now
        or not_before > now
        or exp <= issued
    ):
        raise StaffAuthenticationError("invalid assertion claims")
    try:
        jwks = await load_jwks(issuer + "/cdn-cgi/access/certs")
    except Exception as exc:
        raise StaffAuthenticationError("staff verification unavailable") from exc
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list) or len(keys) > 20:
        raise StaffAuthenticationError("invalid signing keys")
    matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]]
    if len(matches) != 1:
        raise StaffAuthenticationError("unknown signing key")
    _verify_signature(matches[0], signature, (parts[0] + "." + parts[1]).encode("ascii"))
    granted = permissions.get(subject)
    if (
        not isinstance(granted, list)
        or any(not isinstance(x, str) or x not in _ALLOWED_PERMISSIONS for x in granted)
        or required_permission not in granted
    ):
        raise StaffAuthenticationError("staff permission denied")
    stable_id = hashlib.sha256((issuer + "\x00" + subject).encode()).hexdigest()
    return {"actor_id": "staff:" + stable_id, "permission": required_permission}
