"""Temporary, test-only assertions; remove with the recovery route before merge."""

import base64
import json

import pytest
from kinglet import TestClient

import cartray.worker as worker_module
from cartray.worker import create_app

KID = "cartray-test-2026-09-01"
SPKI = bytes.fromhex("302a300506032b6570032100") + bytes(range(32))
SPKI_B64 = base64.b64encode(SPKI).decode()
TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
OTHER_TOKEN = base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode().rstrip("=")
CHALLENGE = b"cartray-m11b-public-key-recovery-v1\ntest\ncartray-test-2026-09-01\n"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class MatchingSigner:
    def __init__(self, private_key):
        assert private_key == "synthetic-private-key"

    async def sign(self, payload):
        assert payload == CHALLENGE
        return bytes(range(64))


class MatchingVerifier:
    def __init__(self, public_key):
        assert public_key == SPKI_B64

    async def verify(self, payload, signature):
        assert payload == CHALLENGE
        assert signature == bytes(range(64))
        return True


@pytest.fixture(autouse=True)
def matching_crypto(monkeypatch):
    monkeypatch.setattr(worker_module, "WorkersEd25519Signer", MatchingSigner)
    monkeypatch.setattr(worker_module, "WorkersEd25519Verifier", MatchingVerifier)


def _env(keyring: str, *, environment: str = "test", kid: str = KID) -> dict[str, str]:
    return {
        "CARTRAY_ENVIRONMENT": environment,
        "CARTRAY_SIGNING_KEY_ID": kid,
        "CARTRAY_PROJECTION_PUBLIC_KEYS_JSON": keyring,
        "CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64": "synthetic-private-key",
        "CARTRAY_RECOVERY_TOKEN": TOKEN,
    }


def test_recovery_logs_only_the_active_public_key_and_returns_no_content(monkeypatch):
    records = []
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)
    keyring = json.dumps({"other-kid": "not-a-key", KID: SPKI_B64})

    status, headers, body = TestClient(create_app(), env=_env(keyring)).request(
        "GET", "/_m11b/test-public-key-recovery", headers=AUTH
    )

    assert status == 204
    assert body in ("", b"")
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers
    assert records == [
        {
            "kid": KID,
            "spki_b64": SPKI_B64,
            "spki_sha256": "sha256:9408457aefd071cec127c1f98539930861ad1ba94c940db975c972c09fc68b68",
        }
    ]
    assert "other-kid" not in json.dumps(records)


@pytest.mark.parametrize(
    "keyring",
    [
        "{}",
        "[]",
        "not-json",
        json.dumps({"other-kid": SPKI_B64}),
        json.dumps({KID: SPKI_B64.rstrip("=")}),
        json.dumps({KID: base64.b64encode(bytes(43)).decode()}),
        json.dumps({KID: base64.b64encode(bytes(12) + bytes(range(32))).decode()}),
        json.dumps({KID: base64.b64encode(SPKI + b"x").decode()}),
        json.dumps({KID: 4}),
        '{"cartray-test-2026-09-01":"first","cartray-test-2026-09-01":"second"}',
    ],
)
def test_recovery_rejects_malformed_keyrings_without_logging(monkeypatch, keyring):
    records = []
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)

    status, headers, body = TestClient(create_app(), env=_env(keyring)).request(
        "GET", "/_m11b/test-public-key-recovery", headers=AUTH
    )

    assert status == 503
    assert body in ("", b"")
    assert headers["Cache-Control"] == "no-store"
    assert records == []


@pytest.mark.parametrize("environment,kid", [("live", KID), ("TEST", KID), ("test", "other-kid")])
def test_recovery_rejects_wrong_environment_or_signing_kid(monkeypatch, environment, kid):
    records = []
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)

    status, _headers, _body = TestClient(
        create_app(), env=_env(json.dumps({KID: SPKI_B64}), environment=environment, kid=kid)
    ).request("GET", "/_m11b/test-public-key-recovery", headers=AUTH)

    assert status == 503
    assert records == []


@pytest.mark.parametrize(
    "authorization", [None, "Bearer wrong", f"Bearer {OTHER_TOKEN}", f"bearer {TOKEN}", f"Bearer {TOKEN} "]
)
def test_recovery_rejects_unauthorized_before_reading_other_bindings(monkeypatch, authorization):
    records = []
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)
    monkeypatch.setattr(worker_module, "_public_key_recovery_record", lambda _env: pytest.fail("read domain bindings"))
    headers = {} if authorization is None else {"authorization": authorization}
    status, response_headers, body = TestClient(create_app(), env={"CARTRAY_RECOVERY_TOKEN": TOKEN}).request(
        "GET", "/_m11b/test-public-key-recovery", headers=headers
    )
    assert status == 404
    assert body in ("", b"")
    assert response_headers["Cache-Control"] == "no-store"
    assert records == []


@pytest.mark.parametrize("configured", [None, "short", "A" * 42 + "B", TOKEN + "="])
def test_recovery_rejects_invalid_configured_token_without_logging(monkeypatch, configured):
    records = []
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)
    environment = _env(json.dumps({KID: SPKI_B64}))
    environment["CARTRAY_RECOVERY_TOKEN"] = configured
    status, headers, body = TestClient(create_app(), env=environment).request(
        "GET", "/_m11b/test-public-key-recovery", headers=AUTH
    )
    assert status == 404
    assert body in ("", b"")
    assert headers["Cache-Control"] == "no-store"
    assert records == []


@pytest.mark.parametrize("failure", ["signer_exception", "short_signature", "verifier_exception", "verifier_false"])
def test_recovery_rejects_signer_verifier_failures_without_logging(monkeypatch, failure):
    class FailingSigner(MatchingSigner):
        async def sign(self, payload):
            if failure == "signer_exception":
                raise RuntimeError("synthetic failure")
            if failure == "short_signature":
                return b"short"
            return await super().sign(payload)

    class FailingVerifier(MatchingVerifier):
        async def verify(self, payload, signature):
            if failure == "verifier_exception":
                raise RuntimeError("synthetic failure")
            if failure == "verifier_false":
                return False
            return await super().verify(payload, signature)

    records = []
    monkeypatch.setattr(worker_module, "WorkersEd25519Signer", FailingSigner)
    monkeypatch.setattr(worker_module, "WorkersEd25519Verifier", FailingVerifier)
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)
    status, headers, body = TestClient(create_app(), env=_env(json.dumps({KID: SPKI_B64}))).request(
        "GET", "/_m11b/test-public-key-recovery", headers=AUTH
    )
    assert status == 503
    assert body in ("", b"")
    assert headers["Cache-Control"] == "no-store"
    assert records == []
