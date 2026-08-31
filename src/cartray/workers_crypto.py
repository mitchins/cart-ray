from __future__ import annotations

import base64
from dataclasses import dataclass, field

from kinglet.storage import arraybuffer_to_bytes, bytes_to_arraybuffer
from pyodide.ffi import to_js


@dataclass
class WorkersEd25519Signer:
    """Ed25519 signer backed by a PKCS#8 key held in a Worker secret."""

    private_key_pkcs8_b64: str
    _key: object | None = field(default=None, init=False, repr=False)

    async def sign(self, payload: bytes) -> bytes:
        try:
            from js import crypto
        except ImportError as error:
            raise RuntimeError("WorkersEd25519Signer is available only in a Cloudflare Python Worker") from error
        if self._key is None:
            try:
                encoded = base64.b64decode(self.private_key_pkcs8_b64, validate=True)
            except (ValueError, TypeError) as error:
                raise RuntimeError("CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64 is not valid base64") from error
            try:
                self._key = await crypto.subtle.importKey(
                    "pkcs8", bytes_to_arraybuffer(encoded), {"name": "Ed25519"}, False, to_js(["sign"])
                )
            except Exception as error:
                raise RuntimeError("CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64 is not an Ed25519 PKCS#8 key") from error
        signature = await crypto.subtle.sign("Ed25519", self._key, bytes_to_arraybuffer(payload))
        return arraybuffer_to_bytes(signature)
