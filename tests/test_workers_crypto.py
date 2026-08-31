from __future__ import annotations

import asyncio
import base64
import sys
from types import SimpleNamespace

from cartray.workers_crypto import WorkersEd25519Signer


class RecordingSubtle:
    def __init__(self) -> None:
        self.imported: tuple[object, object, object, object, object] | None = None
        self.signed: tuple[object, object, object] | None = None

    async def importKey(self, format_name, key_data, algorithm, extractable, usages):
        self.imported = (format_name, key_data, algorithm, extractable, usages)
        return "known-private-key"

    async def sign(self, algorithm, key, payload):
        self.signed = (algorithm, key, payload)
        return b"known-signature"


def test_worker_signer_imports_a_known_pkcs8_key_and_reuses_it(monkeypatch):
    subtle = RecordingSubtle()
    monkeypatch.setitem(sys.modules, "js", SimpleNamespace(crypto=SimpleNamespace(subtle=subtle)))
    monkeypatch.setattr("cartray.workers_crypto.bytes_to_arraybuffer", lambda value: value)
    monkeypatch.setattr("cartray.workers_crypto.arraybuffer_to_bytes", bytes)
    monkeypatch.setattr("cartray.workers_crypto.to_js", lambda value: value)
    # PKCS#8 DER prefix for Ed25519 followed by a fixed test-only 32-byte seed.
    private_key = b'0.\x02\x01\x000\x05\x06\x03+ep\x04"\x04 ' + bytes(range(32))
    signer = WorkersEd25519Signer(base64.b64encode(private_key).decode("ascii"))

    first = asyncio.run(signer.sign(b"first payload"))
    second = asyncio.run(signer.sign(b"second payload"))

    assert first == second == b"known-signature"
    assert subtle.imported == ("pkcs8", private_key, {"name": "Ed25519"}, False, ["sign"])
    assert subtle.signed == ("Ed25519", "known-private-key", b"second payload")
