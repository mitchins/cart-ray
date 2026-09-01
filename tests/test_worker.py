import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256

from kinglet import TestClient

from cartray.errors import CheckoutValidationError, SettlementInconsistency
from cartray.models import CheckoutRedirect
from cartray.stripe import ProjectionSealError
from cartray.worker import app, create_app

STOREFRONT_ORIGIN = "https://cartray-store-test.pages.dev"
STORE_ENV = {"CARTRAY_STOREFRONT_ORIGIN": STOREFRONT_ORIGIN}


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

    status, _headers, _body = client.request(
        "POST", "/stripe/webhook", data=body_text, headers={"stripe-signature": "t=1,v1=" + "0" * 64}
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
    status, _headers, _body = client.request(
        "POST", "/stripe/webhook", data=body_text, headers=_webhook_headers(body, secret)
    )

    assert status == 409


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
