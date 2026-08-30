from __future__ import annotations

from collections.abc import Mapping

from .stripe import AsyncStripeTransport


class WorkersFetchTransport(AsyncStripeTransport):
    """Stripe transport for the Cloudflare Python Workers request context."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: str | None = None,
    ) -> tuple[int, Mapping[str, object]]:
        try:
            from workers import fetch
        except ImportError as error:
            raise RuntimeError("WorkersFetchTransport is available only in a Cloudflare Python Worker") from error

        response = await fetch(
            f"https://api.stripe.com{path}",
            method=method,
            headers=dict(headers),
            body=body,
        )
        payload = await response.json()
        if hasattr(payload, "to_py"):
            payload = payload.to_py()
        if not isinstance(payload, dict):
            raise RuntimeError("Stripe returned a non-object API response")
        return response.status, payload
