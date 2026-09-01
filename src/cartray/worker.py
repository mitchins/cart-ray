from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

from kinglet import Kinglet, Response

from cartray.catalogue import build_catalogue
from cartray.checkout import CheckoutService
from cartray.errors import CheckoutInProgress, CheckoutValidationError, IdempotencyConflict
from cartray.models import CheckoutRequest
from cartray.store import D1OrderStore
from cartray.stripe import CheckoutMetadataSealer, StripeApiClient, StripeCheckoutGateway, StripePriceResolver
from cartray.test_catalogue import TEST_CATALOGUE_SOURCES, TEST_FULFILMENT_EXPANSIONS
from cartray.workers_crypto import WorkersEd25519Signer
from cartray.workers_transport import WorkersFetchTransport

MAX_CHECKOUT_BODY_BYTES = 16_384
_BROWSER_ROUTES = {"/catalogue": "GET", "/checkout": "POST"}


class CorsRejected(Exception):
    """A cross-origin browser request did not match the configured storefront."""


@dataclass(frozen=True)
class CorsPolicy:
    origin: str | None
    method: str

    def response_headers(self) -> dict[str, str]:
        if self.origin is None:
            return {}
        return {
            "Access-Control-Allow-Origin": self.origin,
            "Access-Control-Allow-Methods": self.method,
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin",
        }


def create_app(service_factory=None, catalogue_factory=None):
    app = Kinglet(auto_wrap_exceptions=False)
    service_factory = service_factory or checkout_service_from_environment
    catalogue_factory = catalogue_factory or catalogue_from_environment

    @app.get("/health")
    async def health(request):
        result = await request.env.DB.prepare("SELECT 1 AS healthy").run()
        if not _d1_success(result):
            return Response({"error": "database unavailable"}, status=503)
        return {"service": "cartray", "mode": "test-only", "status": "ok"}

    @app.get("/catalogue")
    async def catalogue(request):
        try:
            cors = _cors_policy(request.env, request, method="GET")
        except CorsRejected:
            return Response({"error": "origin not allowed"}, status=403)
        except RuntimeError:
            return Response({"error": "catalogue unavailable"}, status=503)
        try:
            resolved_catalogue = await catalogue_factory(request.env)
        except Exception:
            return Response({"error": "catalogue unavailable"}, status=503, headers=cors.response_headers())
        return Response(resolved_catalogue.public_manifest(), headers=cors.response_headers())

    @app.post("/checkout")
    async def checkout(request):
        try:
            cors = _cors_policy(request.env, request, method="POST")
        except CorsRejected:
            return Response({"error": "origin not allowed"}, status=403)
        except RuntimeError:
            return Response({"error": "checkout unavailable"}, status=503)
        try:
            request_data = await _checkout_payload(request)
        except CheckoutValidationError as error:
            return Response({"error": str(error)}, status=400, headers=cors.response_headers())
        try:
            redirect = await (await service_factory(request.env)).checkout(request_data)
        except CheckoutValidationError as error:
            return Response({"error": str(error)}, status=400, headers=cors.response_headers())
        except IdempotencyConflict:
            return Response({"error": "idempotency conflict"}, status=409, headers=cors.response_headers())
        except CheckoutInProgress:
            return Response({"error": "checkout in progress"}, status=409, headers=cors.response_headers())
        except Exception:
            return Response({"error": "checkout unavailable"}, status=503, headers=cors.response_headers())
        return Response(
            {"checkout_url": redirect.url, "session_id": redirect.session_id},
            status=201,
            headers=cors.response_headers(),
        )

    @app.route("/catalogue", methods=["OPTIONS"])
    @app.route("/checkout", methods=["OPTIONS"])
    async def preflight(request):
        try:
            method = _BROWSER_ROUTES[request.path]
            cors = _cors_policy(request.env, request, method=method)
            _validate_preflight(request, method)
        except (CorsRejected, KeyError):
            return Response({"error": "origin not allowed"}, status=403)
        except RuntimeError:
            return Response({"error": "checkout unavailable"}, status=503)
        return Response(status=204, headers=cors.response_headers())

    return app


async def checkout_service_from_environment(env) -> CheckoutService:
    environment = _test_environment(env)
    success_url = _https_url(_required_env(env, "CARTRAY_SUCCESS_URL"), "CARTRAY_SUCCESS_URL")
    cancel_url = _https_url(_required_env(env, "CARTRAY_CANCEL_URL"), "CARTRAY_CANCEL_URL")
    client = StripeApiClient(_required_env(env, "STRIPE_API_KEY"), WorkersFetchTransport())
    catalogue = await build_catalogue(TEST_CATALOGUE_SOURCES, StripePriceResolver(client), TEST_FULFILMENT_EXPANSIONS)
    signer = WorkersEd25519Signer(_required_env(env, "CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64"))
    gateway = StripeCheckoutGateway(
        client,
        CheckoutMetadataSealer(environment, _required_env(env, "CARTRAY_SIGNING_KEY_ID"), signer),
    )
    return CheckoutService(catalogue, D1OrderStore(env.DB), gateway, success_url, cancel_url)


async def catalogue_from_environment(env):
    _test_environment(env)
    client = StripeApiClient(_required_env(env, "STRIPE_API_KEY"), WorkersFetchTransport())
    return await build_catalogue(TEST_CATALOGUE_SOURCES, StripePriceResolver(client), TEST_FULFILMENT_EXPANSIONS)


async def _checkout_payload(request) -> CheckoutRequest:
    content_type = request.header("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise CheckoutValidationError("content type must be application/json")
    declared_size = request.header("content-length")
    if declared_size is not None and (not declared_size.isdigit() or int(declared_size) > MAX_CHECKOUT_BODY_BYTES):
        raise CheckoutValidationError("checkout request body is too large")
    body = await request.bytes()
    if len(body) > MAX_CHECKOUT_BODY_BYTES:
        raise CheckoutValidationError("checkout request body is too large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckoutValidationError("checkout request body is not valid JSON") from error
    return CheckoutRequest.from_payload(payload)


def _required_env(env, name: str) -> str:
    value = getattr(env, name, None)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"missing required CartRay environment binding: {name}")
    return value


def _test_environment(env) -> str:
    environment = _required_env(env, "CARTRAY_ENVIRONMENT")
    if environment != "test":
        raise RuntimeError("CartRay Worker accepts only the test environment in this milestone")
    return environment


def _https_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"{name} must be an absolute https URL")
    return value


def _d1_success(result) -> bool:
    success = getattr(result, "success", result.get("success") if isinstance(result, dict) else False)
    return bool(success)


def _cors_policy(env, request, *, method: str) -> CorsPolicy:
    configured_origin = _origin(_required_env(env, "CARTRAY_STOREFRONT_ORIGIN"))
    origin = request.header("origin")
    if origin is None:
        return CorsPolicy(None, method)
    if origin != configured_origin:
        raise CorsRejected
    return CorsPolicy(origin, method)


def _origin(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("CARTRAY_STOREFRONT_ORIGIN must be an absolute https origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_preflight(request, method: str) -> None:
    if request.header("access-control-request-method") != method:
        raise CorsRejected
    requested_headers = request.header("access-control-request-headers", "")
    headers = {header.strip().lower() for header in requested_headers.split(",") if header.strip()}
    if headers - {"content-type"}:
        raise CorsRejected


app = create_app()


try:
    from workers import WorkerEntrypoint
except ImportError:

    class WorkerEntrypoint:  # pragma: no cover - local test shim
        pass


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await app(request, self.env)
