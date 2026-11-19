import asyncio
import hmac
import json
import time
import tomllib
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from kinglet import TestClient

import cartray.worker as worker_module
from cartray.compiled_catalogue import COMPILED_CATALOGUE
from cartray.errors import CheckoutValidationError, SettlementInconsistency
from cartray.models import CheckoutRedirect
from cartray.presentation import build_presented_catalogue
from cartray.stripe import ProjectionSealError
from cartray.worker import app, create_app

STOREFRONT_ORIGIN = "https://cartray-store-test.pages.dev"


def test_invitation_mode_is_explicit_and_squirrel_test_config_selects_automatic():
    config = tomllib.loads(
        (Path(__file__).parents[1] / "examples/acorn-store/cartray-test-vars.example.toml").read_text()
    )["vars"]
    assert worker_module._automatic_invitation_enabled(SimpleNamespace(**config))
    for mode in (None, "", "disabled", "receipt-only", "Automatic", True):
        assert not worker_module._automatic_invitation_enabled(SimpleNamespace(CARTRAY_REVIEW_INVITATION_MODE=mode))
    assert not worker_module._automatic_invitation_enabled(SimpleNamespace())


def test_health_endpoint_declares_test_only_mode():
    status, _headers, body = TestClient(app).request("GET", "/health")

    assert status == 200
    assert json.loads(body) == {"service": "cartray", "mode": "test-only", "status": "ok"}


@dataclass
class RecordingCheckoutService:
    redirects: int = 0

    async def checkout(self, _request):
        self.redirects += 1
        return CheckoutRedirect("cs_test_worker", "https://checkout.stripe.test/cs_test_worker")


@dataclass
class FailingCheckoutService:
    error: Exception

    async def checkout(self, _request):
        raise self.error


@dataclass
class FixtureRateLimiter:
    allowed: bool
    calls: int = 0

    async def limit(self, _options):
        self.calls += 1
        return {"success": self.allowed}


@dataclass
class RecordingStatusStore:
    states: dict[str, str]
    calls: int = 0
    receipt: dict | None = None

    async def checkout_status(self, session_id):
        self.calls += 1
        return self.states.get(session_id)

    async def checkout_receipt(self, session_id):
        assert self.states.get(session_id) == "confirmed"
        return self.receipt


STORE_ENV = {
    "CARTRAY_STOREFRONT_ORIGIN": STOREFRONT_ORIGIN,
    "CHECKOUT_RATE_LIMITER": FixtureRateLimiter(True),
    "CHECKOUT_STORE_RATE_LIMITER": FixtureRateLimiter(True),
    "CHECKOUT_STATUS_RATE_LIMITER": FixtureRateLimiter(True),
    "CARTRAY_SIGNING_KEY_ID": "fixture-installation",
}


@dataclass
class RecordingSettlementService:
    events: list = None
    error: Exception | None = None

    def __post_init__(self):
        self.events = [] if self.events is None else self.events

    async def settle(self, event):
        if self.error is not None:
            raise self.error
        self.events.append(event)
        return False


def test_scheduled_entrypoint_runs_only_the_server_side_reconciliation(monkeypatch):
    observed = []

    async def scheduled(env):
        observed.append(env)

    monkeypatch.setattr(worker_module, "scheduled_reconciliation_from_environment", scheduled)
    env = {"CARTRAY_ENVIRONMENT": "test"}
    entrypoint = worker_module.Default()
    entrypoint.env = env

    asyncio.run(entrypoint.scheduled(None, object(), None))

    assert observed == [env]


def _webhook_headers(body: bytes, secret: str, *, timestamp: int | None = None) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, sha256).hexdigest()
    return {"stripe-signature": f"t={timestamp},v1={signature}", "content-length": str(len(body))}


def test_checkout_route_accepts_only_valid_json_and_returns_a_redirect(fixture_catalogue):
    service = RecordingCheckoutService()

    async def factory(_env):
        return service

    client = TestClient(create_app(factory), env=STORE_ENV)
    payload = {
        "checkout_request_id": "worker-request-1",
        "manifest_version": fixture_catalogue.version,
        "items": [{"product_key": "TEST-TEMPLATE", "quantity": 1}],
    }
    status, headers, body = client.request(
        "POST", "/checkout", json_data=payload, headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"}
    )

    assert status == 201
    assert json.loads(body) == {
        "checkout_url": "https://checkout.stripe.test/cs_test_worker",
        "session_id": "cs_test_worker",
    }
    assert service.redirects == 1
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN
    assert headers["Vary"] == "Origin"

    status, _headers, _body = client.request(
        "POST",
        "/checkout",
        json_data={**payload, "amount_minor": 1},
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 400
    assert service.redirects == 1

    status, _headers, _body = client.request(
        "POST",
        "/checkout",
        data="{}",
        headers={"content-type": "text/plain", "origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 400

    status, _headers, _body = client.request(
        "POST",
        "/checkout",
        json_data={**payload, "items": [{"product_key": "TEST-TEMPLATE", "quantity": 1, "amount": 1}]},
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 400

    status, _headers, _body = client.request(
        "POST",
        "/checkout",
        data="x" * 16_385,
        headers={"content-type": "application/json", "origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 400


def test_checkout_route_maps_domain_validation_to_bad_request(fixture_catalogue):
    async def factory(_env):
        return FailingCheckoutService(CheckoutValidationError("catalogue refresh required"))

    client = TestClient(create_app(factory), env=STORE_ENV)
    status, headers, body = client.request(
        "POST",
        "/checkout",
        json_data={
            "checkout_request_id": "worker-validation-request",
            "manifest_version": fixture_catalogue.version,
            "items": [{"product_key": "TEST-TEMPLATE", "quantity": 1}],
        },
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )

    assert status == 400
    assert json.loads(body) == {"error": "catalogue refresh required"}
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN


def test_checkout_rate_limit_rejects_before_invoking_the_checkout_service(fixture_catalogue):
    service = RecordingCheckoutService()
    limiter = FixtureRateLimiter(False)
    backstop = FixtureRateLimiter(True)

    async def factory(_env):
        return service

    client = TestClient(create_app(factory), env={**STORE_ENV, "CHECKOUT_RATE_LIMITER": limiter,
                                                   "CHECKOUT_STORE_RATE_LIMITER": backstop})
    status, headers, body = client.request(
        "POST",
        "/checkout",
        json_data={
            "checkout_request_id": "worker-rate-limited",
            "manifest_version": fixture_catalogue.version,
            "items": [{"product_key": "TEST-TEMPLATE", "quantity": 1}],
        },
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )

    assert status == 429
    assert json.loads(body) == {"error": "checkout rate limit exceeded"}
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN
    assert limiter.calls == 1
    assert backstop.calls == 0
    assert service.redirects == 0


def test_checkout_budgets_are_scoped_to_actor_and_installation():
    class Limiter:
        def __init__(self):
            self.keys = []

        async def limit(self, options):
            self.keys.append(options["key"])
            return {"success": True}

    limiter = Limiter()
    for installation, ip in (("a", "192.0.2.1"), ("a", "192.0.2.2"), ("b", "192.0.2.1")):
        request = SimpleNamespace(
            env=SimpleNamespace(
                CHECKOUT_RATE_LIMITER=limiter,
                CHECKOUT_STORE_RATE_LIMITER=FixtureRateLimiter(True),
                CARTRAY_SIGNING_KEY_ID=installation,
            ),
            header=lambda _: ip,
        )
        assert asyncio.run(worker_module._checkout_rate_limit(request))
    assert len(set(limiter.keys)) == 3


def test_checkout_route_fails_closed_for_unavailable_configuration(fixture_catalogue):
    async def factory(_env):
        return FailingCheckoutService(ProjectionSealError("missing signing key"))

    client = TestClient(create_app(factory), env=STORE_ENV)
    status, headers, body = client.request(
        "POST",
        "/checkout",
        json_data={
            "checkout_request_id": "worker-unavailable-request",
            "manifest_version": fixture_catalogue.version,
            "items": [{"product_key": "TEST-TEMPLATE", "quantity": 1}],
        },
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )

    assert status == 503
    assert json.loads(body) == {"error": "checkout unavailable"}
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN


def test_catalogue_route_has_an_active_only_public_allowlist_and_cors(fixture_catalogue):
    async def catalogue_factory(_env):
        return fixture_catalogue

    status, headers, body = TestClient(create_app(catalogue_factory=catalogue_factory), env=STORE_ENV).request(
        "GET", "/catalogue", headers={"origin": STOREFRONT_ORIGIN}
    )

    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN
    payload = json.loads(body)
    assert set(payload) == {"version", "products"}
    assert set(payload["products"][0]) == {"product_key", "title", "amount_minor", "currency", "max_quantity"}
    assert "stripe_price_id" not in json.dumps(payload)
    assert "fulfilment_resources" not in json.dumps(payload)


def test_catalogue_route_can_serve_trusted_public_presentation(fixture_catalogue):
    async def catalogue_factory(_env):
        return build_presented_catalogue(fixture_catalogue, COMPILED_CATALOGUE.presentation_sources)

    status, _headers, body = TestClient(create_app(catalogue_factory=catalogue_factory), env=STORE_ENV).request(
        "GET", "/catalogue", headers={"origin": STOREFRONT_ORIGIN}
    )

    payload = json.loads(body)
    assert status == 200
    assert set(payload) == {"version", "presentation_version", "products"}
    assert set(payload["products"][0]) == {
        "product_key",
        "title",
        "short_description",
        "image_url",
        "images",
        "amount_minor",
        "currency",
        "max_quantity",
    }
    assert payload["products"][0]["images"][0]["url"] == payload["products"][0]["image_url"]
    assert "stripe_price_id" not in json.dumps(payload)
    assert "fulfilment_resources" not in json.dumps(payload)


def test_checkout_status_is_a_minimal_d1_only_browser_read():
    store = RecordingStatusStore(
        {"cs_test_pending": "pending", "cs_test_confirmed": "confirmed", "cs_test_expired": "expired"}
    )

    async def status_factory(_env):
        return store

    client = TestClient(create_app(status_store_factory=status_factory), env=STORE_ENV)
    status, headers, body = client.request(
        "GET",
        "/checkout-status?session_id=cs_test_pending",
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 200
    assert json.loads(body) == {"state": "pending"}
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN
    assert headers["Cache-Control"] == "no-store"

    status, _headers, body = client.request(
        "GET",
        "/checkout-status?session_id=cs_test_confirmed",
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 200
    assert json.loads(body) == {"state": "confirmed"}
    assert store.calls == 2

    status, _headers, body = client.request(
        "GET",
        "/checkout-status?session_id=cs_test_expired",
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 200
    assert json.loads(body) == {"state": "expired"}
    assert store.calls == 3

    status, headers, body = client.request(
        "GET",
        "/checkout-status?session_id=not-a-session",
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 404
    assert json.loads(body) == {"error": "checkout not found"}
    assert headers["Cache-Control"] == "no-store"
    assert store.calls == 3

    status, _headers, body = client.request(
        "GET",
        "/checkout-status?session_id=cs_test_unknown",
        headers={"origin": STOREFRONT_ORIGIN, "cf-connecting-ip": "192.0.2.1"},
    )
    assert status == 404
    assert json.loads(body) == {"error": "checkout not found"}
    assert store.calls == 4


def test_checkout_status_only_includes_receipt_for_confirmed_sessions():
    receipt = {"currency": "AUD", "items": [{"product_key": "TEST-TEMPLATE", "quantity": 2, "unit_amount_minor": 2500}]}
    store = RecordingStatusStore(
        {"cs_test_pending": "pending", "cs_test_confirmed": "confirmed", "cs_test_expired": "expired"},
        receipt=receipt,
    )

    async def status_factory(_env):
        return store

    client = TestClient(create_app(status_store_factory=status_factory), env=STORE_ENV)
    for state in ("pending", "confirmed", "expired", "unknown"):
        status, headers, body = client.request(
            "GET", f"/checkout-status?session_id=cs_test_{state}", headers={"cf-connecting-ip": "192.0.2.1"}
        )
        assert headers["Cache-Control"] == "no-store"
        assert status == (404 if state == "unknown" else 200)
        expected = {"error": "checkout not found"} if state == "unknown" else {"state": state}
        if state == "confirmed":
            expected["receipt"] = receipt
        assert json.loads(body) == expected


def test_status_budget_stops_originless_requests_before_database():
    store = RecordingStatusStore({"cs_test_pending": "pending"})
    limiter = FixtureRateLimiter(False)

    async def status_factory(_env):
        return store

    env = {**STORE_ENV, "CHECKOUT_STATUS_RATE_LIMITER": limiter}
    client = TestClient(create_app(status_store_factory=status_factory), env=env)
    for _ in range(10):
        status, headers, _ = client.request(
            "GET", "/checkout-status?session_id=cs_test_pending", headers={"cf-connecting-ip": "192.0.2.1"}
        )
        assert status == 429
        assert headers["Cache-Control"] == "no-store"
    assert store.calls == 0
    assert limiter.calls == 10


def test_status_rejects_bad_queries_and_missing_edge_admission_before_database():
    store = RecordingStatusStore({"cs_test_pending": "pending"})

    async def status_factory(_env):
        return store

    client = TestClient(create_app(status_store_factory=status_factory), env=STORE_ENV)
    for query in ("?session_id=cs_test_" + "x" * 300, "?a=1&b=2&session_id=cs_test_pending"):
        status, headers, _ = client.request(
            "GET", "/checkout-status" + query, headers={"cf-connecting-ip": "192.0.2.1"}
        )
        assert status == 404
        assert headers["Cache-Control"] == "no-store"
    status, headers, _ = client.request("GET", "/checkout-status?session_id=cs_test_pending")
    assert status == 503
    assert headers["Cache-Control"] == "no-store"
    assert store.calls == 0

    env = {**STORE_ENV, "CHECKOUT_STATUS_RATE_LIMITER": None}
    client = TestClient(create_app(status_store_factory=status_factory), env=env)
    status, _, _ = client.request(
        "GET", "/checkout-status?session_id=cs_test_pending", headers={"cf-connecting-ip": "192.0.2.1"}
    )
    assert status == 503
    assert store.calls == 0


def test_status_normalizes_ipv4_mapped_and_ipv6_client_budgets():
    class RecordingLimiter:
        def __init__(self):
            self.keys = []

        async def limit(self, options):
            self.keys.append(options["key"])
            return {"success": True}

    limiter = RecordingLimiter()
    for address in ("192.0.2.1", "::ffff:192.0.2.1", "2001:db8::1", "2001:db8::2", "192.0.2.2", "2001:db8:1::1"):
        request = SimpleNamespace(
            env=SimpleNamespace(CHECKOUT_STATUS_RATE_LIMITER=limiter, CARTRAY_SIGNING_KEY_ID="store-one"),
            header=lambda _: address,
        )
        assert asyncio.run(worker_module._checkout_status_rate_limit(request))
    assert limiter.keys[0] == limiter.keys[1]
    assert limiter.keys[2] == limiter.keys[3]
    assert len({limiter.keys[0], limiter.keys[2], limiter.keys[4], limiter.keys[5]}) == 4
    other = SimpleNamespace(
        env=SimpleNamespace(CHECKOUT_STATUS_RATE_LIMITER=limiter, CARTRAY_SIGNING_KEY_ID="store-two"),
        header=lambda _: "192.0.2.1",
    )
    assert asyncio.run(worker_module._checkout_status_rate_limit(other))
    assert limiter.keys[-1] != limiter.keys[0]


def test_public_catalogue_snapshot_needs_neither_stripe_nor_d1(monkeypatch):
    def forbidden(_env):
        raise AssertionError("public read attempted to contact Stripe")

    monkeypatch.setattr(worker_module, "_stripe_client", forbidden)
    locked = replace(COMPILED_CATALOGUE, expected_catalogue_version=COMPILED_CATALOGUE.public_manifest()["version"])
    monkeypatch.setattr(worker_module, "COMPILED_CATALOGUE", locked)
    client = TestClient(app, env={"CARTRAY_ENVIRONMENT": "test", "CARTRAY_STOREFRONT_ORIGIN": STOREFRONT_ORIGIN})
    for headers in ({}, {"origin": STOREFRONT_ORIGIN}):
        status, response_headers, body = client.request("GET", "/catalogue", headers=headers)
        assert status == 200
        assert response_headers["Cache-Control"] == "no-store"
        assert json.loads(body) == COMPILED_CATALOGUE.public_manifest()


def test_unlocked_fixture_cannot_be_used_as_connected_commerce():
    with pytest.raises(RuntimeError, match="preflight"):
        worker_module._require_locked_catalogue()


def test_checkout_status_rejects_foreign_origins_and_has_route_specific_preflight():
    async def forbidden_factory(_env):
        raise AssertionError("a rejected origin must not access D1")

    client = TestClient(create_app(status_store_factory=forbidden_factory), env=STORE_ENV)
    status, headers, _body = client.request(
        "GET", "/checkout-status?session_id=cs_test_pending", headers={"origin": "https://attacker.invalid"}
    )
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers

    status, headers, _body = client.request(
        "OPTIONS",
        "/checkout-status",
        headers={
            "origin": STOREFRONT_ORIGIN,
            "access-control-request-method": "GET",
        },
    )
    assert status == 204
    assert headers["Access-Control-Allow-Methods"] == "GET"
    assert "Access-Control-Allow-Headers" not in headers

    status, _headers, _body = client.request(
        "OPTIONS",
        "/checkout-status",
        headers={
            "origin": STOREFRONT_ORIGIN,
            "access-control-request-method": "GET",
            "access-control-request-headers": "content-type",
        },
    )
    assert status == 403


def test_cors_rejects_other_origins_before_invoking_browser_factories():
    async def forbidden_factory(_env):
        raise AssertionError("a rejected origin must not invoke the catalogue factory")

    client = TestClient(create_app(catalogue_factory=forbidden_factory), env=STORE_ENV)
    status, headers, body = client.request("GET", "/catalogue", headers={"origin": "https://attacker.invalid"})

    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers
    assert json.loads(body) == {"error": "origin not allowed"}


def test_cors_preflight_is_route_and_header_specific():
    client = TestClient(create_app(), env=STORE_ENV)
    status, headers, _body = client.request(
        "OPTIONS",
        "/checkout",
        headers={
            "origin": STOREFRONT_ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )

    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN
    assert headers["Access-Control-Allow-Methods"] == "POST"
    assert headers["Access-Control-Allow-Headers"] == "Content-Type"

    status, headers, _body = client.request(
        "OPTIONS",
        "/checkout",
        headers={
            "origin": STOREFRONT_ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "authorization",
        },
    )
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers


def test_stripe_webhook_requires_a_valid_signature_and_never_applies_browser_cors():
    secret = "whsec_fixture"
    service = RecordingSettlementService()

    async def settlement_factory(_env):
        return service

    client = TestClient(
        create_app(settlement_service_factory=settlement_factory), env={"STRIPE_WEBHOOK_SECRET": secret}
    )
    payload = {
        "id": "evt_worker_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": {"id": "cs_worker_1"}},
    }
    body_text = json.dumps(payload, separators=(",", ":"))
    body = body_text.encode()
    status, headers, _body = client.request(
        "POST", "/stripe/webhook", data=body_text, headers=_webhook_headers(body, secret)
    )

    assert status == 200
    assert len(service.events) == 1
    assert "Access-Control-Allow-Origin" not in headers

    invalid_signature_headers = _webhook_headers(body, secret)
    invalid_signature_headers["stripe-signature"] = invalid_signature_headers["stripe-signature"].rsplit("=", 1)[0] + (
        "=" + "0" * 64
    )
    status, _headers, _body = client.request(
        "POST", "/stripe/webhook", data=body_text, headers=invalid_signature_headers
    )
    assert status == 400
    assert len(service.events) == 1


def test_stripe_webhook_fails_closed_on_a_settlement_inconsistency():
    secret = "whsec_fixture"
    service = RecordingSettlementService(error=SettlementInconsistency("mismatch"))

    async def settlement_factory(_env):
        return service

    client = TestClient(
        create_app(settlement_service_factory=settlement_factory), env={"STRIPE_WEBHOOK_SECRET": secret}
    )
    body_text = json.dumps(
        {
            "id": "evt_worker_2",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {"object": {"id": "cs_worker_2"}},
        },
        separators=(",", ":"),
    )
    body = body_text.encode()
    status, _headers, response_body = client.request(
        "POST", "/stripe/webhook", data=body_text, headers=_webhook_headers(body, secret)
    )

    assert status == 409
    assert json.loads(response_body) == {"error": "Stripe webhook rejected"}


def test_stripe_webhook_treats_missing_server_secret_as_unavailable():
    body_text = json.dumps(
        {
            "id": "evt_worker_3",
            "type": "checkout.session.completed",
            "livemode": False,
            "data": {"object": {"id": "cs_worker_3"}},
        },
        separators=(",", ":"),
    )
    body = body_text.encode()
    status, _headers, response_body = TestClient(create_app()).request(
        "POST", "/stripe/webhook", data=body_text, headers=_webhook_headers(body, "whsec_fixture")
    )

    assert status == 503
    assert json.loads(response_body) == {"error": "Stripe webhook unavailable"}


def test_source_identity_uses_deployed_credentials_and_is_fail_closed(monkeypatch):
    from types import SimpleNamespace

    calls = []

    class Transport:
        async def request(self, method, path, *, headers, body=None):
            calls.append((method, path))
            return 200, {"id": "acct_test123"}

    monkeypatch.setattr(worker_module, "WorkersFetchTransport", Transport)
    monkeypatch.setattr(worker_module, "COMPILED_CATALOGUE", SimpleNamespace(expected_catalogue_version="sha256:abc"))
    env = {
        "CARTRAY_ENVIRONMENT": "test",
        "CARTRAY_REVIEWS_SOURCE": "test",
        "CARTRAY_REVIEWS_IDENTITY_TOKEN": "i" * 32,
        "STRIPE_API_KEY": "sk_test_synthetic",
        "CF_VERSION_METADATA": SimpleNamespace(id="version-1"),
    }
    client = TestClient(create_app(), env=env)
    for headers in ({}, {"authorization": "Bearer wrong"}):
        assert client.request("GET", "/internal/reviews-source-identity", headers=headers)[0] == 503
    assert not calls
    status, headers, body = client.request(
        "GET", "/internal/reviews-source-identity", headers={"authorization": "Bearer " + "i" * 32}
    )
    assert status == 200 and json.loads(body) == {
        "version_id": "version-1",
        "stripe_account": "acct_test123",
        "catalogue_version": "sha256:abc",
    }
    assert calls == [("GET", "/v1/account")]
    assert headers.get("Cache-Control") == "no-store"
    for update in (
        {"CARTRAY_ENVIRONMENT": "live"},
        {"CARTRAY_REVIEWS_SOURCE": "disabled"},
        {"STRIPE_API_KEY": "sk_live_never"},
        {"CF_VERSION_METADATA": None},
    ):
        assert (
            TestClient(create_app(), env=env | update).request(
                "GET", "/internal/reviews-source-identity", headers={"authorization": "Bearer " + "i" * 32}
            )[0]
            == 503
        )


def test_runtime_reviews_identity_is_authenticated_and_does_not_call_stripe(monkeypatch):
    from types import SimpleNamespace

    class NoTransport:
        def __init__(self):
            raise AssertionError("runtime identity must not call Stripe")

    monkeypatch.setattr(worker_module, "WorkersFetchTransport", NoTransport)
    monkeypatch.setattr(worker_module, "COMPILED_CATALOGUE", SimpleNamespace(expected_catalogue_version="sha256:abc"))
    env = {
        "CARTRAY_ENVIRONMENT": "test",
        "CARTRAY_REVIEWS_SOURCE": "test",
        "CARTRAY_REVIEWS_IDENTITY_TOKEN": "i" * 32,
        "CF_VERSION_METADATA": SimpleNamespace(id="version-1"),
    }
    path = "/internal/reviews-runtime-identity"
    assert TestClient(create_app(), env=env).request("GET", path)[0] == 503
    status, headers, body = TestClient(create_app(), env=env).request(
        "GET", path, headers={"authorization": "Bearer " + "i" * 32}
    )
    assert status == 200
    assert json.loads(body) == {"version_id": "version-1", "catalogue_version": "sha256:abc"}
    assert headers.get("Cache-Control") == "no-store"
    assert (
        TestClient(create_app(), env=env | {"CF_VERSION_METADATA": None}).request(
            "GET", path, headers={"authorization": "Bearer " + "i" * 32}
        )[0]
        == 503
    )


def test_webhook_subscriber_wakeup_does_not_change_checkout_response(monkeypatch):
    from types import SimpleNamespace

    captured = []
    response = SimpleNamespace(status=200)

    async def fake_app(request, env):
        return response

    monkeypatch.setattr(worker_module, "app", fake_app)

    class Context:
        def waitUntil(self, work):
            captured.append(work)

    entry = worker_module.Default()
    entry.env = SimpleNamespace(CARTRAY_REVIEWS_PRODUCER="test", CARTRAY_ENVIRONMENT="test")
    entry.ctx = Context()
    result = asyncio.run(entry.fetch(SimpleNamespace(method="POST", url="https://store.test/stripe/webhook")))
    assert result is response and len(captured) == 1
    asyncio.run(captured.pop())
    entry.ctx = None
    assert asyncio.run(entry.fetch(SimpleNamespace(method="POST", url="https://store.test/stripe/webhook"))) is response


def test_post_purchase_webhook_wakeup_is_opt_in_and_checkout_independent(monkeypatch):
    captured = []
    response = SimpleNamespace(status=200)

    async def fake_app(request, env):
        return response

    async def failing_drain(env):
        raise RuntimeError("test subscriber failure")

    class Context:
        def waitUntil(self, work):
            captured.append(work)

    monkeypatch.setattr(worker_module, "app", fake_app)
    monkeypatch.setattr(worker_module, "_drain_post_purchase", failing_drain)
    entry = worker_module.Default()
    entry.ctx = Context()
    request = SimpleNamespace(method="POST", url="https://store.test/stripe/webhook")
    entry.env = SimpleNamespace(CARTRAY_POST_PURCHASE_MODE="disabled", CARTRAY_ENVIRONMENT="test")
    assert asyncio.run(entry.fetch(request)) is response
    assert captured == []
    entry.env = SimpleNamespace(CARTRAY_POST_PURCHASE_MODE="test", CARTRAY_ENVIRONMENT="test")
    assert asyncio.run(entry.fetch(request)) is response
    assert len(captured) == 1
    with pytest.raises(RuntimeError, match="subscriber failure"):
        asyncio.run(captured.pop())
    entry.env = SimpleNamespace(CARTRAY_POST_PURCHASE_MODE="test", CARTRAY_ENVIRONMENT="production")
    assert asyncio.run(entry.fetch(request)) is response
    assert captured == []


def test_gets_never_wake_review_or_post_purchase_work(monkeypatch):
    scheduled = []

    async def fake_app(request, env):
        return SimpleNamespace(status=200)

    class Context:
        def waitUntil(self, work):
            scheduled.append(work)

    monkeypatch.setattr(worker_module, "app", fake_app)
    entry = worker_module.Default()
    entry.ctx = Context()
    entry.env = SimpleNamespace(
        CARTRAY_POST_PURCHASE_MODE="test", CARTRAY_REVIEWS_PRODUCER="test", CARTRAY_ENVIRONMENT="test"
    )
    for path in ("/catalogue", "/checkout-status?session_id=cs_test_x", "/health", "/stripe/webhook"):
        result = asyncio.run(entry.fetch(SimpleNamespace(method="GET", url="https://store.test" + path)))
        assert result.status == 200
    for work in scheduled:
        work.close()
    assert scheduled == []


def test_missing_post_purchase_service_is_rejected_before_claims(monkeypatch):
    action = {
        "workflow_key": "delivery",
        "offer_keys": ["product"],
        "handler": "service",
        "config_ref": "private/delivery",
        "config_digest": "a" * 64,
    }
    env = SimpleNamespace(
        CARTRAY_POST_PURCHASE_MODE="test",
        CARTRAY_ENVIRONMENT="test",
        CARTRAY_POST_PURCHASE_CONFIG=json.dumps({"merchant": "store", "environment": "test", "actions": [action]}),
        CARTRAY_POST_PURCHASE_TOKEN="a" * 32,
    )
    monkeypatch.setattr(worker_module, "D1Dispatcher", lambda *args: pytest.fail("must not claim"))
    asyncio.run(worker_module._drain_post_purchase(env))
