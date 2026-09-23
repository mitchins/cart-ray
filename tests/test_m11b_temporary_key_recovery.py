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


def _env(keyring: str, *, environment: str = "test", kid: str = KID) -> dict[str, str]:
    return {
        "CARTRAY_ENVIRONMENT": environment,
        "CARTRAY_SIGNING_KEY_ID": kid,
        "CARTRAY_PROJECTION_PUBLIC_KEYS_JSON": keyring,
    }


def test_recovery_logs_only_the_active_public_key_and_returns_no_content(monkeypatch):
    records = []
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)
    keyring = json.dumps({"other-kid": "not-a-key", KID: SPKI_B64})

    status, headers, body = TestClient(create_app(), env=_env(keyring)).request(
        "GET", "/_m11b/test-public-key-recovery"
    )

    assert status == 204
    assert body in ("", b"")
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers
    assert records == [{
        "kid": KID,
        "spki_b64": SPKI_B64,
        "spki_sha256": "sha256:9408457aefd071cec127c1f98539930861ad1ba94c940db975c972c09fc68b68",
    }]
    assert "other-kid" not in json.dumps(records)


@pytest.mark.parametrize("keyring", [
    "{}",
    "[]",
    "not-json",
    json.dumps({"other-kid": SPKI_B64}),
    json.dumps({KID: SPKI_B64.rstrip("=")}),
    json.dumps({KID: base64.b64encode(bytes(43)).decode()}),
    json.dumps({KID: base64.b64encode(bytes(12) + bytes(range(32))).decode()}),
    json.dumps({KID: base64.b64encode(SPKI + b"x").decode()}),
    json.dumps({KID: 4}),
    "{\"cartray-test-2026-09-01\":\"first\",\"cartray-test-2026-09-01\":\"second\"}",
])
def test_recovery_rejects_malformed_keyrings_without_logging(monkeypatch, keyring):
    records = []
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)

    status, headers, body = TestClient(create_app(), env=_env(keyring)).request(
        "GET", "/_m11b/test-public-key-recovery"
    )

    assert status == 503
    assert body in ("", b"")
    assert headers["Cache-Control"] == "no-store"
    assert records == []


@pytest.mark.parametrize("environment,kid", [("live", KID), ("TEST", KID), ("test", "other-kid")])
def test_recovery_rejects_wrong_environment_or_signing_kid(monkeypatch, environment, kid):
    records = []
    monkeypatch.setattr(worker_module, "_emit_public_key_recovery_record", records.append)

    status, _headers, _body = TestClient(create_app(), env=_env(json.dumps({KID: SPKI_B64}),
                                                           environment=environment, kid=kid)).request(
        "GET", "/_m11b/test-public-key-recovery"
    )

    assert status == 503
    assert records == []
