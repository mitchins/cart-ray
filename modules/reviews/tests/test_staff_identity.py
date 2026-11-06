"""Exercise real RS256 signatures and explicit staff authorization."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest
from cartray_reviews.staff_identity import StaffAuthenticationError, authenticate_staff

KEY = json.loads((Path(__file__).parent / "fixtures/synthetic-staff-rsa.json").read_text())
ISSUER = "https://example.cloudflareaccess.com"
AUDIENCE = "reviews-app"
NOW = 1_800_000_000


def encode(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def integer(value):
    return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")


def token(claims=None, header=None, key=KEY):
    header = header or {"alg": "RS256", "kid": "key-1", "typ": "JWT"}
    claims = claims or {"iss": ISSUER, "aud": [AUDIENCE], "sub": "staff-1", "iat": NOW - 5, "exp": NOW + 60}
    prefix = (
        encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + encode(json.dumps(claims, separators=(",", ":")).encode())
    )
    digest = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(prefix.encode()).digest()
    n, d = integer(key["n"]), integer(key["d"])
    size = (n.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(encoded, "big"), d, n).to_bytes(size, "big")
    return prefix + "." + encode(signature)


def authenticate(value, *, permission="reviews:moderate", policy=None, jwks=None, issuer=ISSUER):
    async def loader(url):
        assert url == ISSUER + "/cdn-cgi/access/certs"
        if isinstance(jwks, Exception):
            raise jwks
        return jwks if jwks is not None else {"keys": [{"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}]}

    return asyncio.run(
        authenticate_staff(
            value,
            issuer=issuer,
            audience=AUDIENCE,
            permissions=policy or {},
            required_permission=permission,
            now=NOW,
            load_jwks=loader,
        )
    )


def test_verified_identity_and_explicit_permission():
    policy = {"staff-1": ["reviews:moderate"]}
    result = authenticate(token(), policy=policy)
    assert result["actor_id"].startswith("staff:")
    assert "staff-1" not in result["actor_id"]
    assert authenticate(token(), policy=policy) == result
    with pytest.raises(StaffAuthenticationError):
        authenticate(token(), permission="reviews:subjects", policy=policy)
    with pytest.raises(StaffAuthenticationError):
        authenticate(token(), policy={})


@pytest.mark.parametrize(
    "change",
    [
        {"exp": NOW},
        {"iat": NOW + 1},
        {"nbf": NOW + 1},
        {"aud": "other"},
        {"iss": "https://evil.cloudflareaccess.com"},
        {"sub": "other"},
    ],
)
def test_invalid_claims_or_unlisted_subject_fail_closed(change):
    claims = {"iss": ISSUER, "aud": [AUDIENCE], "sub": "staff-1", "iat": NOW - 5, "exp": NOW + 60} | change
    with pytest.raises(StaffAuthenticationError):
        authenticate(token(claims), policy={"staff-1": ["reviews:moderate"]})


def test_signature_rotation_outage_and_duplicate_keys_fail_closed():
    value = token()
    policy = {"staff-1": ["reviews:moderate"]}
    with pytest.raises(StaffAuthenticationError):
        authenticate(value[:-2] + ("AA" if value[-2:] != "AA" else "BB"), policy=policy)
    with pytest.raises(StaffAuthenticationError):
        authenticate(value, policy=policy, jwks={"keys": []})
    key = {"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}
    with pytest.raises(StaffAuthenticationError):
        authenticate(value, policy=policy, jwks={"keys": [key, key]})
    with pytest.raises(StaffAuthenticationError):
        authenticate(value, policy=policy, jwks=RuntimeError("offline"))
    with pytest.raises(StaffAuthenticationError):
        authenticate(token(header={"alg": "none", "kid": "key-1"}), policy=policy)


@pytest.mark.parametrize(
    "assertion",
    [
        "",
        "a.b",
        "!invalid.e30.sig",
        "e30.e30.a",
        "e30.e30.%%%",
        "e30.e30." + "x" * 8200,
        encode(b'{"alg":"RS256","alg":"none","kid":"key-1"}') + ".e30.eA",
        encode(b"[]") + ".e30.eA",
        encode(b'{"alg":NaN}') + ".e30.eA",
    ],
)
def test_malformed_or_ambiguous_assertions_are_rejected(assertion):
    with pytest.raises(StaffAuthenticationError):
        authenticate(assertion, policy={"staff-1": ["reviews:moderate"]})


@pytest.mark.parametrize(
    "issuer",
    [
        "http://example.cloudflareaccess.com",
        "https://example.cloudflareaccess.com:443",
        "https://example.cloudflareaccess.com/path",
        "https://evil.example",
        "https://user@example.cloudflareaccess.com",
        "https://example.cloudflareaccess.com?query=x",
        "https://example.cloudflareaccess.com/#fragment",
        "https://example.cloudflareaccess.com:bad",
        "x" * 256,
    ],
)
def test_issuer_policy_rejects_untrusted_endpoints(issuer):
    with pytest.raises(StaffAuthenticationError):
        authenticate(token(), issuer=issuer, policy={"staff-1": ["reviews:moderate"]})


@pytest.mark.parametrize(
    "change",
    [
        {"kty": "EC"},
        {"alg": "HS256"},
        {"use": "enc"},
        {"key_ops": ["sign"]},
        {"n": "AQ"},
        {"e": "Aw"},
        {"n": "A"},
        {"e": "AA"},
    ],
)
def test_wrong_or_malformed_signing_keys_are_rejected(change):
    key = {"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]} | change
    with pytest.raises(StaffAuthenticationError):
        authenticate(token(), policy={"staff-1": ["reviews:moderate"]}, jwks={"keys": [key]})


def test_untrusted_policy_and_jwks_structures_fail_closed():
    for jwks in ({"keys": [None] * 21}, {"keys": "not-a-list"}, [], {"keys": [None]}):
        with pytest.raises(StaffAuthenticationError):
            authenticate(token(), policy={"staff-1": ["reviews:moderate"]}, jwks=jwks)
    with pytest.raises(StaffAuthenticationError):
        authenticate(token(), policy={"staff-1": [["reviews:moderate"]]})
