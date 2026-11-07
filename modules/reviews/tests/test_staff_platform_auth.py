"""Staff key rotation, platform-verifier and edge-admission behavior."""

import asyncio
import types

import pytest
from cartray_reviews.staff_identity import JwksCache, StaffAuthenticationError, authenticate_staff
from test_native_lifecycle import lifecycle as lifecycle
from test_staff_auth_runtime import actor, claims, settings
from test_staff_identity import AUDIENCE, ISSUER, KEY, token


def run(coro):
    return asyncio.run(coro)


def test_cache_expiry_rotation_and_provider_outage_fail_closed():
    now = [0.0]
    cache = JwksCache(capacity=2, clock=lambda: now[0])
    state = {"keys": [{"kid": "old"}]}
    calls = []

    async def loader(url):
        calls.append(url)
        if state is None:
            raise OSError("offline")
        return state

    assert run(cache.get("issuer-a", loader, 10))["keys"][0]["kid"] == "old"
    state = {"keys": [{"kid": "new"}]}
    now[0] = 9.0
    assert run(cache.get("issuer-a", loader, 10))["keys"][0]["kid"] == "old"
    assert len(calls) == 1
    now[0] = 10.0
    assert run(cache.get("issuer-a", loader, 10))["keys"][0]["kid"] == "new"
    state = None
    now[0] = 20.0
    with pytest.raises(OSError):
        run(cache.get("issuer-a", loader, 10))
    assert "issuer-a" not in cache.entries


def test_cache_bounds_and_shorter_policy_are_respected():
    now = [0.0]
    cache = JwksCache(capacity=2, clock=lambda: now[0])
    calls = []

    async def loader(url):
        calls.append(url)
        return {"keys": []}

    run(cache.get("a", loader, 60))
    now[0] = 2.0
    run(cache.get("a", loader, 1))
    assert calls == ["a", "a"]
    run(cache.get("b", loader, 10))
    run(cache.get("c", loader, 10))
    assert len(cache.entries) == 2
    for ttl in (0, -1, 61, True, "1"):
        with pytest.raises(StaffAuthenticationError):
            run(cache.get("d", loader, ttl))


def test_cache_rejects_invalid_capacity_and_clock():
    for capacity in (0, -1, True, 1.5, float("nan"), "2"):
        with pytest.raises(ValueError):
            JwksCache(capacity=capacity)
    with pytest.raises(TypeError):
        JwksCache(clock=None)


def test_cache_lifetime_starts_at_fetch_and_slow_refresh_fails_closed():
    now = [0.0]
    cache = JwksCache(clock=lambda: now[0])
    calls = []

    async def loader(url):
        calls.append(url)
        now[0] += 6
        return {"keys": []}

    run(cache.get("issuer", loader, 10))
    now[0] = 10
    run(cache.get("issuer", loader, 10))
    assert len(calls) == 2
    with pytest.raises(StaffAuthenticationError, match="expired during refresh"):
        run(cache.get("other", loader, 5))
    assert "other" not in cache.entries


def test_cache_rejects_malformed_sets_without_storing_them():
    cache = JwksCache()

    async def invalid(_url):
        return {"keys": [{}] * 21}

    with pytest.raises(StaffAuthenticationError):
        run(cache.get("issuer", invalid, 30))
    assert cache.entries == {}


def test_concurrent_refresh_does_not_stampede_and_recovers_after_cancellation():
    async def exercise():
        cache = JwksCache()
        started = asyncio.Event()

        async def blocked(_url):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(cache.get("issuer", blocked, 30))
        await started.wait()
        with pytest.raises(StaffAuthenticationError, match="refreshing"):
            await cache.get("issuer", blocked, 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not cache.refreshing

        async def healthy(_url):
            return {"keys": []}

        assert await cache.get("issuer", healthy, 30) == {"keys": []}

    run(exercise())


def test_injected_platform_verifier_is_required_and_fails_closed():
    policy = {"staff-1": ["reviews:moderate"]}
    jwks = {"keys": [{"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}]}
    now = 1_800_000_000
    calls = []

    async def loader(_url):
        return jwks

    async def verify(key, signature, data):
        calls.append((key["kid"], len(signature), data))
        return True

    async def deny(*_args):
        return False

    async def broken(*_args):
        raise RuntimeError("provider failure")

    def authenticate(verifier):
        return run(
            authenticate_staff(
                token(),
                issuer=ISSUER,
                audience=AUDIENCE,
                permissions=policy,
                required_permission="reviews:moderate",
                now=now,
                load_jwks=loader,
                verify_signature=verifier,
            )
        )

    assert authenticate(verify)["actor_id"].startswith("staff:")
    assert calls[0][0:2] == ("key-1", 256)
    for verifier in (deny, broken):
        with pytest.raises(StaffAuthenticationError):
            authenticate(verifier)


def test_edge_admission_fails_closed_and_uses_actor_specific_hashed_key(lifecycle):
    seen = []

    async def limit(options):
        seen.append(options["key"])
        return types.SimpleNamespace(success=True)

    async def await_direct(value, _timeout):
        return await value

    lifecycle.await_promise = await_direct
    env = settings(
        REVIEWS_STAFF_ADMISSION="edge",
        REVIEWS_STAFF_RATE_KEY="r" * 40,
        REVIEWS_STAFF_INGRESS=types.SimpleNamespace(limit=limit),
    )
    assertion = token(claims())
    # A missing or malformed trusted IP never reaches verification.
    assert actor(lifecycle, env, assertion) is None
    request = types.SimpleNamespace(headers={"Cf-Access-Jwt-Assertion": assertion, "CF-Connecting-IP": "192.0.2.1"})
    assert run(lifecycle.staff_actor(request, env, "reviews:moderate")).startswith("staff:")
    assert len(seen[0]) == 64 and "192.0.2.1" not in seen[0]
    request.headers["CF-Connecting-IP"] = "192.0.2.2"
    assert run(lifecycle.staff_actor(request, env, "reviews:moderate")).startswith("staff:")
    assert seen[0] != seen[1]
    for address in ("2001:db8:1:2::1", "2001:db8:1:2::ffff", "2001:db8:1:3::1", "::ffff:192.0.2.1"):
        request.headers["CF-Connecting-IP"] = address
        assert run(lifecycle.staff_actor(request, env, "reviews:moderate")).startswith("staff:")
    assert seen[2] == seen[3]
    assert seen[3] != seen[4]
    assert seen[5] == seen[0]
    env.REVIEWS_STAFF_INGRESS = None
    assert run(lifecycle.staff_actor(request, env, "reviews:moderate")) is None

    async def deny(_options):
        return types.SimpleNamespace(success=False)

    env.REVIEWS_STAFF_INGRESS = types.SimpleNamespace(limit=deny)
    assert run(lifecycle.staff_actor(request, env, "reviews:moderate")) is None
    env.REVIEWS_STAFF_RATE_KEY = "too-short"
    assert run(lifecycle.staff_actor(request, env, "reviews:moderate")) is None


def test_webcrypto_unavailable_never_falls_back_to_python_verifier(lifecycle):
    env = settings(REVIEWS_STAFF_CRYPTO="webcrypto")
    # CPython fixture has no Workerd crypto object; a valid signature must fail.
    assert actor(lifecycle, env, token(claims())) is None
    assert actor(lifecycle, settings(REVIEWS_STAFF_CRYPTO="unknown"), token(claims())) is None
