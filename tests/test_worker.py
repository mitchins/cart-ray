import asyncio
import hmac
import json
import time
import tomllib
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

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

    async def checkout_status(self, session_id):
        self.calls += 1
        return self.states.get(session_id)


STORE_ENV = {"CARTRAY_STOREFRONT_ORIGIN": STOREFRONT_ORIGIN, "CHECKOUT_RATE_LIMITER": FixtureRateLimiter(True)}


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
        "POST", "/checkout", json_data=payload, headers={"origin": STOREFRONT_ORIGIN}
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
        "POST", "/checkout", json_data={**payload, "amount_minor": 1}, headers={"origin": STOREFRONT_ORIGIN}
    )
    assert status == 400
    assert service.redirects == 1

    status, _headers, _body = client.request(
        "POST", "/checkout", data="{}", headers={"content-type": "text/plain", "origin": STOREFRONT_ORIGIN}
    )
    assert status == 400

    status, _headers, _body = client.request(
        "POST",
        "/checkout",
        json_data={**payload, "items": [{"product_key": "TEST-TEMPLATE", "quantity": 1, "amount": 1}]},
        headers={"origin": STOREFRONT_ORIGIN},
    )
    assert status == 400

    status, _headers, _body = client.request(
        "POST",
        "/checkout",
        data="x" * 16_385,
        headers={"content-type": "application/json", "origin": STOREFRONT_ORIGIN},
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
        headers={"origin": STOREFRONT_ORIGIN},
    )

    assert status == 400
    assert json.loads(body) == {"error": "catalogue refresh required"}
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN


def test_checkout_rate_limit_rejects_before_invoking_the_checkout_service(fixture_catalogue):
    service = RecordingCheckoutService()
    limiter = FixtureRateLimiter(False)

    async def factory(_env):
        return service

    client = TestClient(create_app(factory), env={**STORE_ENV, "CHECKOUT_RATE_LIMITER": limiter})
    status, headers, body = client.request(
        "POST",
        "/checkout",
        json_data={
            "checkout_request_id": "worker-rate-limited",
            "manifest_version": fixture_catalogue.version,
            "items": [{"product_key": "TEST-TEMPLATE", "quantity": 1}],
        },
        headers={"origin": STOREFRONT_ORIGIN},
    )

    assert status == 429
    assert json.loads(body) == {"error": "checkout rate limit exceeded"}
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN
    assert limiter.calls == 1
    assert service.redirects == 0


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
        headers={"origin": STOREFRONT_ORIGIN},
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
        "amount_minor",
        "currency",
        "max_quantity",
    }
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
        "GET", "/checkout-status?session_id=cs_test_pending", headers={"origin": STOREFRONT_ORIGIN}
    )
    assert status == 200
    assert json.loads(body) == {"state": "pending"}
    assert headers["Access-Control-Allow-Origin"] == STOREFRONT_ORIGIN
    assert headers["Cache-Control"] == "no-store"

    status, _headers, body = client.request(
        "GET", "/checkout-status?session_id=cs_test_confirmed", headers={"origin": STOREFRONT_ORIGIN}
    )
    assert status == 200
    assert json.loads(body) == {"state": "confirmed"}
    assert store.calls == 2

    status, _headers, body = client.request(
        "GET", "/checkout-status?session_id=cs_test_expired", headers={"origin": STOREFRONT_ORIGIN}
    )
    assert status == 200
    assert json.loads(body) == {"state": "expired"}
    assert store.calls == 3

    status, headers, body = client.request(
        "GET", "/checkout-status?session_id=not-a-session", headers={"origin": STOREFRONT_ORIGIN}
    )
    assert status == 404
    assert json.loads(body) == {"error": "checkout not found"}
    assert headers["Cache-Control"] == "no-store"
    assert store.calls == 3

    status, _headers, body = client.request(
        "GET", "/checkout-status?session_id=cs_test_unknown", headers={"origin": STOREFRONT_ORIGIN}
    )
    assert status == 404
    assert json.loads(body) == {"error": "checkout not found"}
    assert store.calls == 4


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
