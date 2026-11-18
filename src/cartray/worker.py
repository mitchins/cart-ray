from __future__ import annotations

import asyncio
import hmac
import inspect
import ipaddress
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse

from kinglet import Kinglet, Response

from cartray.catalogue_bundle import build_runtime_catalogue
from cartray.checkout import CheckoutService
from cartray.compiled_catalogue import COMPILED_CATALOGUE
from cartray.errors import (
    CheckoutInProgress,
    CheckoutValidationError,
    IdempotencyConflict,
    SettlementInconsistency,
    WebhookValidationError,
)
from cartray.models import CheckoutRequest
from cartray.post_purchase import D1Dispatcher, drain_confirmed_d1, parse_action_config
from cartray.reconciliation import StripeReconciliationService
from cartray.settlement import StripeSettlementService
from cartray.store import D1OrderStore
from cartray.stripe import (
    CheckoutMetadataSealer,
    CheckoutMetadataVerifier,
    ProjectionSealError,
    StripeApiClient,
    StripeCheckoutGateway,
    StripeCheckoutSessionRetriever,
    StripePriceResolver,
)
from cartray.webhook import MAX_WEBHOOK_BODY_BYTES, StripeWebhookEvent, StripeWebhookSignatureVerifier
from cartray.workers_crypto import WorkersEd25519Signer, WorkersEd25519Verifier
from cartray.workers_transport import WorkersFetchTransport

MAX_CHECKOUT_BODY_BYTES = 16_384
_BROWSER_ROUTES = {"/catalogue": "GET", "/checkout": "POST", "/checkout-status": "GET"}
_CHECKOUT_SESSION_ID_RE = re.compile(r"^cs_[A-Za-z0-9_]+$")
_STATUS_UNAVAILABLE = "checkout status unavailable"


class CorsRejected(Exception):
    """A cross-origin browser request did not match the configured storefront."""


@dataclass(frozen=True)
class CorsPolicy:
    origin: str | None
    method: str

    def response_headers(self, *, allowed_headers: str | None = "Content-Type") -> dict[str, str]:
        if self.origin is None:
            return {}
        headers = {
            "Access-Control-Allow-Origin": self.origin,
            "Access-Control-Allow-Methods": self.method,
            "Vary": "Origin",
        }
        if allowed_headers is not None:
            headers["Access-Control-Allow-Headers"] = allowed_headers
        return headers


def create_app(
    service_factory=None, catalogue_factory=None, settlement_service_factory=None, status_store_factory=None
):
    app = Kinglet(auto_wrap_exceptions=False)
    service_factory = service_factory or checkout_service_from_environment
    catalogue_factory = catalogue_factory or public_catalogue_from_environment
    settlement_service_factory = settlement_service_factory or settlement_service_from_environment
    status_store_factory = status_store_factory or status_store_from_environment

    @app.get("/health")
    async def health(request):
        # Public liveness is deliberately independent of D1 readiness.
        return {"service": "cartray", "mode": "test-only", "status": "ok"}

    @app.get("/internal/reviews-runtime-identity")
    async def reviews_runtime_identity(request):
        """Identify the running source version without a control-plane or Stripe call."""
        try:
            env = request.env
            if _test_environment(env) != "test" or getattr(env, "CARTRAY_REVIEWS_SOURCE", None) != "test":
                raise RuntimeError("disabled")
            token = _required_env(env, "CARTRAY_REVIEWS_IDENTITY_TOKEN")
            if len(token) < 32 or not hmac.compare_digest(request.header("authorization") or "", f"Bearer {token}"):
                raise RuntimeError("unauthorized")
            version = getattr(getattr(env, "CF_VERSION_METADATA", None), "id", None)
            catalogue = COMPILED_CATALOGUE.expected_catalogue_version
            if not isinstance(version, str) or not version or not isinstance(catalogue, str) or not catalogue:
                raise RuntimeError("unbound version")
            return Response(
                {"version_id": version, "catalogue_version": catalogue}, headers={"Cache-Control": "no-store"}
            )
        except Exception:
            return Response({"error": "source identity unavailable"}, status=503, headers={"Cache-Control": "no-store"})

    @app.get("/internal/reviews-source-identity")
    async def reviews_source_identity(request):
        # Disabled unless explicitly provisioned. This endpoint never returns
        # credentials or purchaser data and is not part of checkout.
        try:
            env = request.env
            if _test_environment(env) != "test" or getattr(env, "CARTRAY_REVIEWS_SOURCE", None) != "test":
                raise RuntimeError("disabled")
            token = _required_env(env, "CARTRAY_REVIEWS_IDENTITY_TOKEN")
            provided = request.header("authorization") or ""
            if len(token) < 32 or not hmac.compare_digest(provided, f"Bearer {token}"):
                raise RuntimeError("unauthorized")
            version = getattr(getattr(env, "CF_VERSION_METADATA", None), "id", None)
            catalogue = COMPILED_CATALOGUE.expected_catalogue_version
            if not isinstance(version, str) or not version or not isinstance(catalogue, str) or not catalogue:
                raise RuntimeError("unbound version")
            client = StripeApiClient(_required_env(env, "STRIPE_API_KEY"), WorkersFetchTransport())
            account = await client.get("/v1/account", {})
            account_id = account.get("id")
            if not isinstance(account_id, str) or not re.fullmatch(r"acct_[A-Za-z0-9]+", account_id):
                raise RuntimeError("unavailable")
            return Response(
                {"version_id": version, "stripe_account": account_id, "catalogue_version": catalogue},
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            return Response({"error": "source identity unavailable"}, status=503, headers={"Cache-Control": "no-store"})

    @app.get("/catalogue")
    async def catalogue(request):
        try:
            cors = _cors_policy(request.env, request, method="GET")
        except CorsRejected:
            return Response({"error": "origin not allowed"}, status=403)
        except RuntimeError:
            return Response({"error": "catalogue unavailable"}, status=503)
        try:
            resolved_catalogue = catalogue_factory(request.env)
            if inspect.isawaitable(resolved_catalogue):
                resolved_catalogue = await resolved_catalogue
        except Exception:
            return Response({"error": "catalogue unavailable"}, status=503, headers=cors.response_headers())
        return Response(
            resolved_catalogue.public_manifest(), headers={**cors.response_headers(), "Cache-Control": "no-store"}
        )

    @app.post("/checkout")
    async def checkout(request):
        try:
            cors = _cors_policy(request.env, request, method="POST")
        except CorsRejected:
            return Response({"error": "origin not allowed"}, status=403)
        except RuntimeError:
            return Response({"error": "checkout unavailable"}, status=503)
        try:
            checkout_allowed = await _checkout_rate_limit(request.env)
        except Exception:
            return Response({"error": "checkout unavailable"}, status=503, headers=cors.response_headers())
        if not checkout_allowed:
            return Response({"error": "checkout rate limit exceeded"}, status=429, headers=cors.response_headers())
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

    @app.get("/checkout-status")
    async def checkout_status(request):
        try:
            cors = _cors_policy(request.env, request, method="GET")
        except CorsRejected:
            return Response({"error": "origin not allowed"}, status=403)
        except RuntimeError:
            return Response({"error": _STATUS_UNAVAILABLE}, status=503)
        session_id = _checkout_status_session_id(request)
        if session_id is None:
            return Response({"error": "checkout not found"}, status=404, headers=_status_headers(cors))
        try:
            allowed = await _checkout_status_rate_limit(request)
        except Exception:
            return Response({"error": _STATUS_UNAVAILABLE}, status=503, headers=_status_headers(cors))
        if not allowed:
            return Response({"error": _STATUS_UNAVAILABLE}, status=429, headers=_status_headers(cors))
        try:
            state = await (await status_store_factory(request.env)).checkout_status(session_id)
        except Exception:
            return Response({"error": _STATUS_UNAVAILABLE}, status=503, headers=_status_headers(cors))
        if state not in {"pending", "confirmed", "expired"}:
            return Response({"error": "checkout not found"}, status=404, headers=_status_headers(cors))
        return Response({"state": state}, headers=_status_headers(cors))

    @app.post("/stripe/webhook")
    async def stripe_webhook(request):
        declared_size = request.header("content-length")
        if declared_size is None:
            return Response({"error": "Stripe webhook content length is required"}, status=400)
        if not declared_size.isdigit() or int(declared_size) > MAX_WEBHOOK_BODY_BYTES:
            return Response({"error": "Stripe webhook body is too large"}, status=400)
        raw_body = await request.bytes()
        try:
            endpoint_secret = _required_env(request.env, "STRIPE_WEBHOOK_SECRET")
        except RuntimeError:
            return Response({"error": "Stripe webhook unavailable"}, status=503)
        try:
            StripeWebhookSignatureVerifier(endpoint_secret).verify(raw_body, request.header("stripe-signature"))
            event = StripeWebhookEvent.from_raw(raw_body)
        except WebhookValidationError as error:
            return Response({"error": str(error)}, status=400)
        try:
            await (await settlement_service_factory(request.env)).settle(event)
        except (SettlementInconsistency, ProjectionSealError, WebhookValidationError):
            return Response({"error": "Stripe webhook rejected"}, status=409)
        except Exception:
            return Response({"error": "Stripe webhook unavailable"}, status=503)
        return Response(status=200)

    @app.route("/catalogue", methods=["OPTIONS"])
    @app.route("/checkout", methods=["OPTIONS"])
    @app.route("/checkout-status", methods=["OPTIONS"])
    async def preflight(request):
        try:
            method = _BROWSER_ROUTES[request.path]
            cors = _cors_policy(request.env, request, method=method)
            _validate_preflight(request, method)
        except (CorsRejected, KeyError):
            return Response({"error": "origin not allowed"}, status=403)
        except RuntimeError:
            return Response({"error": "checkout unavailable"}, status=503)
        allowed_headers = None if request.path == "/checkout-status" else "Content-Type"
        return Response(status=204, headers=cors.response_headers(allowed_headers=allowed_headers))

    return app


def _stripe_client(env):
    return StripeApiClient(_required_env(env, "STRIPE_API_KEY"), WorkersFetchTransport())


async def checkout_service_from_environment(env) -> CheckoutService:
    environment = _test_environment(env)
    _require_locked_catalogue()
    success_url = _https_url(_required_env(env, "CARTRAY_SUCCESS_URL"), "CARTRAY_SUCCESS_URL")
    cancel_url = _https_url(_required_env(env, "CARTRAY_CANCEL_URL"), "CARTRAY_CANCEL_URL")
    client = _stripe_client(env)
    catalogue = await build_runtime_catalogue(COMPILED_CATALOGUE, StripePriceResolver(client))
    signer = WorkersEd25519Signer(_required_env(env, "CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64"))
    gateway = StripeCheckoutGateway(
        client,
        CheckoutMetadataSealer(environment, _required_env(env, "CARTRAY_SIGNING_KEY_ID"), signer),
    )
    return CheckoutService(
        catalogue,
        D1OrderStore(env.DB),
        gateway,
        success_url,
        cancel_url,
        review_invitations_enabled=_automatic_invitation_enabled(env),
        review_invitation_default_selected=getattr(env, "CARTRAY_REVIEW_INVITATION_DEFAULT", None) == "checked",
    )


def _automatic_invitation_enabled(env) -> bool:
    return getattr(env, "CARTRAY_REVIEW_INVITATION_MODE", None) == "automatic"


async def catalogue_from_environment(env):
    _test_environment(env)
    client = _stripe_client(env)
    return await build_runtime_catalogue(COMPILED_CATALOGUE, StripePriceResolver(client))


def public_catalogue_from_environment(env):
    _test_environment(env)
    _require_locked_catalogue()
    return COMPILED_CATALOGUE


def _require_locked_catalogue() -> None:
    if not COMPILED_CATALOGUE.expected_catalogue_version:
        raise RuntimeError("connected commerce requires a reviewed Stripe preflight catalogue lock")


async def settlement_service_from_environment(env) -> StripeSettlementService:
    environment = _test_environment(env)
    client = _stripe_client(env)
    return StripeSettlementService(
        D1OrderStore(env.DB),
        StripeCheckoutSessionRetriever(client),
        CheckoutMetadataVerifier(environment, _projection_verifiers(env)),
    )


async def reconciliation_service_from_environment(env) -> StripeReconciliationService:
    environment = _test_environment(env)
    client = _stripe_client(env)
    return StripeReconciliationService(
        D1OrderStore(env.DB),
        StripeCheckoutSessionRetriever(client),
        CheckoutMetadataVerifier(environment, _projection_verifiers(env)),
    )


async def scheduled_reconciliation_from_environment(env) -> None:
    result = await (await reconciliation_service_from_environment(env)).reconcile()
    if result.failures:
        raise RuntimeError("CartRay scheduled reconciliation has failed candidates")


async def status_store_from_environment(env) -> D1OrderStore:
    _test_environment(env)
    return D1OrderStore(env.DB)


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


def _projection_verifiers(env) -> dict[str, WorkersEd25519Verifier]:
    raw_keyring = _required_env(env, "CARTRAY_PROJECTION_PUBLIC_KEYS_JSON")
    try:
        keyring = json.loads(raw_keyring)
    except json.JSONDecodeError as error:
        raise RuntimeError("CARTRAY_PROJECTION_PUBLIC_KEYS_JSON must be a JSON object") from error
    if not isinstance(keyring, dict) or not keyring:
        raise RuntimeError("CARTRAY_PROJECTION_PUBLIC_KEYS_JSON must be a non-empty JSON object")
    if any(
        not isinstance(key_id, str) or not key_id or not isinstance(key, str) or not key
        for key_id, key in keyring.items()
    ):
        raise RuntimeError("CARTRAY_PROJECTION_PUBLIC_KEYS_JSON must map key IDs to public keys")
    return {key_id: WorkersEd25519Verifier(key) for key_id, key in keyring.items()}


def _https_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"{name} must be an absolute https URL")
    return value


def _d1_success(result) -> bool:
    success = getattr(result, "success", result.get("success") if isinstance(result, dict) else False)
    return bool(success)


async def _checkout_rate_limit(env) -> bool:
    limiter = getattr(env, "CHECKOUT_RATE_LIMITER", None)
    if limiter is None:
        raise RuntimeError("missing checkout rate-limit binding")
    result = await limiter.limit({"key": "cartray-test:checkout"})
    success = result.get("success") if isinstance(result, dict) else getattr(result, "success", None)
    return success is True


def _checkout_status_session_id(request) -> str | None:
    if len(request.query_string) > 256:
        return None
    try:
        pairs = parse_qsl(request.query_string, keep_blank_values=True, max_num_fields=2)
    except ValueError:
        return None
    values = [value for key, value in pairs if key == "session_id"]
    if len(values) != 1 or not _CHECKOUT_SESSION_ID_RE.fullmatch(values[0]):
        return None
    return values[0]


async def _checkout_status_rate_limit(request) -> bool:
    limiter = getattr(request.env, "CHECKOUT_STATUS_RATE_LIMITER", None)
    if limiter is None:
        raise RuntimeError("missing checkout status limiter")
    address = ipaddress.ip_address(request.header("cf-connecting-ip") or "")
    if address.version == 6:
        address = address.ipv4_mapped or ipaddress.ip_network(f"{address}/64", strict=False).network_address
    result = await limiter.limit({"key": "checkout-status:" + str(address)})
    success = result.get("success") if isinstance(result, dict) else getattr(result, "success", None)
    return success is True


def _status_headers(cors: CorsPolicy) -> dict[str, str]:
    return {**cors.response_headers(allowed_headers=None), "Cache-Control": "no-store"}


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
    allowed_headers = set() if request.path == "/checkout-status" else {"content-type"}
    if headers - allowed_headers:
        raise CorsRejected


app = create_app()


try:
    from workers import WorkerEntrypoint
except ImportError:

    class WorkerEntrypoint:  # pragma: no cover - local test shim
        pass


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        response = await app(request, self.env)
        # A committed confirmation can wake the independent subscriber, but a
        # missing or broken subscriber must not change the webhook response.
        try:
            if (
                getattr(self.env, "CARTRAY_REVIEWS_PRODUCER", "disabled") == "test"
                and getattr(self.env, "CARTRAY_ENVIRONMENT", "") == "test"
                and request.method == "POST"
                and urlparse(str(request.url)).path == "/stripe/webhook"
                and response.status == 200
            ):
                self.ctx.waitUntil(_wake_reviews_subscriber(self.env))
        except Exception:
            pass
        try:
            if (
                getattr(self.env, "CARTRAY_POST_PURCHASE_MODE", "disabled") == "test"
                and getattr(self.env, "CARTRAY_ENVIRONMENT", "") == "test"
                and request.method == "POST"
                and urlparse(str(request.url)).path == "/stripe/webhook"
                and response.status == 200
            ):
                self.ctx.waitUntil(_drain_post_purchase(self.env))
        except Exception:
            pass
        return response

    async def scheduled(self, controller, env, ctx):
        try:
            await scheduled_reconciliation_from_environment(self.env)
        finally:
            try:
                if getattr(self.env, "CARTRAY_REVIEWS_PRODUCER", "disabled") == "test":
                    ctx.waitUntil(_wake_reviews_subscriber(self.env))
            except Exception:
                pass
            try:
                if (
                    getattr(self.env, "CARTRAY_POST_PURCHASE_MODE", "disabled") == "test"
                    and getattr(self.env, "CARTRAY_ENVIRONMENT", "") == "test"
                ):
                    ctx.waitUntil(_drain_post_purchase(self.env))
            except Exception:
                pass


def _internal_post(url, token, body=None):
    from js import Request
    from pyodide.ffi import to_js

    options = {"method": "POST", "headers": {"authorization": f"Bearer {token}"}}
    if body is not None:
        options["headers"]["content-type"] = "application/json"
        options["body"] = body
    return Request.new(url, to_js(options, dict_converter=__import__("js").Object.fromEntries))


async def _drain_post_purchase(env):
    """Test-only best-effort drain; absent checkpoint or handler fails closed."""
    try:
        if getattr(env, "CARTRAY_POST_PURCHASE_MODE", "disabled") != "test" or _test_environment(env) != "test":
            return
        merchant, environment, actions = parse_action_config(_required_env(env, "CARTRAY_POST_PURCHASE_CONFIG"))
        token = _required_env(env, "CARTRAY_POST_PURCHASE_TOKEN")
        service = getattr(env, "POST_PURCHASE_HANDLER", None)
        if len(token) < 32 or not callable(getattr(service, "fetch", None)):
            return

        async def service_handler(purchase, execution):
            body = json.dumps(
                {
                    "merchant": execution.merchant,
                    "environment": execution.environment,
                    "workflow_key": execution.workflow_key,
                    "config_ref": execution.config_ref,
                    "config_digest": execution.config_digest,
                    "order_id": purchase.order_id,
                    "session_id": purchase.session_id,
                    "items": purchase.items,
                },
                separators=(",", ":"),
            )
            request = _internal_post("https://post-purchase-handler.internal/", token, body)
            response = await asyncio.wait_for(service.fetch(request), timeout=8)
            if response.status != 200:
                raise RuntimeError("post-purchase handler outcome uncertain")
            result = await asyncio.wait_for(response.json(), timeout=2)
            if hasattr(result, "to_py"):
                result = result.to_py()
            if not isinstance(result, dict):
                raise RuntimeError("post-purchase handler outcome uncertain")
            return result.get("provider_reference")

        dispatcher = D1Dispatcher(env.DB, merchant, environment, {"service": service_handler})
        await asyncio.wait_for(
            drain_confirmed_d1(dispatcher, actions, limit=10, max_new_claims=2, deadline=time.monotonic() + 21),
            timeout=22,
        )
    except Exception as error:
        # Checkout is already committed; cron retries the durable checkpoint.
        print(f"Post-purchase drain failed: {type(error).__name__}")
        return


async def _wake_reviews_subscriber(env):
    """Best-effort wakeup; durable source cursor and queue own retry semantics."""
    if getattr(env, "CARTRAY_REVIEWS_PRODUCER", "disabled") != "test":
        return
    try:
        token = _required_env(env, "CARTRAY_REVIEWS_TRIGGER_TOKEN")
        if _test_environment(env) != "test" or len(token) < 32:
            return
        request = _internal_post("https://reviews-producer.internal/", token)
        await env.REVIEWS_SOURCE_TRIGGER.fetch(request)
    except Exception:
        # Never fail an already committed settlement because a subscriber is down.
        return
