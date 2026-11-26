from __future__ import annotations

import asyncio
import hmac
import inspect
import ipaddress
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlparse

from kinglet import Kinglet, Response

from cartray import order_reviews
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
from cartray.order_access import (
    issue_order_access,
    issuer_token,
    purchased_resources,
    resolve_configured_resource,
    resource_config_from_environment,
)
from cartray.order_access_presentation import render_order_access_page
from cartray.post_purchase import OrderReceiptDispatcher, drain_confirmed_d1
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

    @app.post("/internal/order-access")
    async def issue_access(request):
        headers = {"Cache-Control": "no-store"}
        try:
            if (
                getattr(request.env, "CARTRAY_ORDER_ACCESS_MODE", "disabled") != "test"
                or _test_environment(request.env) != "test"
            ):
                raise ValueError("unavailable")
            secret = issuer_token(_required_env(request.env, "CARTRAY_ORDER_ACCESS_KEY"))
            if len(secret) < 32 or not hmac.compare_digest(
                _request_header(request, "authorization") or "", "Bearer " + secret
            ):
                raise ValueError("unavailable")
            length = _request_header(request, "content-length")
            if length is None or not length.isdigit() or int(length) > 512:
                raise ValueError("unavailable")
            data = await request.bytes()
            if len(data) != int(length):
                raise ValueError("unavailable")
            payload = json.loads(data)
            if not isinstance(payload, dict) or set(payload) != {"order_id"}:
                raise ValueError("unavailable")
            token = await issue_order_access(
                request.env.DB,
                _required_env(request.env, "CARTRAY_ORDER_ACCESS_KEY"),
                _required_env(request.env, "CARTRAY_MERCHANT_ID"),
                "test",
                payload["order_id"],
            )
            path = getattr(request.env, "CARTRAY_ORDER_ACCESS_PATH", "/order-access")
            if not isinstance(path, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*order-access", path):
                raise ValueError("unavailable")
            return Response({"path": path + "#token=" + quote(token, safe="")}, headers=headers)
        except Exception:
            return Response({"error": "order unavailable"}, status=404, headers=headers)

    @app.route("/review-unsubscribe", methods=["GET", "POST"])
    async def review_unsubscribe(request):
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; base-uri 'none'",
        }
        try:
            if _test_environment(request.env) != "test":
                raise ValueError("unavailable")
            pairs = parse_qsl(request.query_string, max_num_fields=2)
            if len(pairs) != 1 or pairs[0][0] != "token" or not re.fullmatch("[a-f0-9]{64}", pairs[0][1]):
                raise ValueError("unavailable")
            if request.method == "GET":
                return Response(
                    "<!doctype html><title>Stop review requests</title><h1>Stop review requests</h1>"
                    '<form method="post"><button>Unsubscribe</button></form>',
                    headers=headers,
                )
            if not await _client_rate_limit(request, "CHECKOUT_STATUS_RATE_LIMITER", "review-unsubscribe"):
                return Response("Please try again later.", status=429, headers=headers)
            await order_reviews.unsubscribe(
                request.env.DB, request.env.CARTRAY_MERCHANT_ID, "test", pairs[0][1], int(time.time())
            )
            return Response("You will no longer receive review requests from this store.", headers=headers)
        except Exception:
            return Response("Unsubscribe unavailable. Please contact the merchant.", status=503, headers=headers)

    @app.route("/order-access", methods=["GET", "POST"])
    async def order_access(request):
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'",
        }
        try:
            if (
                getattr(request.env, "CARTRAY_ORDER_ACCESS_MODE", "disabled") != "test"
                or _test_environment(request.env) != "test"
                or request.query_string
            ):
                raise ValueError("unavailable")
            if request.method == "GET":
                page, policy = render_order_access_page(request.env)
                return Response(page, headers={**headers, "Content-Security-Policy": policy,
                                               "Content-Type": "text/html; charset=utf-8"})
            content_type = (_request_header(request, "content-type") or "").split(";", 1)[0].lower()
            length = _request_header(request, "content-length")
            if length is None or not length.isdigit() or int(length) > 1024:
                raise ValueError("unavailable")
            raw_body = await request.bytes()
            if len(raw_body) != int(length):
                raise ValueError("unavailable")
            if content_type == "application/json":
                args = json.loads(raw_body)
                if not isinstance(args, dict) or set(args) != {"token"}:
                    raise ValueError("unavailable")
            elif content_type == "application/x-www-form-urlencoded":
                pairs = parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True, max_num_fields=2)
                args = dict(pairs)
                if len(args) != len(pairs) or set(args) != {"token", "resource"}:
                    raise ValueError("unavailable")
            else:
                raise ValueError("unavailable")
            if not isinstance(args.get("token"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", args["token"]):
                raise ValueError("unavailable")
            if not await _client_rate_limit(request, "CHECKOUT_STATUS_RATE_LIMITER", "order-access"):
                return Response({"error": "order unavailable"}, status=429, headers=headers)
            merchant = _required_env(request.env, "CARTRAY_MERCHANT_ID")
            raw = resource_config_from_environment(request.env)
            resources = json.loads(raw)
            if not isinstance(resources, dict):
                raise ValueError("unavailable")
            items = await purchased_resources(request.env.DB, args["token"], merchant, "test")
            if "resource" in args:
                match = re.fullmatch(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})", args["resource"])
                if not match:
                    raise ValueError("unavailable")
                item_index, resource_index = int(match[1]), int(match[2])
                if item_index >= len(items) or resource_index >= len(items[item_index]["resources"]):
                    raise ValueError("unavailable")
                resource_id = items[item_index]["resources"][resource_index]
                result = await resolve_configured_resource(resource_id, resources, request.env)
                if result["status"] == "redirect":
                    return Response(status=302, headers={**headers, "Location": result["url"]})
                if result["status"] == "file":
                    filename, mime = result["filename"], result["mime_type"]
                    if (
                        not isinstance(filename, str)
                        or len(filename) > 180
                        or not filename
                        or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in filename)
                        or not isinstance(mime, str)
                        or len(mime) > 127
                        or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime)
                    ):
                        raise ValueError("unavailable")
                    return Response(
                        result["body"],
                        headers={
                            **headers,
                            "Content-Type": mime,
                            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
                        },
                    )
                message = (
                    "<!doctype html><html lang='en'><meta charset='utf-8'><title>Resource unavailable</title>"
                    "<h1>Not available yet</h1><p>Please use your browser's Back button to return "
                    "to your order and try again later.</p></html>"
                )
                return Response(message, status=404, headers={**headers, "Content-Type": "text/html; charset=utf-8"})
            names = {source.product_key: source.title for source in COMPILED_CATALOGUE.catalogue_sources}
            output = []
            for item_index, item in enumerate(items):
                entries = []
                for resource_index, resource in enumerate(item["resources"]):
                    config = resources.get(resource)
                    label = config.get("filename", "Resource") if isinstance(config, dict) else "Resource"
                    if not isinstance(label, str) or len(label) > 180:
                        label = "Resource"
                    available = isinstance(config, dict)
                    entries.append(
                        {
                            "index": f"{item_index}.{resource_index}",
                            "name": label if available else "Resource — not available yet",
                            "available": available,
                        }
                    )
                output.append(
                    {
                        "title": names.get(item["product_key"], item["product_key"]),
                        "quantity": item["quantity"],
                        "resources": entries,
                    }
                )
            reviews = None
            try:
                reviews = await asyncio.wait_for(
                    _order_access_review_links(request.env, merchant, args['token'], items), timeout=4
                )
            except Exception:
                pass
            return Response({"items": output, **({"reviews": reviews} if reviews else {})}, headers=headers)
        except Exception:
            return Response({"error": "order unavailable"}, status=404, headers=headers)

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
            checkout_allowed = await _checkout_rate_limit(request)
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
            store = await status_store_factory(request.env)
            state = await store.checkout_status(session_id)
            receipt = await store.checkout_receipt(session_id) if state == "confirmed" else None
        except Exception:
            return Response({"error": _STATUS_UNAVAILABLE}, status=503, headers=_status_headers(cors))
        if state not in {"pending", "confirmed", "expired"}:
            return Response({"error": "checkout not found"}, status=404, headers=_status_headers(cors))
        return Response({"state": state, **({"receipt": receipt} if receipt else {})}, headers=_status_headers(cors))

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
    try:
        store_subject = order_reviews.StoreReviewSubject.parse(getattr(env, "CARTRAY_STORE_REVIEW_SUBJECT", None))
    except (ValueError, TypeError):
        store_subject = None  # Optional Reviews configuration must not block checkout.
    return CheckoutService(
        catalogue,
        D1OrderStore(env.DB),
        gateway,
        success_url,
        cancel_url,
        review_invitations_enabled=_automatic_invitation_enabled(env),
        review_invitation_default_selected=getattr(env, "CARTRAY_REVIEW_INVITATION_DEFAULT", None) == "checked",
        store_review_subject=store_subject.subject_id if store_subject else None,
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
    if declared_size is None or not declared_size.isdigit() or int(declared_size) > MAX_CHECKOUT_BODY_BYTES:
        raise CheckoutValidationError("checkout request body is too large")
    try:
        body = await request.bytes()
    except Exception as error:
        raise CheckoutValidationError("checkout request body could not be read") from error
    if len(body) > MAX_CHECKOUT_BODY_BYTES or len(body) != int(declared_size):
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


async def _checkout_rate_limit(request) -> bool:
    if not await _client_rate_limit(request, "CHECKOUT_RATE_LIMITER", "checkout"):
        return False
    limiter = getattr(request.env, "CHECKOUT_STORE_RATE_LIMITER", None)
    if limiter is None:
        raise RuntimeError("missing checkout store rate-limit binding")
    installation = _required_env(request.env, "CARTRAY_SIGNING_KEY_ID")
    result = await limiter.limit({"key": "checkout-store:" + installation})
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
    return await _client_rate_limit(request, "CHECKOUT_STATUS_RATE_LIMITER", "checkout-status")


def _request_header(request, name: str) -> str | None:
    value = request.header(name)
    if value:
        return str(value)
    raw_headers = getattr(getattr(request, "_raw", None), "headers", None)
    getter = getattr(raw_headers, "get", None)
    value = getter(name) if callable(getter) else None
    return str(value) if value else None


async def _client_rate_limit(request, binding: str, scope: str) -> bool:
    limiter = getattr(request.env, binding, None)
    if limiter is None:
        raise RuntimeError("missing checkout status limiter")
    address = ipaddress.ip_address(_request_header(request, "cf-connecting-ip") or "")
    if address.version == 6:
        address = address.ipv4_mapped or ipaddress.ip_network(f"{address}/64", strict=False).network_address
    installation = _required_env(request.env, "CARTRAY_SIGNING_KEY_ID")
    result = await limiter.limit({"key": scope + ":" + installation + ":" + str(address)})
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
            if str(getattr(controller, "cron", "")) != "* * * * *":
                await scheduled_reconciliation_from_environment(self.env)
        finally:
            try:
                if (
                    getattr(self.env, "CARTRAY_POST_PURCHASE_MODE", "disabled") == "test"
                    and getattr(self.env, "CARTRAY_ENVIRONMENT", "") == "test"
                ):
                    await _drain_post_purchase(
                        self.env, priority=('recovery', 'followup', 'receipt')[int(time.time()) // 60 % 3]
                    )
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


async def _drain_post_purchase(env, *, priority="receipt"):
    try:
        await asyncio.wait_for(_drain_order_notifications(env, priority=priority), timeout=22)
    except TimeoutError:
        print("Order notification invocation reached its time budget")


async def _drain_order_notifications(env, *, priority="receipt"):
    """Test-only best-effort drain; absent checkpoint or handler fails closed."""
    try:
        if getattr(env, "CARTRAY_POST_PURCHASE_MODE", "disabled") != "test" or _test_environment(env) != "test":
            return
        # A legacy workflow grant does not authorize order-wide confirmations.
        # A deliberate schema-2 handover removes this binding.
        if getattr(env, "CARTRAY_POST_PURCHASE_CONFIG", None):
            return
        merchant = _required_env(env, "CARTRAY_MERCHANT_ID")
        token = _required_env(env, "CARTRAY_POST_PURCHASE_TOKEN")
        service = getattr(env, "POST_PURCHASE_HANDLER", None)
        if len(token) < 32 or not callable(getattr(service, "fetch", None)):
            return

        async def receipt_handler(purchase):
            names = {source.product_key: source.title for source in COMPILED_CATALOGUE.catalogue_sources}

            def receipt_title(key):
                title = names.get(key, key)
                if not isinstance(title, str):
                    return key
                title = " ".join("".join(c if c.isprintable() else " " for c in title).split())
                return title[:100] or key

            access_url = None
            if getattr(env, "CARTRAY_ORDER_ACCESS_MODE", "disabled") == "test":
                try:
                    capability = await asyncio.wait_for(issue_order_access(
                        env.DB, _required_env(env, "CARTRAY_ORDER_ACCESS_KEY"), merchant, "test", purchase.order_id
                    ), timeout=2)
                    path = getattr(env, "CARTRAY_ORDER_ACCESS_PATH", "/order-access")
                    if not isinstance(path, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*order-access", path):
                        raise ValueError("invalid order path")
                    access_url = (
                        _origin(_required_env(env, "CARTRAY_STOREFRONT_ORIGIN")) + path + "#token=" + capability
                    )
                except Exception:
                    # A resource outage never prevents the payment receipt.
                    access_url = None
            envelope = {
                "type": "order_confirmation",
                "merchant": merchant,
                "environment": "test",
                "order_id": purchase.order_id,
                "session_id": purchase.session_id,
                "access_url": access_url,
                "items": [
                    {"product_key": key, "quantity": quantity, "title": receipt_title(key)}
                    for key, quantity in purchase.items
                ],
            }
            try:
                envelope["reviews"] = await asyncio.wait_for(_receipt_reviews(env, envelope), timeout=4)
            except Exception as error:
                print(f"Review inclusion unavailable: {type(error).__name__}")
            body = json.dumps(envelope, separators=(",", ":"))
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

        deadline = time.monotonic() + 21
        reviews = bool(getattr(env, 'CARTRAY_REVIEW_SUBJECT_MAPS', None))

        async def review_stage(stage):
            try:
                await stage(env, service, token, deadline=deadline)
            except Exception:
                # Review faults must not suppress an otherwise valid receipt.
                print("Order review processing deferred; inspect installation health")

        if reviews and priority == 'recovery':
            await review_stage(_drain_review_recovery)
        if reviews and priority == 'followup':
            await review_stage(_drain_review_followups)
        dispatcher = OrderReceiptDispatcher(env.DB, merchant, "test", {"receipt": receipt_handler})
        await asyncio.wait_for(
            drain_confirmed_d1(dispatcher, limit=10, max_new_claims=2, deadline=deadline),
            timeout=22,
        )
        if reviews and priority != 'followup':
            await review_stage(_drain_review_followups)
        if reviews and priority != 'recovery':
            await review_stage(_drain_review_recovery)
    except Exception as error:
        # Checkout is already committed; cron retries the durable checkpoint.
        print(f"Post-purchase drain failed: {type(error).__name__}")
        return


class MailRequestRejected(ValueError):
    """The private handler definitively refused this request before delivery."""


async def _mail_call(service, token, envelope, *, fetch_timeout=8):
    response = await asyncio.wait_for(
        service.fetch(
            _internal_post(
                "https://post-purchase-handler.internal/", token, json.dumps(envelope, separators=(",", ":"))
            )
        ),
        fetch_timeout,
    )
    if response.status == 422:
        raise MailRequestRejected("mail request rejected")
    if response.status != 200:
        raise ValueError("mail service unavailable")
    value = await asyncio.wait_for(response.json(), 2)
    return value.to_py() if hasattr(value, "to_py") else value


async def _order_access_review_links(env, merchant, token, items):
    import hashlib

    scope = await order_reviews.first(
        env.DB, "SELECT a.order_id,s.external_session_id FROM order_access a "
        "JOIN checkout_sessions s USING(order_id) WHERE a.token_digest=? AND a.merchant=? "
        "AND a.environment='test' AND a.revoked_at IS NULL AND s.settlement_state='confirmed'",
        hashlib.sha256(token.encode()).hexdigest(), merchant,
    )
    if not scope:
        return None
    store_subject = order_reviews.StoreReviewSubject.parse(getattr(env, "CARTRAY_STORE_REVIEW_SUBJECT", None))
    voluntary = bool(store_subject and await order_reviews.store_scope_disclosed(env.DB, scope['order_id']))
    message = await order_reviews.first(env.DB,
        "SELECT * FROM order_review_messages WHERE merchant_id=? AND environment='test' AND order_id=?",
        merchant, scope['order_id'])
    if not message:
        receipt = await order_reviews.first(env.DB,
            "SELECT state FROM post_purchase_executions WHERE merchant_id=? AND environment='test' "
            "AND order_id=? AND workflow_key='order-receipt' AND state IN ('accepted','uncertain')",
            merchant, scope['order_id'])
        if not receipt:
            return None  # A pre-receipt view must not freeze notification preferences.
        envelope = {'merchant':merchant, 'environment':'test', 'order_id':scope['order_id'],
                    'session_id':scope['external_session_id'], 'access_url':None,
                    'items':[{'product_key':item['product_key'],'quantity':item['quantity'],
                              'title':item['product_key']} for item in items]}
        message = await _prepare_order_reviews(env, envelope, access_only=True, voluntary=voluntary)
    return await _review_message_links(env, message, voluntary=voluntary) if message else None


async def _receipt_reviews(env, envelope):
    message = await _prepare_order_reviews(env, envelope)
    return await _review_message_links(env, message, receipt=True) if message else None


async def _prepare_order_reviews(env, envelope, *, access_only=False, voluntary=False):
    settings = order_reviews.ReviewSettings.parse(getattr(env, "CARTRAY_REVIEW_SETTINGS", "{}"))
    mapping = json.loads(getattr(env, "CARTRAY_REVIEW_SUBJECT_MAPS", "{}"))
    store_subject = order_reviews.StoreReviewSubject.parse(getattr(env, "CARTRAY_STORE_REVIEW_SUBJECT", None))
    if (settings.timing == "off" and not voluntary) or (not mapping and not store_subject):
        return None
    if access_only:
        # Restore access only: do not arm another email or change the receipt.
        from dataclasses import replace
        settings = replace(settings, timing='receipt')
    evidence = await _mail_call(
        env.POST_PURCHASE_HANDLER, env.CARTRAY_POST_PURCHASE_TOKEN, {**envelope, "type": "recipient_evidence"}
    )
    return await order_reviews.prepare(
        env.DB,
        merchant=envelope["merchant"],
        environment="test",
        order=envelope["order_id"],
        email=evidence["email"],
        hash_secret=_required_env(env, "CARTRAY_RECIPIENT_HASH_KEY"),
        key_version=getattr(env, "CARTRAY_RECIPIENT_HASH_KEY_VERSION", "key1"),
        installation=_required_env(env, "CARTRAY_INSTALLATION_ID"),
        subject_map=mapping,
        store_subject=store_subject,
        voluntary=voluntary,
        settings=settings,
        now=int(time.time()),
    )


async def _review_message_links(env, message, *, receipt=False, voluntary=False):
    if not voluntary and not await order_reviews.permitted(
        env.DB, message["merchant_id"], "test", message["order_id"], message["recipient_hash"]
    ):
        return None
    invitation = await order_reviews.first(
        env.DB,
        "SELECT token_hash FROM acquisition_invitations "
        "WHERE store_key=? AND environment='test' AND acquisition_id=? AND revoked_at IS NULL",
        message["merchant_id"],
        message["order_id"],
    )
    import hashlib

    if not invitation or invitation["token_hash"] != hashlib.sha256(message["invitation_token"].encode()).hexdigest():
        return None
    settings = order_reviews.ReviewSettings.parse(message["settings_json"])
    if receipt and settings.timing not in ("receipt", "both"):
        return None
    column = "receipt_selection_json" if receipt else "selection_json"
    selection = message[column] if receipt else None
    if selection is None:
        selected = settings.select(
            await order_reviews.outstanding(env.DB, message["merchant_id"], "test", message["order_id"])
        )
        selection = json.dumps(selected)
        if receipt:
            await (
                env.DB.prepare(
                    f"UPDATE order_review_messages SET {column}=? WHERE merchant_id=? "
                    f"AND environment='test' AND order_id=? AND {column} IS NULL"
                )
                .bind(json.dumps(selected), message["merchant_id"], message["order_id"])
                .run()
            )
            saved = await order_reviews.first(
                env.DB,
                f"SELECT {column} FROM order_review_messages WHERE merchant_id=? AND environment='test' AND order_id=?",
                message["merchant_id"],
                message["order_id"],
            )
            selection = saved[column]
    subjects = json.loads(selection)
    if not subjects and not voluntary:
        return None
    origin = _origin(_required_env(env, "CARTRAY_STOREFRONT_ORIGIN"))
    return {
        "url": origin
        + "/review/#invitation="
        + message["invitation_token"]
        + "&featured="
        + quote(",".join(subjects), safe=""),
        "unsubscribe_url": origin
        + getattr(env, "CARTRAY_SOURCE_API_PATH", "")
        + "/review-unsubscribe?token="
        + message["unsubscribe_token"],
        "subjects": subjects,
        "names": await _review_subject_names(env.DB, message["merchant_id"], subjects),
    }


async def _review_subject_names(db, merchant, subjects):
    from cartray.store import _results

    rows = _results(
        await db.prepare(
            "SELECT subject_id,public_name FROM review_subject_display_names WHERE store_key=? AND environment='test' "
            "AND subject_id IN (SELECT value FROM json_each(?))"
        )
        .bind(merchant, json.dumps(subjects))
        .all()
    )
    return {row["subject_id"]: row["public_name"] for row in rows}


async def _drain_review_followups(env, service, token, *, deadline):
    # Same cron and provider as the receipt. No separate queue or activation.
    from cartray.store import _results

    merchant = env.CARTRAY_MERCHANT_ID
    rows = _results(
        await env.DB.prepare(
            "SELECT * FROM order_review_messages WHERE merchant_id=? "
            "AND environment='test' AND state='pending' AND due_at<=? ORDER BY due_at LIMIT 2"
        )
        .bind(merchant, int(time.time()))
        .all()
    )
    for message in rows:
        if time.monotonic() + 12 >= deadline:
            return
        links = None
        settings = order_reviews.ReviewSettings.parse(getattr(env, "CARTRAY_REVIEW_SETTINGS", "{}"))
        if settings.timing in ("both", "followup") and await order_reviews.permitted(
            env.DB, merchant, "test", message["order_id"], message["recipient_hash"]
        ):
            links = await _review_message_links(env, message)
        if not links:
            await (
                env.DB.prepare(
                    "UPDATE order_review_messages SET state='skipped' WHERE merchant_id=? "
                    "AND environment='test' AND order_id=? AND state='pending' AND invitation_token=?"
                )
                .bind(merchant, message["order_id"], message["invitation_token"])
                .run()
            )
            continue
        if not await order_reviews.claim_followup(env.DB, message, links["subjects"]):
            continue
        state, reference = "uncertain", None
        try:
            purchase = await order_reviews.first(
                env.DB,
                "SELECT external_session_id FROM checkout_sessions WHERE order_id=? AND settlement_state='confirmed'",
                message["order_id"],
            )
            items = _results(
                await env.DB.prepare("SELECT product_key,quantity FROM order_items WHERE order_id=?")
                .bind(message["order_id"])
                .all()
            )
            result = await _mail_call(
                service,
                token,
                {
                    "type": "review_followup",
                    "merchant": merchant,
                    "environment": "test",
                    "order_id": message["order_id"],
                    "session_id": purchase["external_session_id"],
                    "access_url": None,
                    "items": [{**row, "title": row["product_key"]} for row in items],
                    "reviews": links,
                },
            )
            reference = result.get("provider_reference")
            if isinstance(reference, str) and 0 < len(reference) <= 256:
                state = "accepted"
        except Exception:
            pass  # Unknown provider outcomes require explicit reconciliation.
        finally:
            await (
                env.DB.prepare(
                    "UPDATE order_review_messages SET state=?,provider_reference=? WHERE merchant_id=? "
                    "AND environment='test' AND order_id=? AND state='claimed'"
                )
                .bind(state, reference, merchant, message["order_id"])
                .run()
            )


async def _fail_order_recovery(db, merchant, message):
    await (
        db.prepare(
            "UPDATE review_recovery_requests SET state='failed' WHERE store_key=? "
            "AND environment='test' AND acquisition_id=? AND request_id=? AND state='pending'"
        )
        .bind(merchant, message["order_id"], message["request_id"])
        .run()
    )


async def _defer_order_recovery(db, merchant, message):
    # Reuse the native ledger: a temporarily unusable purchase cannot monopolize
    # recovery turns, and deferral never authorizes a provider retry.
    await db.prepare(
        "INSERT INTO review_recovery_deferrals VALUES (?,'test',?,?,?) "
        "ON CONFLICT(store_key,environment,acquisition_id) DO UPDATE SET "
        "request_id=excluded.request_id,retry_after=excluded.retry_after"
    ).bind(merchant,message['order_id'],message['request_id'],int(time.time())+300).run()


async def _drain_review_recovery(env, service, token, *, deadline):
    """Customer-requested replacements reuse native generations and the shared provider."""
    import secrets

    from cartray_reviews.invitations import claim_delivery, prepare_replacement, record_delivery_outcome

    from cartray.store import _results

    merchant = env.CARTRAY_MERCHANT_ID
    rows = _results(
        await env.DB.prepare("""SELECT r.acquisition_id,r.request_id,m.* FROM review_recovery_requests r
        JOIN order_review_messages m ON m.merchant_id=r.store_key AND m.environment=r.environment
          AND m.order_id=r.acquisition_id LEFT JOIN review_recovery_deferrals d
          ON d.store_key=r.store_key AND d.environment=r.environment AND d.acquisition_id=r.acquisition_id
          AND d.request_id=r.request_id
        WHERE r.store_key=? AND r.environment='test' AND r.state='pending'
          AND (d.retry_after IS NULL OR d.retry_after<=?)
          AND NOT EXISTS(SELECT 1 FROM invitation_generations g WHERE g.store_key=r.store_key
            AND g.environment=r.environment AND g.acquisition_id=r.acquisition_id
            AND g.request_key='recovery-'||r.request_id AND g.state<>'prepared')
        ORDER BY COALESCE(d.retry_after,r.requested_at),r.requested_at LIMIT 1""")
        .bind(merchant,int(time.time()))
        .all()
    )
    for message in rows:
        if time.monotonic() + 18 >= deadline:
            return
        if not await order_reviews.permitted(env.DB, merchant, "test", message["order_id"], message["recipient_hash"]):
            await _fail_order_recovery(env.DB, merchant, message)
            continue
        purchase = await order_reviews.first(
            env.DB,
            "SELECT external_session_id FROM checkout_sessions WHERE order_id=? AND settlement_state='confirmed'",
            message["order_id"],
        )
        if not purchase or not purchase.get("external_session_id"):
            await _fail_order_recovery(env.DB, merchant, message)
            continue
        items = _results(
            await env.DB.prepare("SELECT product_key,quantity FROM order_items WHERE order_id=?")
            .bind(message["order_id"])
            .all()
        )
        envelope = {
            "type": "recipient_evidence",
            "merchant": merchant,
            "environment": "test",
            "order_id": message["order_id"],
            "session_id": purchase["external_session_id"],
            "access_url": None,
            "items": [{**item, "title": item["product_key"]} for item in items],
        }
        try:
            evidence = await _mail_call(service, token, envelope, fetch_timeout=3)
        except MailRequestRejected:
            await _fail_order_recovery(env.DB, merchant, message)
            continue
        except Exception:
            await _defer_order_recovery(env.DB, merchant, message)
            continue

        recipient = order_reviews.recipient_hash(env.CARTRAY_RECIPIENT_HASH_KEY, merchant, "test", evidence["email"])
        if not hmac.compare_digest(recipient, message["recipient_hash"]):
            await _fail_order_recovery(env.DB, merchant, message)
            continue  # A changed contact is not authority to redirect an issued capability.
        available = _results(
            await env.DB.prepare(
                "SELECT subject_id FROM acquisition_invitation_subjects "
                "WHERE store_key=? AND environment='test' AND acquisition_id=? AND consumed_at IS NULL"
            )
            .bind(merchant, message["order_id"])
            .all()
        )
        selected = [row["subject_id"] for row in available]
        if not selected:
            await _fail_order_recovery(env.DB, merchant, message)
            continue
        now = int(time.time())
        try:
            draft = await prepare_replacement(
                env.DB,
                store=merchant,
                environment="test",
                acquisition=message["order_id"],
                request_key="recovery-" + message["request_id"],
                recovery_request_id=message["request_id"],
                key_version="v1",
                secret=env.CARTRAY_REVIEW_INVITATION_KEY.encode(),
                now=now,
                random_token=lambda: secrets.token_hex(32),
            )
        except ValueError:
            await _defer_order_recovery(env.DB, merchant, message)
            continue
        if draft["state"] != "prepared":
            continue  # Accepted, dispatching and uncertain deliveries are never resent.
        # Reserve the final send, result read and durable outcome write before
        # claiming. A prepared generation can safely resume without sending.
        if time.monotonic() + 12 >= deadline:
            return
        try:
            await claim_delivery(
                env.DB,
                store=merchant,
                environment="test",
                acquisition=message["order_id"],
                generation=draft["generation_id"],
                delivery=draft["delivery_identity"],
                now=now,
            )
        except ValueError:
            continue  # Another claimant or a failed guard owns the disposition.
        # A cancelled/unacknowledged claim stays dispatching for inspection. Do
        # not overwrite another worker's outcome without a confirmed own claim.
        outcome, reference = "uncertain", None
        try:
            origin = _origin(env.CARTRAY_STOREFRONT_ORIGIN)
            result = await _mail_call(
                service,
                token,
                {
                    **envelope,
                    "type": "review_followup", "delivery_identity":draft['delivery_identity'],
                    "reviews": {
                        "url": origin + "/review/#invitation=" + draft["token"],
                        "subjects": selected,
                        "unsubscribe_url": origin
                        + getattr(env, "CARTRAY_SOURCE_API_PATH", "")
                        + "/review-unsubscribe?token="
                        + message["unsubscribe_token"],
                    },
                },
            )
            reference = result.get("provider_reference")
            if isinstance(reference, str) and 0 < len(reference) <= 256:
                outcome = "accepted"
        finally:
            outcome_time = int(time.time())
            await record_delivery_outcome(
                env.DB,
                store=merchant,
                environment="test",
                acquisition=message["order_id"],
                generation=draft["generation_id"],
                delivery=draft["delivery_identity"],
                outcome=outcome,
                now=outcome_time,
                accepted_at=outcome_time if outcome == "accepted" else None,
                lifetime=None,
                order_message_token=draft["token"] if outcome == "accepted" else None,
                order_provider_reference=reference if outcome == "accepted" else None,
                provider_email_id=(
                    reference if outcome == "accepted" and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reference) else None
                ),
            )
