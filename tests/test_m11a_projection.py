from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from cartray.canonical import CanonicalItem, build_item_chunks, items_digest, projection_metadata
from cartray.stripe import CheckoutMetadataSealer, CheckoutMetadataVerifier, ProjectionSealError, signature_payload

FIXTURE = json.loads((Path(__file__).parent / "fixtures/m11a_schema1_projection.json").read_text())
METADATA = FIXTURE["metadata"]
PAYLOAD = (
    b'{"catalogue_version":"sha256:8d3674ae409653d9194f71fcdeb5f7b24f4d8442aac3fd966d0c2612aeba71ee",'
    b'"environment":"test","item_count":"2",'
    b'"items_digest":"sha256:bc015bfe893b50b78899b3dd40de8734c7b43ee9c8e67fcc419507b792a24e75",'
    b'"key_id":"kid-fixture-1","nonce":"nonce-fixture","order_id":"cr_order_fixture","schema":"1",'
    b'"session_id":"cs_test_fixture","source":"cartray"}'
)
SIGNATURE = base64.urlsafe_b64decode(METADATA["cr_signature"] + "==")
ITEMS = (CanonicalItem("EP-LMS-TRAINING-CATALOGUE", 1), CanonicalItem("EP-SIL-2026", 1))


@dataclass(frozen=True)
class ExactFixtureVerifier:
    async def verify(self, payload: bytes, signature: bytes) -> bool:
        return payload == PAYLOAD and signature == SIGNATURE


@dataclass(frozen=True)
class ExactPayloadVerifier:
    expected_payload: bytes

    async def verify(self, payload: bytes, signature: bytes) -> bool:
        return payload == self.expected_payload and signature == SIGNATURE


@dataclass(frozen=True)
class FixedSigner:
    signature: bytes

    async def sign(self, _payload: bytes) -> bytes:
        return self.signature


def verify_fixture(*, metadata=METADATA, session_id="cs_test_fixture", environment="test", keyring=None):
    if keyring is None:
        keyring = {"kid-fixture-1": ExactFixtureVerifier()}
    return asyncio.run(CheckoutMetadataVerifier(environment, keyring).verify(session_id=session_id, metadata=metadata))


def test_frozen_portal_fixture_matches_exact_canonical_bytes_and_items():
    unsigned = {key: value for key, value in METADATA.items() if key != "cr_signature"}
    assert signature_payload(session_id=FIXTURE["session_id"], environment="test", metadata=unsigned) == PAYLOAD
    assert items_digest(ITEMS) == METADATA["cr_items_digest"]
    assert build_item_chunks(ITEMS) == (METADATA["cr_items_01"],)
    assert projection_metadata(
        order_id="cr_order_fixture",
        catalogue_version=METADATA["cr_catalogue_version"],
        items=ITEMS,
        nonce="nonce-fixture",
    ) == {key: value for key, value in unsigned.items() if key != "cr_kid"}
    assert verify_fixture() == ITEMS


@pytest.mark.parametrize("environment", [None, "staging", "TEST", " test "])
def test_invalid_environment_fails_closed_in_sealer_and_verifier(environment):
    with pytest.raises(ProjectionSealError, match="environment"):
        asyncio.run(CheckoutMetadataSealer(environment, "kid", FixedSigner(SIGNATURE)).seal(
            session_id="cs_test_fixture", metadata=METADATA | {"cr_kid": "kid"} | {"cr_signature": ""}
        ))
    with pytest.raises(ProjectionSealError, match="environment"):
        verify_fixture(environment=environment)


@pytest.mark.parametrize("environment", ["test", "live"])
def test_exact_environments_are_accepted_for_sealing(environment):
    unsigned = {key: value for key, value in METADATA.items() if key not in ("cr_signature", "cr_kid")}
    sealed = asyncio.run(CheckoutMetadataSealer(environment, "kid-fixture-1", FixedSigner(SIGNATURE)).seal(
        session_id="cs_test_fixture", metadata=unsigned
    ))
    assert sealed["cr_signature"] == METADATA["cr_signature"]
    expected_payload = signature_payload(
        session_id="cs_test_fixture", environment=environment, metadata=unsigned | {"cr_kid": "kid-fixture-1"}
    )
    assert b'"environment":"' + environment.encode() + b'"' in expected_payload
    assert asyncio.run(CheckoutMetadataVerifier(
        environment, {"kid-fixture-1": ExactPayloadVerifier(expected_payload)}
    ).verify(session_id="cs_test_fixture", metadata=sealed)) == ITEMS


@pytest.mark.parametrize("field", [
    "cr_schema", "cr_source", "cr_order_id", "cr_catalogue_version", "cr_item_count", "cr_items_digest",
    "cr_nonce", "cr_kid", "cr_chunk_count", "cr_items_01",
])
def test_every_signed_field_or_chunk_mutation_is_rejected(field):
    changed = {**METADATA, field: METADATA[field] + "x"}
    with pytest.raises(ProjectionSealError):
        verify_fixture(metadata=changed)


def test_session_environment_unknown_field_and_signature_mutations_are_rejected():
    for kwargs in (
        {"session_id": "cs_test_another"},
        {"environment": "live"},
        {"metadata": {**METADATA, "cr_unknown": "x"}},
        {"metadata": {**METADATA, "cr_signature": "A" + METADATA["cr_signature"][1:]}},
        {"metadata": {**METADATA, "cr_signature": METADATA["cr_signature"] + "=="}},
        {"metadata": {**METADATA, "cr_signature": "*" + METADATA["cr_signature"][1:]}},
        {"metadata": {**METADATA, "cr_signature": base64.urlsafe_b64encode(bytes(64)).rstrip(b"=").decode()}},
    ):
        with pytest.raises(ProjectionSealError):
            verify_fixture(**kwargs)


def test_retired_and_unknown_key_ids_fail_with_non_empty_replacement_keyring():
    with pytest.raises(ProjectionSealError):
        verify_fixture(keyring={"replacement-key": ExactFixtureVerifier()})
    with pytest.raises(ProjectionSealError):
        verify_fixture(metadata={**METADATA, "cr_kid": "unknown-key"})


@pytest.mark.parametrize("signature", [b"", b"short", bytes(65)])
def test_sealer_rejects_non_ed25519_signature_lengths(signature):
    unsigned = {key: value for key, value in METADATA.items() if key not in ("cr_signature", "cr_kid")}
    with pytest.raises(ProjectionSealError, match="64-byte"):
        asyncio.run(CheckoutMetadataSealer("test", "kid-fixture-1", FixedSigner(signature)).seal(
            session_id="cs_test_fixture", metadata=unsigned
        ))
