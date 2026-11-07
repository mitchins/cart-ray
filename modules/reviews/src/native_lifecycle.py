"""Test-only native Reviews lifecycle; no external identity or anti-abuse provider."""

import hashlib
import hmac
import ipaddress
import json
import re
import time
from urllib.parse import parse_qs

from cartray_reviews.acquisition import confirmed_purchase
from cartray_reviews.automation_due import claim_due, complete_claim
from cartray_reviews.automation_intake import intake_order
from cartray_reviews.automation_recipient import evidence_status, record_recipient
from cartray_reviews.invitations import (
    claim_delivery,
    prepare_initial_delivery,
    prepare_replacement,
    record_delivery_outcome,
    record_signed_consent,
    redeem_purchase,
    revoke_purchase,
)
from cartray_reviews.management import manage_review, management_token
from cartray_reviews.recovery import accept_recovery_command
from cartray_reviews.staff_identity import JwksCache, StaffAuthenticationError, authenticate_staff
from cartray_reviews.staff_replies import mutate_reply, public_replies
from js import Object, Promise, Uint8Array, clearTimeout, crypto, setTimeout
from pyodide.ffi import create_proxy, to_js
from workers import Response

ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
STAFF_JWKS_CACHE = JwksCache()


def random_token():
    """Use Workerd's cryptographic random generator."""
    data = Uint8Array.new(32)
    crypto.getRandomValues(data)
    return bytes(data.to_py()).hex()


def rows(result):
    """Convert D1 rows without exposing database metadata."""
    return [row.to_py() if hasattr(row, "to_py") else dict(row) for row in result.results]


def fail(status, message):
    """Return a bounded error without leaking private state."""
    return Response.json({"error": message}, status=status)


async def automation_authorize(request, env, acquisition_mode, secrets, unavailable):
    """Authenticate test automation requests with an independent bearer."""
    token = str(getattr(env, "REVIEWS_AUTOMATION_INGRESS_TOKEN", ""))
    if (
        not acquisition_mode
        or str(getattr(env, "REVIEWS_AUTOMATION_INTAKE", "disabled")) != "test"
        or len(token) < 32
        or token in secrets
        or token
        in (
            str(getattr(env, "REVIEWS_CONSENT_SECRET", "")),
            str(getattr(env, "REVIEWS_DELIVERY_TOKEN", "")),
            str(getattr(env, "REVIEWS_RECIPIENT_EVIDENCE_SECRET", "")),
        )
    ):
        await discard_body(request)
        return fail(503, unavailable)
    if not hmac.compare_digest(str(request.headers.get("Authorization") or "").encode(), ("Bearer " + token).encode()):
        await discard_body(request)
        return fail(401, "unauthorized")
    return None


def unique_pairs(pairs):
    """Reject ambiguous duplicate JSON keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_value):
    """Reject non-finite JSON numbers."""
    raise ValueError("non-finite number")


async def await_promise(promise, timeout):
    """Race a JS promise against a JS timer without cancelling its Python awaiter."""
    marker = "reviews-stream-timeout"
    timer_state = {}

    def executor(resolve, _reject):
        callback = create_proxy(lambda: resolve(marker))
        timer_state["callback"] = callback
        timer_state["id"] = setTimeout(callback, max(1, int(timeout * 1000)))

    executor_proxy = create_proxy(executor)
    try:
        timer = Promise.new(executor_proxy)
    finally:
        executor_proxy.destroy()
    try:
        result = await Promise.race(to_js([promise, timer]))
        if result == marker:
            raise TimeoutError("body deadline")
        return result
    finally:
        clearTimeout(timer_state["id"])
        timer_state["callback"].destroy()


async def discard_body(request):
    """Best-effort bounded drain of a rejected body, with a total deadline."""
    reader = None
    try:
        length = request.headers.get("Content-Length")
        if length == "0" or request.body is None:
            return
        reader = request.body.getReader()
        if not length or not str(length).isdigit() or int(length) > 16384:
            await await_promise(reader.cancel(), 0.25)
            return
        received, deadline = 0, time.monotonic() + 1.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("body deadline")
            chunk = await await_promise(reader.read(), remaining)
            if chunk.done:
                return
            received += int(chunk.value.length)
            if received > 16384:
                raise ValueError("body limit")
    except Exception:
        if reader is not None:
            try:
                await await_promise(reader.cancel(), 0.25)
            except Exception:
                pass
    finally:
        if reader is not None:
            try:
                reader.releaseLock()
            except Exception:
                pass


def admin_ok(request, env):
    """Only a configured test identity can exercise privileged local routes."""
    token = str(getattr(env, "REVIEWS_TEST_ADMIN_TOKEN", ""))
    supplied = str(request.headers.get("Authorization") or "")
    return len(token) >= 32 and hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode())


async def staff_actor(request, env, permission):
    """Use one configured test-only authentication boundary without fallback."""
    if str(getattr(env, "REVIEWS_ENVIRONMENT", "")) != "test":
        return None
    mode = str(getattr(env, "REVIEWS_STAFF_MODE", "test_admin"))
    if mode == "test_admin":
        return "test_admin" if admin_ok(request, env) else None
    if mode != "cloudflare_access":
        return None
    admission = str(getattr(env, "REVIEWS_STAFF_ADMISSION", "test_unlimited"))
    if admission == "edge":
        try:
            ip = ipaddress.ip_address(str(request.headers.get("CF-Connecting-IP") or ""))
            if ip.version == 6 and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            elif ip.version == 6:
                ip = ipaddress.ip_network(f"{ip}/64", strict=False).network_address
            secret = str(getattr(env, "REVIEWS_STAFF_RATE_KEY", ""))
            store = str(getattr(env, "REVIEWS_STORE_KEY", ""))
            if len(secret) < 32 or not store:
                return None
            scope = str(getattr(env, "REVIEWS_ENVIRONMENT", ""))
            key = hmac.new(
                secret.encode(), f"staff-ingress-v1\0{store}\0{scope}\0{ip.compressed}".encode(), hashlib.sha256
            )
            limiter = getattr(env, "REVIEWS_STAFF_INGRESS", None)
            decision = await await_promise(
                limiter.limit(to_js({"key": key.hexdigest()}, dict_converter=Object.fromEntries)), 1
            )
            if not decision.success:
                return None
        except Exception:
            return None
    elif admission != "test_unlimited":
        return None
    crypto_mode = str(getattr(env, "REVIEWS_STAFF_CRYPTO", "python_test"))
    if crypto_mode not in ("python_test", "webcrypto"):
        return None
    policy_text = str(getattr(env, "REVIEWS_STAFF_POLICY", ""))
    if len(policy_text) > 16384:
        return None

    async def fetch_jwks(url):
        # A test-only pinned key fixture permits Workerd integration without a live provider.
        fixture = str(getattr(env, "REVIEWS_STAFF_TEST_JWKS", ""))
        if fixture:
            if len(fixture) > 65536:
                raise ValueError("invalid JWKS")
            return json.loads(fixture, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
        from js import AbortController, fetch

        controller = AbortController.new()
        reader = None
        try:
            options = to_js({"redirect": "error", "signal": controller.signal}, dict_converter=Object.fromEntries)
            response = await await_promise(fetch(url, options), 3)
            if int(response.status) != 200 or response.body is None:
                raise ValueError("JWKS unavailable")
            reader = response.body.getReader()
            parts, length, deadline = [], 0, time.monotonic() + 3
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("JWKS deadline")
                part = await await_promise(reader.read(), remaining)
                if part.done:
                    break
                data = bytes(part.value.to_py())
                length += len(data)
                if length > 65536:
                    raise ValueError("JWKS too large")
                parts.append(data)
            return json.loads(b"".join(parts), object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
        finally:
            controller.abort()
            if reader is not None:
                try:
                    reader.releaseLock()
                except Exception:
                    pass

    async def load_jwks(url):
        # Synthetic fixtures are not cached. Cache is explicitly enabled per Worker.
        if str(getattr(env, "REVIEWS_STAFF_TEST_JWKS", "")):
            return await fetch_jwks(url)
        raw_ttl = str(getattr(env, "REVIEWS_STAFF_JWKS_CACHE_SECONDS", "0"))
        if raw_ttl == "0":
            return await fetch_jwks(url)
        if not raw_ttl.isascii() or not raw_ttl.isdigit():
            raise StaffAuthenticationError("invalid staff cache policy")
        return await STAFF_JWKS_CACHE.get(url, fetch_jwks, int(raw_ttl))

    async def verify_webcrypto(key, signature, signed):
        """Delegate RS256 verification to Workerd WebCrypto without a fallback."""
        public = {name: key[name] for name in ("kty", "n", "e")}
        algorithm = to_js({"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"}, dict_converter=Object.fromEntries)
        imported = await await_promise(
            crypto.subtle.importKey(
                "jwk", to_js(public, dict_converter=Object.fromEntries), algorithm, False, to_js(["verify"])
            ),
            3,
        )
        signature_bytes = Uint8Array.new(to_js(list(signature)))
        signed_bytes = Uint8Array.new(to_js(list(signed)))
        return bool(
            await await_promise(crypto.subtle.verify("RSASSA-PKCS1-v1_5", imported, signature_bytes, signed_bytes), 3)
        )

    try:
        policy = json.loads(policy_text, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
        assertion = str(request.headers.get("Cf-Access-Jwt-Assertion") or "")
        identity = await authenticate_staff(
            assertion,
            issuer=str(getattr(env, "REVIEWS_STAFF_ISSUER", "")),
            audience=str(getattr(env, "REVIEWS_STAFF_AUDIENCE", "")),
            permissions=policy,
            required_permission=permission,
            now=int(time.time()),
            load_jwks=load_jwks,
            verify_signature=verify_webcrypto if crypto_mode == "webcrypto" else None,
        )
        return identity["actor_id"]
    except (StaffAuthenticationError, ValueError, TypeError, AttributeError, RecursionError):
        return None


async def pending_read(request, env, query):
    """Allow the test administrator to inspect bounded pending work."""
    if str(env.REVIEWS_ENVIRONMENT) != "test":
        return fail(503, "moderation unavailable")
    if not await staff_actor(request, env, "reviews:moderate"):
        return fail(401, "unauthorized")
    if len(query) > 512:
        return fail(400, "invalid query")
    params = parse_qs(query, keep_blank_values=True)
    if set(params) - {"after"} or any(len(values) != 1 for values in params.values()):
        return fail(400, "invalid query")
    after = params.get("after", [""])[0]
    if after and not re.fullmatch(r"[0-9a-f]{64}", after):
        return fail(400, "invalid cursor")
    result = rows(
        await env.REVIEWS_DB.prepare(
            "SELECT r.review_id,r.subject_id,r.revision_id,r.created_at,v.author,v.title,v.body,v.rating "
            "FROM native_reviews r JOIN native_revisions v ON v.store_key=r.store_key "
            "AND v.environment=r.environment AND v.revision_id=r.revision_id "
            "WHERE r.store_key=? AND r.environment=? AND r.state='pending' AND r.review_id>? "
            "ORDER BY r.review_id LIMIT 51"
        )
        .bind(str(env.REVIEWS_STORE_KEY), str(env.REVIEWS_ENVIRONMENT), after)
        .all()
    )
    page = result[:50]
    output = {"pending": page, "next_after": page[-1]["review_id"] if len(result) > 50 else None}
    while len(json.dumps(output, ensure_ascii=True).encode("ascii")) > 128_000:
        page.pop()
        if not page:
            return fail(503, "response exceeds limit")
        output["next_after"] = page[-1]["review_id"]
    return Response.json(output, headers={"Cache-Control": "no-store"})


def staff_cursor(query):
    """Parse one bounded opaque cursor without accepting duplicate parameters."""
    if len(query) > 512:
        raise ValueError("invalid query")
    params = parse_qs(query, keep_blank_values=True)
    if set(params) - {"after"} or any(len(values) != 1 for values in params.values()):
        raise ValueError("invalid query")
    after = params.get("after", [""])[0]
    if after and not re.fullmatch(r"[0-9a-f]{64}", after):
        raise ValueError("invalid cursor")
    return after


def bounded_staff_page(key, result, id_key, metadata=None):
    """Bound both row count and serialized size for authenticated staff reads."""
    page = result[:50]
    output = {**(metadata or {}), key: page, "next_after": page[-1][id_key] if len(result) > 50 else None}
    while len(json.dumps(output, ensure_ascii=True).encode("ascii")) > 128_000:
        page.pop()
        if not page:
            return fail(503, "response exceeds limit")
        output["next_after"] = page[-1][id_key]
    return Response.json(output, headers={"Cache-Control": "no-store"})


async def published_staff_read(request, env, query):
    """List published native reviews for revision-bound staff withdrawals."""
    if str(env.REVIEWS_ENVIRONMENT) != "test":
        return fail(503, "moderation unavailable")
    if not await staff_actor(request, env, "reviews:moderate"):
        return fail(401, "unauthorized")
    try:
        after = staff_cursor(query)
    except ValueError:
        return fail(400, "invalid query")
    result = rows(
        await env.REVIEWS_DB.prepare(
            "SELECT r.review_id,r.subject_id,r.revision_id,r.created_at,v.author,v.title,v.body,v.rating,"
            "n.public_name AS subject_name FROM native_reviews r JOIN native_revisions v "
            "ON v.store_key=r.store_key AND v.environment=r.environment AND v.revision_id=r.revision_id "
            "AND v.review_id=r.review_id LEFT JOIN review_subject_display_names n "
            "ON n.store_key=r.store_key AND n.environment=r.environment AND n.subject_id=r.subject_id "
            "WHERE r.store_key=? AND r.environment=? AND r.state='published' AND r.review_id>? "
            "ORDER BY r.review_id LIMIT 51"
        )
        .bind(str(env.REVIEWS_STORE_KEY), str(env.REVIEWS_ENVIRONMENT), after)
        .all()
    )
    return bounded_staff_page("published", result, "review_id")


async def delivery_queue_read(request, env, query):
    """List held delivery identities without exposing recipients or capabilities."""
    if str(env.REVIEWS_ENVIRONMENT) != "test":
        return fail(503, "operations unavailable")
    if not await staff_actor(request, env, "reviews:operations"):
        return fail(401, "unauthorized")
    if len(query) > 512:
        return fail(400, "invalid query")
    params = parse_qs(query, keep_blank_values=True)
    if set(params) - {"state", "after"} or any(len(values) != 1 for values in params.values()):
        return fail(400, "invalid query")
    state = params.get("state", [""])[0]
    after = params.get("after", [""])[0]
    if state not in ("dispatching", "uncertain", "failed") or (after and not re.fullmatch(r"[0-9a-f]{64}", after)):
        return fail(400, "invalid query")
    result = rows(
        await env.REVIEWS_DB.prepare(
            "SELECT generation_id,delivery_identity,state,prepared_at FROM invitation_generations "
            "WHERE store_key=? AND environment=? AND state=? AND delivery_identity>? "
            "ORDER BY delivery_identity LIMIT 51"
        )
        .bind(str(env.REVIEWS_STORE_KEY), str(env.REVIEWS_ENVIRONMENT), state, after)
        .all()
    )
    return bounded_staff_page("deliveries", result, "delivery_identity", {"state": state})


async def staff_history_read(request, env, query, review_id, kind):
    """Read only the selected review's revision or audit history."""
    if str(env.REVIEWS_ENVIRONMENT) != "test":
        return fail(503, "moderation unavailable")
    if not await staff_actor(request, env, "reviews:moderate"):
        return fail(401, "unauthorized")
    try:
        after = staff_cursor(query)
    except ValueError:
        return fail(400, "invalid query")
    db, store, environment = env.REVIEWS_DB, str(env.REVIEWS_STORE_KEY), str(env.REVIEWS_ENVIRONMENT)
    found = rows(
        await db.prepare(
            "SELECT review_id,subject_id,state,revision_id AS current_revision_id FROM native_reviews "
            "WHERE store_key=? AND environment=? AND review_id=?"
        )
        .bind(store, environment, review_id)
        .all()
    )
    if not found:
        return fail(404, "not found")
    metadata = found[0]
    if kind.startswith("reply-"):
        heads = rows(
            await db.prepare(
                "SELECT reply_id,reply_revision_id,parent_revision_id,state FROM native_staff_replies "
                "WHERE store_key=? AND environment=? AND review_id=?"
            )
            .bind(store, environment, review_id)
            .all()
        )
        metadata["reply"] = heads[0] if heads else None
    if kind == "reply-revisions":
        if not heads:
            return bounded_staff_page("reply_revisions", [], "reply_revision_id", metadata)
        sql = (
            "SELECT reply_revision_id,author,body,created_at FROM native_staff_reply_revisions "
            "WHERE store_key=? AND environment=? AND reply_id=? AND reply_revision_id>? "
            "ORDER BY reply_revision_id LIMIT 51"
        )
        id_key, identifier, output_key = "reply_revision_id", heads[0]["reply_id"], "reply_revisions"
    elif kind == "reply-audit":
        sql = (
            "SELECT event_id,reply_id,reply_revision_id,actor,action,occurred_at FROM native_staff_reply_audit "
            "WHERE store_key=? AND environment=? AND review_id=? AND event_id>? ORDER BY event_id LIMIT 51"
        )
        id_key, identifier, output_key = "event_id", review_id, "reply_audit"
    elif kind == "revisions":
        sql = (
            "SELECT revision_id,author,title,body,rating FROM native_revisions "
            "WHERE store_key=? AND environment=? AND review_id=? AND revision_id>? ORDER BY revision_id LIMIT 51"
        )
        id_key, identifier, output_key = "revision_id", review_id, kind
    else:
        sql = (
            "SELECT event_id,revision_id,action,actor,occurred_at FROM native_audit "
            "WHERE store_key=? AND environment=? AND review_id=? AND event_id>? ORDER BY event_id LIMIT 51"
        )
        id_key, identifier, output_key = "event_id", review_id, kind
    result = rows(await db.prepare(sql).bind(store, environment, identifier, after).all())
    return bounded_staff_page(output_key, result, id_key, metadata)


async def read_json(request):
    """Read a small JSON object; caller handles invalid data uniformly."""
    length = request.headers.get("Content-Length")
    content_type = str(request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if not length or not str(length).isdigit() or not 1 <= int(length) <= 16384 or content_type != "application/json":
        await discard_body(request)
        raise ValueError("invalid request size")
    reader = request.body.getReader()
    data = bytearray()
    deadline = time.monotonic() + 2.0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("body deadline")
            chunk = await await_promise(reader.read(), remaining)
            if chunk.done:
                break
            if len(data) + int(chunk.value.length) > 16384:
                raise ValueError("invalid request size")
            data.extend(chunk.value.to_py())
    except Exception as exc:
        try:
            await await_promise(reader.cancel(), 0.25)
        except Exception:
            pass
        raise ValueError("invalid request body") from exc
    finally:
        try:
            reader.releaseLock()
        except Exception:
            pass
    if len(data) != int(length):
        raise ValueError("invalid request size")
    try:
        value = json.loads(bytes(data), object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except RecursionError as exc:
        raise ValueError("JSON nesting too deep") from exc
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def rate(db, store, environment, action, now):
    """Atomically bound operations per store and minute across Worker instances."""
    return db.prepare(
        "INSERT INTO native_rate_limits VALUES (?,?,?,?,1) "
        "ON CONFLICT(store_key,environment,action,window) DO UPDATE SET count=count+1"
    ).bind(store, environment, action, now // 60)


async def cleanup_rates(db, store, environment, operations, now):
    """Occasionally remove bounded expired rate rows after verified requests."""
    if operations % 64:
        return
    try:
        await db.batch(
            [
                db.prepare(
                    "DELETE FROM native_rate_limits WHERE rowid IN (SELECT rowid FROM native_rate_limits "
                    "WHERE store_key=? AND environment=? AND "
                    "(action LIKE 'purchase-submit:%' OR action LIKE 'review-manage:%') "
                    "AND window<? LIMIT 128)"
                ).bind(store, environment, now // 60 - 1440),
                db.prepare(
                    "DELETE FROM acquisition_rate_backstop WHERE rowid IN "
                    "(SELECT rowid FROM acquisition_rate_backstop WHERE store_key=? AND environment=? "
                    "AND window<? LIMIT 128)"
                ).bind(store, environment, now // 60 - 1440),
            ]
        )
    except Exception:
        pass  # retention does not change the result of a verified operation


async def handle_write(request, env, path):
    """Handle test-only writes; no production credentials or identity are implied."""
    store, environment = str(env.REVIEWS_STORE_KEY), str(env.REVIEWS_ENVIRONMENT)
    if environment != "test" or (
        str(getattr(env, "REVIEWS_STAFF_MODE", "test_admin")) != "cloudflare_access"
        and len(str(getattr(env, "REVIEWS_TEST_ADMIN_TOKEN", ""))) < 32
    ):
        await discard_body(request)
        return fail(503, "collection unavailable")
    if str(request.method) != "POST":
        await discard_body(request)
        return fail(405, "method not allowed")
    acquisition_mode = str(getattr(env, "REVIEWS_MODE", "")) == "verified_acquisition_only"
    if acquisition_mode:
        secrets = (
            str(getattr(env, "REVIEWS_SETTLEMENT_TOKEN", "")),
            str(getattr(env, "REVIEWS_INVITATION_KEY", "")),
            str(getattr(env, "REVIEWS_REVIEWER_KEY", "")),
            str(getattr(env, "REVIEWS_TEST_ADMIN_TOKEN", "")),
        )
        if (
            any(len(secret) < 32 for secret in secrets)
            or len(set(secrets)) != 4
            or getattr(env, "CARTRAY_DB", None) is None
        ):
            await discard_body(request)
            return fail(503, "collection unavailable")
        if path.startswith("/reviews/v1/"):
            source = str(request.headers.get("CF-Connecting-IP") or "local-test")
            if len(source) > 64:
                await discard_body(request)
                return fail(400, "invalid request")
            source_key = hmac.new(
                str(env.REVIEWS_REVIEWER_KEY).encode(),
                json.dumps(["reviews-ingress-v1", store, environment, source], separators=(",", ":")).encode(),
                hashlib.sha256,
            ).hexdigest()
            try:
                limiter = getattr(env, "REVIEWS_INGRESS", None)
                decision = await limiter.limit(to_js({"key": source_key}, dict_converter=Object.fromEntries))
                if not decision.success:
                    await discard_body(request)
                    return fail(429, "too many requests; please try again later")
            except Exception:
                await discard_body(request)
                return fail(503, "collection unavailable")
    if path == "/reviews-internal/v1/automation-due-evaluate":
        failure = await automation_authorize(request, env, acquisition_mode, secrets, "evaluation unavailable")
        if failure is not None:
            return failure
        try:
            data = await read_json(request)
            if set(data) != {"acquisition_id", "claim_token"}:
                return fail(400, "invalid evaluation")
            now = int(time.time())
            claim = await claim_due(
                env.REVIEWS_DB,
                store=store,
                environment=environment,
                acquisition=data["acquisition_id"],
                token=data["claim_token"],
                now=now,
                lease_seconds=60,
            )
            completed = False
            if claim is not None:
                completed = await complete_claim(
                    env.REVIEWS_DB,
                    store=store,
                    environment=environment,
                    acquisition=data["acquisition_id"],
                    token=data["claim_token"],
                    generation=claim["generation"],
                    now=int(time.time()),
                )
            if not completed:
                # Another same-token invocation may have completed between claim and update.
                found = rows(
                    await env.REVIEWS_DB.prepare(
                        "SELECT generation FROM review_automation_due_claims WHERE store_key=? AND environment=? "
                        "AND acquisition_id=? AND claim_token=? AND state='completed'"
                    )
                    .bind(store, environment, data["acquisition_id"], data["claim_token"])
                    .all()
                )
                completed = bool(found)
            return Response.json(
                {"status": "held" if completed else "unavailable"}, headers={"Cache-Control": "no-store"}
            )
        except (ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
            return fail(409, "evaluation unavailable")
    if path == "/reviews-internal/v1/automation-intake":
        token = str(getattr(env, "REVIEWS_AUTOMATION_INGRESS_TOKEN", ""))
        supplied = str(request.headers.get("Authorization") or "")
        if (
            not acquisition_mode
            or str(getattr(env, "REVIEWS_AUTOMATION_INTAKE", "disabled")) != "test"
            or len(token) < 32
            or token in secrets
            or token
            in (str(getattr(env, "REVIEWS_CONSENT_SECRET", "")), str(getattr(env, "REVIEWS_DELIVERY_TOKEN", "")))
        ):
            await discard_body(request)
            return fail(503, "intake unavailable")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            await discard_body(request)
            return fail(401, "unauthorized")
        try:
            data = await read_json(request)
            if set(data) != {"outbox_id"}:
                return fail(400, "invalid intake")
            result = await intake_order(
                env.REVIEWS_DB,
                env.CARTRAY_DB,
                store=store,
                environment=environment,
                outbox_id=data["outbox_id"],
                subject_maps=json.loads(str(getattr(env, "REVIEWS_SUBJECT_MAPS", ""))),
                now=int(time.time()),
            )
            return Response.json(result, status=200, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
            return fail(409, "intake unavailable")
    if path == "/reviews-internal/v1/automation-recipient-evidence":
        recipient_secret = str(getattr(env, "REVIEWS_RECIPIENT_EVIDENCE_SECRET", ""))
        if (
            not acquisition_mode
            or str(getattr(env, "REVIEWS_AUTOMATION_INTAKE", "disabled")) != "test"
            or len(recipient_secret) < 32
            or recipient_secret in secrets
            or recipient_secret
            in (
                str(getattr(env, "REVIEWS_CONSENT_SECRET", "")),
                str(getattr(env, "REVIEWS_DELIVERY_TOKEN", "")),
                str(getattr(env, "REVIEWS_AUTOMATION_INGRESS_TOKEN", "")),
            )
        ):
            await discard_body(request)
            return fail(503, "recipient evidence unavailable")
        try:
            data = await read_json(request)
            await record_recipient(
                env.REVIEWS_DB,
                store=store,
                environment=environment,
                data=data,
                signature=str(request.headers.get("X-Reviews-Recipient-Signature") or ""),
                secret=recipient_secret.encode(),
                now=int(time.time()),
            )
            return Response.json({"status": "recorded"}, status=200, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
            return fail(409, "recipient evidence unavailable")
    if path == "/reviews-internal/v1/automation-evidence-status":
        failure = await automation_authorize(request, env, acquisition_mode, secrets, "evidence unavailable")
        if failure is not None:
            return failure
        try:
            data = await read_json(request)
            if set(data) != {"outbox_id"}:
                return fail(400, "invalid evidence query")
            result = await evidence_status(
                env.REVIEWS_DB,
                store=store,
                environment=environment,
                outbox_id=data["outbox_id"],
                now=int(time.time()),
            )
            return Response.json(result, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
            return fail(409, "evidence unavailable")
    if path == "/reviews-internal/v1/consents":
        consent_secret = str(getattr(env, "REVIEWS_CONSENT_SECRET", ""))
        if not acquisition_mode or len(consent_secret) < 32 or consent_secret in secrets:
            await discard_body(request)
            return fail(503, "consent unavailable")
        try:
            consent_data = await read_json(request)
            await record_signed_consent(
                env.REVIEWS_DB,
                env.CARTRAY_DB,
                store=store,
                environment=environment,
                data=consent_data,
                signature=str(request.headers.get("X-Reviews-Consent-Signature") or ""),
                secret=consent_secret.encode(),
                subject_maps=json.loads(str(getattr(env, "REVIEWS_SUBJECT_MAPS", ""))),
                now=int(time.time()),
            )
            return Response.json({"status": "recorded"}, status=201, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, AttributeError, KeyError):
            return fail(409, "consent unavailable")
    if path in ("/reviews-internal/v1/invitations/delivery-claim", "/reviews-internal/v1/invitations/delivery-outcome"):
        delivery_token = str(getattr(env, "REVIEWS_DELIVERY_TOKEN", ""))
        supplied = str(request.headers.get("Authorization") or "")
        if (
            not acquisition_mode
            or len(delivery_token) < 32
            or delivery_token in secrets
            or delivery_token == str(getattr(env, "REVIEWS_CONSENT_SECRET", ""))
            or not hmac.compare_digest(supplied.encode(), ("Bearer " + delivery_token).encode())
        ):
            await discard_body(request)
            return fail(401, "unauthorized")
        try:
            data = await read_json(request)
            if path.endswith("/delivery-claim"):
                if set(data) != {"acquisition_id", "generation_id", "delivery_identity"}:
                    return fail(400, "invalid delivery claim")
                result = await claim_delivery(
                    env.REVIEWS_DB,
                    store=store,
                    environment=environment,
                    acquisition=data["acquisition_id"],
                    generation=data["generation_id"],
                    delivery=data["delivery_identity"],
                    now=int(time.time()),
                )
                return Response.json(result, headers={"Cache-Control": "no-store"})
            expected = {"acquisition_id", "generation_id", "delivery_identity", "outcome"}
            if data.get("outcome") == "accepted":
                expected.add("accepted_at")
            if set(data) != expected:
                return fail(400, "invalid delivery outcome")
            lifetime = int(str(getattr(env, "REVIEWS_INVITATION_LIFETIME", "5184000")))
            result = await record_delivery_outcome(
                env.REVIEWS_DB,
                store=store,
                environment=environment,
                acquisition=data["acquisition_id"],
                generation=data["generation_id"],
                delivery=data["delivery_identity"],
                outcome=data["outcome"],
                now=int(time.time()),
                accepted_at=data.get("accepted_at"),
                lifetime=lifetime,
            )
            return Response.json(result, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, AttributeError, KeyError):
            return fail(409, "delivery unavailable")
    if path in (
        "/reviews-internal/v1/settlements",
        "/reviews-internal/v1/invitations/revoke",
        "/reviews-internal/v1/invitations/replacement",
        "/reviews-internal/v1/invitations/recovery-next",
        "/reviews-internal/v1/invitations/recovery-command",
        "/reviews-internal/v1/invitations/recovery-prepare",
        "/reviews-internal/v1/invitations/recovery-cancel",
        "/reviews-internal/v1/invitations/recovery-defer",
    ):
        issuer = str(getattr(env, "REVIEWS_SETTLEMENT_TOKEN", ""))
        supplied = str(request.headers.get("Authorization") or "")
        if (
            not acquisition_mode
            or len(issuer) < 32
            or issuer == str(env.REVIEWS_TEST_ADMIN_TOKEN)
            or not hmac.compare_digest(supplied.encode(), ("Bearer " + issuer).encode())
        ):
            await discard_body(request)
            return fail(401, "unauthorized")
        try:
            data = await read_json(request)
            if path == "/reviews-internal/v1/invitations/recovery-command":
                if set(data) != {"token_hash", "request_id", "issued_at"}:
                    return fail(400, "invalid recovery request")
                pending = await accept_recovery_command(
                    env.REVIEWS_DB,
                    store=store,
                    environment=environment,
                    token_hash=data["token_hash"],
                    request_id=data["request_id"],
                    issued_at=data["issued_at"],
                    now=int(time.time()),
                )
                return Response.json({"pending": pending}, headers={"Cache-Control": "no-store"})
            if path == "/reviews-internal/v1/invitations/recovery-next":
                if data:
                    return fail(400, "invalid request")
                pending = rows(
                    await env.REVIEWS_DB.prepare(
                        "SELECT r.acquisition_id,r.request_id,r.requested_at,i.source_event_id, "
                        "i.recipient_context_hash "
                        "FROM review_recovery_requests r JOIN acquisition_invitations i "
                        "USING(store_key,environment,acquisition_id) JOIN review_invitation_consents c "
                        "USING(store_key,environment,acquisition_id) WHERE r.store_key=? AND r.environment=? "
                        "AND r.state='pending' AND c.revoked_at IS NULL AND c.purpose='review_invitation' "
                        "AND NOT EXISTS(SELECT 1 FROM review_recovery_deferrals d WHERE d.store_key=r.store_key "
                        "AND d.environment=r.environment AND d.acquisition_id=r.acquisition_id "
                        "AND d.request_id=r.request_id "
                        "AND d.retry_after>?) "
                        "AND c.recipient_context_hash=i.recipient_context_hash "
                        "AND NOT EXISTS(SELECT 1 FROM invitation_generations g WHERE g.store_key=r.store_key "
                        "AND g.environment=r.environment AND g.acquisition_id=r.acquisition_id "
                        "AND g.state IN ('prepared','dispatching','uncertain') "
                        "AND (g.request_key<>'recovery-'||r.request_id OR g.state<>'prepared')) "
                        "ORDER BY r.requested_at,r.acquisition_id LIMIT 1"
                    )
                    .bind(store, environment, int(time.time()))
                    .all()
                )
                return Response.json(
                    {"pending": pending[0] if pending else None}, headers={"Cache-Control": "no-store"}
                )
            if path == "/reviews-internal/v1/invitations/recovery-defer":
                request_id = data.get("request_id")
                if (
                    set(data) != {"request_id"}
                    or not isinstance(request_id, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", request_id)
                ):
                    return fail(400, "invalid recovery request")
                now = int(time.time())
                await env.REVIEWS_DB.batch(
                    [
                        env.REVIEWS_DB.prepare(
                            "INSERT INTO review_recovery_deferrals "
                            "(store_key,environment,acquisition_id,request_id,retry_after) "
                            "SELECT store_key,environment,acquisition_id,request_id,? FROM review_recovery_requests "
                            "WHERE store_key=? AND environment=? AND request_id=? AND state='pending' "
                            "ON CONFLICT(store_key,environment,acquisition_id) DO UPDATE SET "
                            "request_id=excluded.request_id,retry_after=excluded.retry_after"
                        ).bind(now + 60, store, environment, request_id)
                    ]
                )
                return Response.json({"status": "deferred"}, headers={"Cache-Control": "no-store"})
            if path == "/reviews-internal/v1/invitations/recovery-cancel":
                if (
                    set(data) != {"request_id"}
                    or not isinstance(data["request_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", data["request_id"])
                ):
                    return fail(400, "invalid recovery request")
                try:
                    await env.REVIEWS_DB.batch(
                        [
                            env.REVIEWS_DB.prepare(
                                "INSERT INTO native_guard SELECT CASE WHEN EXISTS("
                                "SELECT 1 FROM review_recovery_requests r "
                                "WHERE r.store_key=? AND r.environment=? AND r.request_id=? AND r.state='pending' "
                                "AND NOT EXISTS(SELECT 1 FROM invitation_generations g WHERE g.store_key=r.store_key "
                                "AND g.environment=r.environment AND g.acquisition_id=r.acquisition_id "
                                "AND g.request_key='recovery-'||r.request_id "
                                "AND g.state IN ('dispatching','uncertain'))) "
                                "THEN 1 ELSE 0 END"
                            ).bind(store, environment, data["request_id"]),
                            env.REVIEWS_DB.prepare(
                                "UPDATE invitation_generations SET state='failed' WHERE store_key=? AND environment=? "
                                "AND request_key='recovery-'||? AND state='prepared'"
                            ).bind(store, environment, data["request_id"]),
                            env.REVIEWS_DB.prepare(
                                "UPDATE review_recovery_requests SET state='failed' "
                                "WHERE store_key=? AND environment=? "
                                "AND request_id=? AND state='pending'"
                            ).bind(store, environment, data["request_id"]),
                            env.REVIEWS_DB.prepare("DELETE FROM native_guard"),
                        ]
                    )
                except Exception:
                    return fail(409, "recovery unavailable")
                return Response.json({"status": "cancelled"}, headers={"Cache-Control": "no-store"})
            if path == "/reviews-internal/v1/invitations/recovery-prepare":
                if (
                    set(data) != {"event_id", "request_id"}
                    or not isinstance(data["request_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", data["request_id"])
                ):
                    return fail(400, "invalid recovery request")
                acquisition = await confirmed_purchase(
                    env.CARTRAY_DB,
                    store=store,
                    environment=environment,
                    event_id=data["event_id"],
                    subject_maps=json.loads(str(getattr(env, "REVIEWS_SUBJECT_MAPS", ""))),
                )
                result = await prepare_replacement(
                    env.REVIEWS_DB,
                    store=store,
                    environment=environment,
                    acquisition=acquisition.acquisition_id,
                    request_key="recovery-" + data["request_id"],
                    recovery_request_id=data["request_id"],
                    key_version=str(getattr(env, "REVIEWS_INVITATION_KEY_VERSION", "v1")),
                    secret=str(env.REVIEWS_INVITATION_KEY).encode(),
                    now=int(time.time()),
                    random_token=random_token,
                )
                return Response.json(
                    result | {"acquisition_id": acquisition.acquisition_id},
                    status=201,
                    headers={"Cache-Control": "no-store"},
                )
            if path == "/reviews-internal/v1/invitations/replacement":
                if set(data) != {"event_id", "request_key"}:
                    return fail(400, "invalid replacement")
                acquisition = await confirmed_purchase(
                    env.CARTRAY_DB,
                    store=store,
                    environment=environment,
                    event_id=data["event_id"],
                    subject_maps=json.loads(str(getattr(env, "REVIEWS_SUBJECT_MAPS", ""))),
                )
                result = await prepare_replacement(
                    env.REVIEWS_DB,
                    store=store,
                    environment=environment,
                    acquisition=acquisition.acquisition_id,
                    request_key=data["request_key"],
                    key_version=str(getattr(env, "REVIEWS_INVITATION_KEY_VERSION", "v1")),
                    secret=str(env.REVIEWS_INVITATION_KEY).encode(),
                    now=int(time.time()),
                    random_token=random_token,
                )
                return Response.json(result, status=201, headers={"Cache-Control": "no-store"})
            if path == "/reviews-internal/v1/invitations/revoke":
                if set(data) != {"invitation_token"}:
                    return fail(400, "invalid invitation")
                await revoke_purchase(
                    env.REVIEWS_DB,
                    store=store,
                    environment=environment,
                    token=data["invitation_token"],
                    now=int(time.time()),
                )
                return Response.json({"status": "revoked"}, headers={"Cache-Control": "no-store"})
            if set(data) != {"event_id", "recipient_context_hash", "request_key"}:
                return fail(400, "invalid settlement")
            token_key = str(getattr(env, "REVIEWS_INVITATION_KEY", ""))
            reviewer_secret = str(getattr(env, "REVIEWS_REVIEWER_KEY", ""))
            if (
                len(token_key) < 32
                or len(reviewer_secret) < 32
                or len({issuer, token_key, reviewer_secret, str(env.REVIEWS_TEST_ADMIN_TOKEN)}) != 4
            ):
                return fail(503, "collection unavailable")
            mappings = json.loads(str(getattr(env, "REVIEWS_SUBJECT_MAPS", "")))
            result = await prepare_initial_delivery(
                env.REVIEWS_DB,
                env.CARTRAY_DB,
                store=store,
                environment=environment,
                event_id=data["event_id"],
                subject_maps=mappings,
                recipient_context_hash=data["recipient_context_hash"],
                request_key=data["request_key"],
                key_version=str(getattr(env, "REVIEWS_INVITATION_KEY_VERSION", "v1")),
                secret=token_key.encode(),
                now=int(time.time()),
                random_token=random_token,
            )
            return Response.json(result, status=201, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, AttributeError, KeyError):
            return fail(409, "settlement unavailable")
    admin = path.startswith("/reviews-admin/")
    actor = None
    if admin:
        permission = (
            "reviews:subjects"
            if path in ("/reviews-admin/v1/subjects", "/reviews-admin/v1/subject-display-names")
            else "reviews:moderate"
        )
        actor = await staff_actor(request, env, permission)
        if not actor or (path == "/reviews-admin/v1/test-capabilities" and actor != "test_admin"):
            await discard_body(request)
            return fail(401, "unauthorized")
    db, now = env.REVIEWS_DB, int(time.time())
    rate_action = {"/reviews-admin/v1/test-capabilities": "issue", "/reviews/v1/contributions": "submit"}.get(path)
    if rate_action and not (acquisition_mode and rate_action == "submit"):
        try:
            await db.batch(
                [
                    rate(db, store, environment, rate_action, now),
                    db.prepare(
                        "DELETE FROM native_rate_limits WHERE store_key=? AND environment=? AND action=? AND window<?"
                    ).bind(store, environment, rate_action, now // 60 - 1440),
                ]
            )
        except Exception:
            await discard_body(request)
            return fail(429, "rate limit exceeded")
    try:
        data = await read_json(request)
    except (ValueError, TypeError, json.JSONDecodeError):
        return fail(400, "invalid request")
    if path == "/reviews/v1/invitation-recovery":
        if not acquisition_mode or set(data) != {"invitation_token"}:
            return fail(400, "invalid request")
        token = data["invitation_token"]
        recovery_mode = str(getattr(env, "REVIEWS_RECOVERY_MODE", "automatic"))
        queue = getattr(env, "REVIEWS_RECOVERY_QUEUE", None)
        if recovery_mode not in ("automatic", "manual_test") or (recovery_mode == "automatic" and queue is None):
            return fail(503, "recovery temporarily unavailable")
        if recovery_mode == "automatic":
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
                return Response.json({"status": "requested"}, status=202, headers={"Cache-Control": "no-store"})
            # The queued command is the durable authority: it is persisted
            # before any consumer writes the recovery request to D1.
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            try:
                await queue.send(
                    to_js(
                        {"token_hash": token_hash, "request_id": random_token(), "issued_at": int(time.time())},
                        dict_converter=Object.fromEntries,
                    )
                )
            except Exception:
                return fail(503, "recovery temporarily unavailable")
            return Response.json({"status": "requested"}, status=202, headers={"Cache-Control": "no-store"})
        if isinstance(token, str) and re.fullmatch(r"[0-9a-f]{64}", token):
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            candidates = rows(
                await db.prepare(
                    "SELECT i.acquisition_id FROM acquisition_invitations i "
                    "JOIN review_invitation_consents c USING(store_key,environment,acquisition_id) "
                    "WHERE i.store_key=? AND i.environment=? AND c.purpose='review_invitation' "
                    "AND c.recipient_context_hash=i.recipient_context_hash AND c.revoked_at IS NULL AND "
                    "((i.token_hash=? AND i.expires_at>0) OR EXISTS(SELECT 1 FROM invitation_generations g "
                    "WHERE g.store_key=i.store_key AND g.environment=i.environment "
                    "AND g.acquisition_id=i.acquisition_id AND g.token_hash=? AND g.accepted_at IS NOT NULL)) LIMIT 1"
                )
                .bind(store, environment, token_hash, token_hash)
                .all()
            )
            if candidates:
                try:
                    await db.batch(
                        [
                            db.prepare(
                                "INSERT INTO review_recovery_requests VALUES (?,?,?,?,?,'pending') "
                                "ON CONFLICT(store_key,environment,acquisition_id) DO UPDATE SET "
                                "request_id=excluded.request_id,requested_at=excluded.requested_at,state='pending' "
                                "WHERE review_recovery_requests.state IN ('fulfilled','failed') "
                                "AND review_recovery_requests.requested_at<=excluded.requested_at-86400"
                            ).bind(store, environment, candidates[0]["acquisition_id"], random_token(), now)
                        ]
                    )
                except Exception:
                    return fail(503, "recovery temporarily unavailable")
        return Response.json({"status": "requested"}, status=202, headers={"Cache-Control": "no-store"})
    customer_management = re.fullmatch(r"/reviews/v1/manage/([0-9a-f]{64})/(read|edit|withdraw)", path)
    if customer_management:
        if not acquisition_mode:
            return fail(404, "not found")
        review_id, operation = customer_management.groups()
        capability = data.get("management_token")
        secret = str(getattr(env, "REVIEWS_REVIEWER_KEY", "")).encode()
        if not isinstance(capability, str) or not re.fullmatch(r"[0-9a-f]{64}", capability):
            return fail(409, "management unavailable")
        if not hmac.compare_digest(
            management_token(secret, store=store, environment=environment, review_id=review_id), capability
        ):
            return fail(409, "management unavailable")
        try:
            managed_rate = await db.batch(
                [
                    rate(
                        db, store, environment, "review-manage:" + hashlib.sha256(capability.encode()).hexdigest(), now
                    ),
                    db.prepare(
                        "INSERT INTO acquisition_rate_backstop VALUES (?,?,?,1) "
                        "ON CONFLICT(store_key,environment,window) DO UPDATE SET count=count+1"
                    ).bind(store, environment, now // 60),
                    db.prepare(
                        "INSERT INTO acquisition_rate_maintenance VALUES (?,?,1) "
                        "ON CONFLICT(store_key,environment) DO UPDATE SET operations=operations+1"
                    ).bind(store, environment),
                    db.prepare(
                        "SELECT operations FROM acquisition_rate_maintenance WHERE store_key=? AND environment=?"
                    ).bind(store, environment),
                ]
            )
        except Exception:
            return fail(429, "management unavailable; please try again later")
        await cleanup_rates(db, store, environment, rows(managed_rate[-1])[0]["operations"], now)
        if operation == "read":
            if set(data) != {"management_token"}:
                return fail(400, "invalid request")
            result = rows(
                await db.prepare(
                    "SELECT r.state,r.revision_id,v.author,v.title,v.body,v.rating FROM native_reviews r "
                    "JOIN native_review_acquisitions a ON a.store_key=r.store_key AND a.environment=r.environment "
                    "AND a.review_id=r.review_id JOIN native_revisions v ON v.store_key=r.store_key "
                    "AND v.environment=r.environment AND v.revision_id=r.revision_id AND v.review_id=r.review_id "
                    "WHERE r.store_key=? AND r.environment=? AND r.review_id=? AND r.state<>'withdrawn'"
                )
                .bind(store, environment, review_id)
                .all()
            )
            if len(result) != 1:
                return fail(409, "management unavailable")
            return Response.json(result[0], headers={"Cache-Control": "no-store"})
        expected_fields = {"management_token", "revision_id"} | ({"update"} if operation == "edit" else set())
        if set(data) != expected_fields:
            return fail(400, "invalid request")
        try:
            result = await manage_review(
                db,
                store=store,
                environment=environment,
                review_id=review_id,
                capability=capability,
                secret=secret,
                action=operation,
                revision=data["revision_id"],
                data=data.get("update", {}),
                request_key=str(request.headers.get("Idempotency-Key") or ""),
                now=now,
                random_token=random_token,
            )
        except ValueError:
            return fail(409, "management unavailable")
        return Response.json(result, headers={"Cache-Control": "no-store"})
    if path == "/reviews/v1/invitation":
        if not acquisition_mode or set(data) != {"invitation_token"}:
            return fail(404, "invitation unavailable")
        token = data["invitation_token"]
        store_name = str(getattr(env, "REVIEWS_PUBLIC_STORE_NAME", "")).strip()
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token) or not 1 <= len(store_name) <= 200:
            return fail(404, "invitation unavailable")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        subjects = rows(
            await db.prepare(
                "SELECT s.subject_id, n.public_name, CASE WHEN s.consumed_at IS NULL THEN 'available' "
                "ELSE 'submitted' END progress, COALESCE(a.review_id,'') review_id, "
                "i.acquisition_id private_acquisition FROM acquisition_invitations i "
                "JOIN acquisition_invitation_subjects s USING(store_key,environment,acquisition_id) "
                "JOIN review_subject_display_names n ON n.store_key=s.store_key AND n.environment=s.environment "
                "AND n.subject_id=s.subject_id LEFT JOIN native_review_acquisitions a "
                "ON a.store_key=s.store_key AND a.environment=s.environment AND a.acquisition_id=s.acquisition_id "
                "AND a.subject_id=s.subject_id WHERE i.store_key=? AND i.environment=? AND i.token_hash=? "
                "AND i.revoked_at IS NULL AND i.expires_at>? AND i.expires_at>unixepoch() "
                "ORDER BY n.public_name,s.subject_id LIMIT 101"
            )
            .bind(store, environment, token_hash, now)
            .all()
        )
        # Refuse partial display: an approved name is required for each subject.
        expected = rows(
            await db.prepare(
                "SELECT COUNT(*) count FROM acquisition_invitations i "
                "JOIN acquisition_invitation_subjects s USING(store_key,environment,acquisition_id) "
                "WHERE i.store_key=? AND i.environment=? AND i.token_hash=? "
                "AND i.revoked_at IS NULL AND i.expires_at>? AND i.expires_at>unixepoch()"
            )
            .bind(store, environment, token_hash, now)
            .all()
        )[0]["count"]
        if not subjects or len(subjects) != expected or len(subjects) > 100:
            return fail(404, "invitation unavailable")
        private_acquisition = subjects[0]["private_acquisition"]
        draft_scope = hmac.new(
            str(env.REVIEWS_REVIEWER_KEY).encode(),
            json.dumps(["reviews-draft-v1", store, environment, private_acquisition], separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        for subject in subjects:
            subject.pop("private_acquisition")
            review_id = subject.pop("review_id")
            if review_id:
                subject["review_id"] = review_id
                subject["management_token"] = management_token(
                    str(env.REVIEWS_REVIEWER_KEY).encode(), store=store, environment=environment, review_id=review_id
                )
        return Response.json(
            {"store_name": store_name, "draft_scope": draft_scope, "subjects": subjects},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    if path == "/reviews-admin/v1/subject-display-names":
        if (
            set(data) != {"subject_id", "public_name"}
            or not isinstance(data["subject_id"], str)
            or not ID.fullmatch(data["subject_id"])
            or not isinstance(data["public_name"], str)
            or not 1 <= len(data["public_name"].strip()) <= 200
        ):
            return fail(400, "invalid subject name")
        try:
            await db.batch(
                [
                    db.prepare(
                        "INSERT INTO review_subject_display_names VALUES (?,?,?,?) "
                        "ON CONFLICT(store_key,environment,subject_id) DO UPDATE SET public_name=excluded.public_name"
                    ).bind(store, environment, data["subject_id"], data["public_name"].strip())
                ]
            )
        except Exception:
            return fail(409, "subject unavailable")
        return Response.json({"status": "saved"}, headers={"Cache-Control": "no-store"})
    if path == "/reviews-admin/v1/subjects":
        if (
            set(data) != {"subject_id", "kind", "path"}
            or not isinstance(data["subject_id"], str)
            or not ID.fullmatch(data["subject_id"])
        ):
            return fail(400, "invalid subject")
        if (
            data["kind"] not in ("product", "service", "store")
            or not isinstance(data["path"], str)
            or len(data["path"]) > 2048
            or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*", data["path"])
        ):
            return fail(400, "invalid subject")
        sid = data["subject_id"]
        try:
            await db.batch(
                [
                    db.prepare(
                        "INSERT INTO native_guard SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM "
                        "review_snapshot_subjects WHERE store_key=? AND environment=? AND subject_id=? "
                        "AND (kind<>? OR canonical_path<>?)) "
                        "THEN 1 ELSE 0 END"
                    ).bind(store, environment, sid, data["kind"], data["path"]),
                    db.prepare("INSERT INTO review_subjects VALUES (?,?,?,?,?)").bind(
                        store, environment, sid, data["kind"], data["path"]
                    ),
                    db.prepare(
                        "INSERT INTO native_publication_versions(store_key,environment,subject_id) VALUES (?,?,?)"
                    ).bind(store, environment, sid),
                    db.prepare("DELETE FROM native_guard"),
                ]
            )
        except Exception:
            return fail(409, "subject conflict")
        return Response.json({"subject_id": sid}, status=201)
    if path == "/reviews-admin/v1/test-capabilities":
        if acquisition_mode:
            return fail(404, "not found")
        if (
            set(data) != {"subject_id", "test_reviewer"}
            or not isinstance(data["subject_id"], str)
            or not ID.fullmatch(data["subject_id"])
            or not isinstance(data["test_reviewer"], str)
            or not ID.fullmatch(data["test_reviewer"])
        ):
            return fail(400, "invalid subject")
        token = random_token()
        reviewer = hmac.new(
            str(env.REVIEWS_TEST_ADMIN_TOKEN).encode(),
            json.dumps([store, environment, data["test_reviewer"]]).encode(),
            hashlib.sha256,
        ).hexdigest()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        try:
            await db.batch(
                [
                    db.prepare(
                        "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM review_subjects "
                        "WHERE store_key=? AND environment=? AND subject_id=?) THEN 1 ELSE 0 END"
                    ).bind(store, environment, data["subject_id"]),
                    db.prepare("INSERT INTO native_capabilities VALUES (?,?,?,?,?,?,NULL)").bind(
                        store, environment, token_hash, reviewer, data["subject_id"], now + 300
                    ),
                    db.prepare("DELETE FROM native_guard"),
                ]
            )
        except Exception:
            return fail(429, "capability unavailable")
        return Response.json(
            {"test_capability": token, "expires_at": now + 300},
            status=201,
            headers={"Cache-Control": "no-store"},
        )
    if path == "/reviews/v1/contributions":
        proof_field = "invitation_token" if acquisition_mode else "test_capability"
        required = {"subject_id", proof_field} | (set() if acquisition_mode else {"author"})
        if not required <= set(data) or set(data) - {
            "subject_id",
            "author",
            "title",
            "body",
            "rating",
            proof_field,
        }:
            return fail(400, "invalid submission")
        sid, proof = data["subject_id"], data[proof_field]
        author = data.get("author", "")
        title, body, rating = (data.get(k) for k in ("title", "body", "rating"))
        key = str(request.headers.get("Idempotency-Key") or "")
        if (
            not isinstance(sid, str)
            or not ID.fullmatch(sid)
            or not ID.fullmatch(key)
            or not isinstance(proof, str)
            or not re.fullmatch(r"[0-9a-f]{64}", proof)
        ):
            return fail(400, "invalid submission")
        if (
            not isinstance(author, str)
            or len(author.strip()) > 120
            or (not acquisition_mode and not author.strip())
            or (body is not None and (not isinstance(body, str) or len(body) > 5000))
            or (title is not None and (not isinstance(title, str) or len(title) > 200))
            or (rating is not None and (type(rating) is not int or not 1 <= rating <= 5))
        ):
            return fail(400, "invalid submission")
        body = body or ""
        if rating is None and (
            (acquisition_mode and len(body.strip()) < 3)
            or (not acquisition_mode and not body.strip() and not (title and title.strip()))
        ):
            return fail(400, "empty submission")
        if acquisition_mode:
            author = author.strip() or "Anonymous"
            token_hash = hashlib.sha256(proof.encode()).hexdigest()
            eligible = rows(
                await db.prepare(
                    "SELECT 1 AS eligible FROM acquisition_invitations i JOIN acquisition_invitation_subjects s "
                    "USING(store_key,environment,acquisition_id) WHERE i.store_key=? AND i.environment=? "
                    "AND i.token_hash=? AND i.revoked_at IS NULL AND i.expires_at>? "
                    "AND i.expires_at>unixepoch() AND s.subject_id=?"
                )
                .bind(store, environment, token_hash, now, sid)
                .all()
            )
            if not eligible:
                return fail(409, "submission unavailable")
            try:
                rate_results = await db.batch(
                    [
                        db.prepare(
                            "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM "
                            "acquisition_invitations i JOIN acquisition_invitation_subjects s "
                            "USING(store_key,environment,acquisition_id) WHERE i.store_key=? AND "
                            "i.environment=? AND i.token_hash=? AND i.revoked_at IS NULL AND "
                            "i.expires_at>? AND i.expires_at>unixepoch() AND s.subject_id=?) "
                            "THEN 1 ELSE 0 END"
                        ).bind(store, environment, token_hash, now, sid),
                        rate(db, store, environment, "purchase-submit:" + token_hash, now),
                        db.prepare(
                            "INSERT INTO acquisition_rate_backstop VALUES (?,?,?,1) "
                            "ON CONFLICT(store_key,environment,window) DO UPDATE SET count=count+1"
                        ).bind(store, environment, now // 60),
                        db.prepare(
                            "INSERT INTO acquisition_rate_maintenance VALUES (?,?,1) "
                            "ON CONFLICT(store_key,environment) DO UPDATE SET operations=operations+1"
                        ).bind(store, environment),
                        db.prepare(
                            "SELECT operations FROM acquisition_rate_maintenance WHERE store_key=? AND environment=?"
                        ).bind(store, environment),
                        db.prepare("DELETE FROM native_guard"),
                    ]
                )
            except Exception:
                return fail(429, "submission unavailable; please try again later")
            await cleanup_rates(db, store, environment, rows(rate_results[-2])[0]["operations"], now)
            try:
                result = await redeem_purchase(
                    db,
                    store=store,
                    environment=environment,
                    subject=sid,
                    token=proof,
                    request_key=key,
                    author=author,
                    title=title,
                    body=body,
                    rating=rating,
                    reviewer_key_secret=str(getattr(env, "REVIEWS_REVIEWER_KEY", "")).encode(),
                    now=now,
                    random_token=random_token,
                )
            except ValueError:
                return fail(409, "submission unavailable")
            result["management_token"] = management_token(
                str(env.REVIEWS_REVIEWER_KEY).encode(),
                store=store,
                environment=environment,
                review_id=result["review_id"],
            )
            return Response.json(result, status=202, headers={"Cache-Control": "no-store"})
        digest = hashlib.sha256(
            json.dumps([sid, author, title, body, rating], ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()
        token_hash = hashlib.sha256(proof.encode()).hexdigest()
        existing = rows(
            await db.prepare(
                "SELECT payload_hash,token_hash,review_id,revision_id FROM native_requests WHERE store_key=? AND "
                "environment=? AND request_key=?"
            )
            .bind(store, environment, key)
            .all()
        )
        if existing:
            item = existing[0]
            if item["payload_hash"] != digest or item["token_hash"] != token_hash:
                return fail(409, "request conflict")
            return Response.json(
                {"review_id": item["review_id"], "revision_id": item["revision_id"], "status": "pending"}, status=202
            )
        review_id, revision_id, event_id = random_token(), random_token(), random_token()
        try:
            await db.batch(
                [
                    db.prepare(
                        "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM "
                        "native_capabilities WHERE store_key=? AND environment=? AND token_hash=? AND "
                        "subject_id=? AND used_at IS NULL AND expires_at>?) THEN 1 ELSE 0 END"
                    ).bind(store, environment, token_hash, sid, now),
                    db.prepare(
                        "INSERT INTO native_reviews SELECT "
                        "store_key,environment,?,subject_id,reviewer_key,'pending',?,? FROM "
                        "native_capabilities WHERE store_key=? AND environment=? AND token_hash=?"
                    ).bind(review_id, revision_id, now, store, environment, token_hash),
                    db.prepare(
                        "INSERT INTO native_revisions VALUES (?,?,?,?,?,NULLIF(?,''),?,CAST(NULLIF(?,'') AS INTEGER))"
                    ).bind(
                        store,
                        environment,
                        revision_id,
                        review_id,
                        author,
                        title or "",
                        body,
                        str(rating) if rating is not None else "",
                    ),
                    db.prepare("INSERT INTO native_requests VALUES (?,?,?,?,?,?,?)").bind(
                        store, environment, key, digest, token_hash, review_id, revision_id
                    ),
                    db.prepare(
                        "UPDATE native_capabilities SET used_at=? WHERE store_key=? AND environment=? AND token_hash=?"
                    ).bind(now, store, environment, token_hash),
                    db.prepare("INSERT INTO native_audit VALUES (?,?,?,?,?,?,?,?)").bind(
                        event_id, store, environment, review_id, revision_id, "submitted", "test_guest", now
                    ),
                    db.prepare("DELETE FROM native_guard"),
                ]
            )
        except Exception:
            existing = rows(
                await db.prepare(
                    "SELECT payload_hash,token_hash,review_id,revision_id FROM native_requests WHERE store_key=? AND "
                    "environment=? AND request_key=?"
                )
                .bind(store, environment, key)
                .all()
            )
            if existing and existing[0]["payload_hash"] == digest and existing[0]["token_hash"] == token_hash:
                return Response.json(
                    {
                        "review_id": existing[0]["review_id"],
                        "revision_id": existing[0]["revision_id"],
                        "status": "pending",
                    },
                    status=202,
                )
            return fail(409, "submission unavailable")
        return Response.json({"review_id": review_id, "revision_id": revision_id, "status": "pending"}, status=202)
    reply_match = re.fullmatch(r"/reviews-admin/v1/contributions/([0-9a-f]{64})/(reply|reply-withdraw)", path)
    if reply_match:
        try:
            result = await mutate_reply(
                db,
                store=store,
                environment=environment,
                review_id=reply_match.group(1),
                data=data,
                action="upsert" if reply_match.group(2) == "reply" else "withdraw",
                key=str(request.headers.get("Idempotency-Key") or ""),
                actor=actor,
                now=now,
                random_token=random_token,
            )
        except (ValueError, TypeError):
            return fail(409, "reply unavailable")
        return Response.json(result, headers={"Cache-Control": "no-store"})
    match = re.fullmatch(r"/reviews-admin/v1/contributions/([0-9a-f]{64})/(approve|reject|withdraw)", path)
    if match:
        if (
            set(data) != {"revision_id"}
            or not isinstance(data["revision_id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", data["revision_id"])
        ):
            return fail(400, "invalid revision")
        rid, action = match.groups()
        desired = {"approve": "published", "reject": "rejected", "withdraw": "withdrawn"}[action]
        source = "published" if action == "withdraw" else "pending"
        revision = data["revision_id"]
        current = rows(
            await db.prepare(
                "SELECT state,revision_id,subject_id FROM native_reviews WHERE store_key=? AND "
                "environment=? AND review_id=?"
            )
            .bind(store, environment, rid)
            .all()
        )
        if not current or current[0]["revision_id"] != revision:
            return fail(409, "revision conflict")
        if current[0]["state"] == desired:
            return Response.json({"review_id": rid, "status": desired})
        subject = current[0]["subject_id"]
        event_id = random_token()
        delta = 1 if action == "approve" else -1 if action == "withdraw" else 0
        try:
            await db.batch(
                [
                    db.prepare(
                        "INSERT INTO native_guard SELECT CASE WHEN EXISTS(SELECT 1 FROM native_reviews r "
                        "JOIN native_publication_versions p ON p.store_key=r.store_key AND p.environment=r.environment "
                        "AND p.subject_id=r.subject_id JOIN native_revisions v ON v.store_key=r.store_key "
                        "AND v.environment=r.environment AND v.revision_id=r.revision_id AND v.review_id=r.review_id "
                        "WHERE r.store_key=? AND r.environment=? AND r.review_id=? AND r.revision_id=? AND "
                        "r.state=?) THEN 1 ELSE 0 END"
                    ).bind(store, environment, rid, revision, source),
                    db.prepare(
                        "UPDATE native_reviews SET state=? WHERE store_key=? AND environment=? AND "
                        "review_id=? AND revision_id=? AND state=?"
                    ).bind(desired, store, environment, rid, revision, source),
                    db.prepare(
                        "UPDATE native_publication_versions SET version=version+1, "
                        "contribution_count=contribution_count+?, "
                        "rated_count=rated_count+?*(SELECT CASE WHEN rating IS NULL THEN 0 ELSE 1 END "
                        "FROM native_revisions WHERE store_key=? AND environment=? AND revision_id=?), "
                        "rating_sum=rating_sum+?*(SELECT COALESCE(rating,0) FROM native_revisions "
                        "WHERE store_key=? AND environment=? AND revision_id=?) "
                        "WHERE store_key=? AND environment=? AND subject_id=?"
                    ).bind(
                        delta,
                        delta,
                        store,
                        environment,
                        revision,
                        delta,
                        store,
                        environment,
                        revision,
                        store,
                        environment,
                        subject,
                    ),
                    db.prepare("INSERT INTO native_audit VALUES (?,?,?,?,?,?,?,?)").bind(
                        event_id, store, environment, rid, revision, action, actor, now
                    ),
                    db.prepare("INSERT INTO native_effect_outbox VALUES (?,?,?,?,?,?)").bind(
                        event_id, store, environment, subject, "invalidate", now
                    ),
                    db.prepare("DELETE FROM native_guard"),
                ]
            )
        except Exception:
            return fail(409, "moderation conflict")
        return Response.json({"review_id": rid, "status": desired})
    return fail(404, "not found")


async def native_read(db, store, environment, subject_id, endpoint, params, native_version):
    """Read only approved native revisions for a scoped native subject."""
    version = "native-" + str(native_version)
    if endpoint == "summary":
        aggregate_rows = rows(
            await db.prepare(
                "SELECT version,contribution_count,rated_count,rating_sum FROM native_publication_versions "
                "WHERE store_key=? AND environment=? AND subject_id=?"
            )
            .bind(store, environment, subject_id)
            .all()
        )
        if not aggregate_rows:
            return fail(503, "publication unavailable")
        aggregate = aggregate_rows[0]
        count, total = aggregate["rated_count"], aggregate["rating_sum"]
        version = "native-" + str(aggregate["version"])
        return Response.json(
            {
                "subject_id": subject_id,
                "snapshot_id": version,
                "rating": {"average": total / count if count else None, "rated_count": count},
                "contribution_count": aggregate["contribution_count"],
            },
            headers={"Cache-Control": "no-store"},
        )
    try:
        limit = int(params.get("limit", ["20"])[0])
    except ValueError:
        return fail(400, "invalid limit")
    if not 1 <= limit <= 50:
        return fail(400, "invalid limit")
    after = params.get("after", [None])[0]
    if after and (params.get("snapshot", [None])[0] != version or not re.fullmatch(r"[0-9a-f]{64}", after)):
        return fail(400, "invalid or stale cursor")
    if not after and "snapshot" in params:
        return fail(400, "invalid cursor")
    cursor_time, cursor_id = None, None
    if after:
        cursor = rows(
            await db.prepare(
                "SELECT created_at FROM native_reviews WHERE store_key=? AND environment=? AND subject_id=? "
                "AND review_id=? AND state='published'"
            )
            .bind(store, environment, subject_id, after)
            .all()
        )
        if not cursor:
            return fail(400, "invalid cursor")
        cursor_time, cursor_id = cursor[0]["created_at"], after
    query = (
        "SELECT r.review_id,r.created_at,v.author,v.title,v.body,v.rating, "
        "EXISTS(SELECT 1 FROM native_review_acquisitions a WHERE a.store_key=r.store_key "
        "AND a.environment=r.environment AND a.review_id=r.review_id AND a.subject_id=r.subject_id) verified_purchase "
        "FROM native_reviews r "
        "JOIN native_revisions v ON v.store_key=r.store_key AND v.environment=r.environment AND "
        "v.revision_id=r.revision_id "
        "WHERE r.store_key=? AND r.environment=? AND r.subject_id=? AND r.state='published' "
    )
    bindings = [store, environment, subject_id]
    if after:
        query += "AND (r.created_at < ? OR (r.created_at=? AND r.review_id>?)) "
        bindings.extend([cursor_time, cursor_time, cursor_id])
    query += "ORDER BY r.created_at DESC,r.review_id LIMIT ?"
    found = rows(await db.prepare(query).bind(*bindings, limit + 1).all())
    current_version = rows(
        await db.prepare(
            "SELECT version FROM native_publication_versions WHERE store_key=? AND environment=? AND subject_id=?"
        )
        .bind(store, environment, subject_id)
        .all()
    )
    if not current_version or "native-" + str(current_version[0]["version"]) != version:
        return fail(409, "publication changed; restart pagination")
    items = [
        {
            "id": x["review_id"],
            "kind": "review",
            "source": {"kind": "native", "attribution": None},
            "author": x["author"],
            "title": x["title"],
            "body": x["body"],
            "rating": x["rating"],
            "created_at": x["created_at"],
            "verification": "cartray_verified_acquisition" if x["verified_purchase"] else "unverified",
            "replies": [],
        }
        for x in found[:limit]
    ]
    replies = await public_replies(db, store, environment, [item["id"] for item in items])
    for item in items:
        if item["id"] in replies:
            item["replies"].append(replies[item["id"]])
    final_version = rows(
        await db.prepare(
            "SELECT version FROM native_publication_versions WHERE store_key=? AND environment=? AND subject_id=?"
        )
        .bind(store, environment, subject_id)
        .all()
    )
    if not final_version or "native-" + str(final_version[0]["version"]) != version:
        return fail(409, "publication changed; restart pagination")
    has_more = len(found) > limit
    output = {
        "subject_id": subject_id,
        "snapshot_id": version,
        "contributions": items,
        "next_after": items[-1]["id"] if has_more and items else None,
        "has_more": has_more,
    }
    while len(json.dumps(output, ensure_ascii=True).encode("ascii")) > 128_000:
        items.pop()
        if not items:
            return fail(503, "response exceeds limit")
        output["has_more"] = True
        output["next_after"] = items[-1]["id"]
    return Response.json(output, headers={"Cache-Control": "no-store"})
