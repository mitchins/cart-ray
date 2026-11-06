"""Exercise the real staff boundary, permissions and audit against SQLite."""

import asyncio
import hashlib
import json
import sys
import time
import types
from unittest.mock import patch

import pytest
from test_native_lifecycle import Database, invoke
from test_native_lifecycle import lifecycle as lifecycle
from test_staff_identity import AUDIENCE, ISSUER, KEY, token

ADMIN = "s" * 40


def settings(**overrides):
    jwks = {"keys": [{"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}]}
    config = dict(
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_STORE_KEY="store",
        REVIEWS_STAFF_MODE="cloudflare_access",
        REVIEWS_STAFF_ISSUER=ISSUER,
        REVIEWS_STAFF_AUDIENCE=AUDIENCE,
        REVIEWS_STAFF_POLICY=json.dumps({"staff-1": ["reviews:moderate"]}),
        REVIEWS_STAFF_TEST_JWKS=json.dumps(jwks),
        REVIEWS_TEST_ADMIN_TOKEN=ADMIN,
    )
    return types.SimpleNamespace(**(config | overrides))


def claims(**overrides):
    now = int(time.time())
    return dict(iss=ISSUER, aud=[AUDIENCE], sub="staff-1", iat=now - 1, exp=now + 300) | overrides


def actor(module, env, assertion=None, *, permission="reviews:moderate", bearer=None):
    headers = {}
    if assertion is not None:
        headers["Cf-Access-Jwt-Assertion"] = assertion
    if bearer is not None:
        headers["Authorization"] = "Bearer " + bearer
    request = types.SimpleNamespace(headers=headers)
    return asyncio.run(module.staff_actor(request, env, permission))


def test_actor_authentication_policy_and_fallback(lifecycle):
    assertion = token(claims())
    env = settings()
    expected = "staff:" + hashlib.sha256((ISSUER + "\0staff-1").encode()).hexdigest()
    assert actor(lifecycle, env, assertion) == expected
    assert actor(lifecycle, env, assertion, permission="reviews:subjects") is None
    assert actor(lifecycle, env, bearer=ADMIN) is None
    assert actor(lifecycle, settings(REVIEWS_ENVIRONMENT="live"), assertion) is None
    assert actor(lifecycle, settings(REVIEWS_STAFF_MODE="unsupported"), assertion, bearer=ADMIN) is None
    assert actor(lifecycle, settings(REVIEWS_STAFF_MODE="test_admin"), bearer=ADMIN) == "test_admin"
    assert actor(lifecycle, settings(REVIEWS_STAFF_MODE="test_admin"), bearer="incorrect") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"REVIEWS_STAFF_POLICY": "{"},
        {"REVIEWS_STAFF_POLICY": '{"x":' + "[" * 1100 + "0" + "]" * 1100 + "}"},
        {"REVIEWS_STAFF_POLICY": '{"a":1,"a":2}'},
        {"REVIEWS_STAFF_POLICY": "x" * 16385},
        {"REVIEWS_STAFF_POLICY": "[]"},
        {"REVIEWS_STAFF_ISSUER": "https://evil.example"},
        {"REVIEWS_STAFF_AUDIENCE": ""},
        {"REVIEWS_STAFF_TEST_JWKS": "{"},
        {"REVIEWS_STAFF_TEST_JWKS": "x" * 65537},
        {"REVIEWS_STAFF_TEST_JWKS": '{"keys":[],"keys":[]}'},
    ],
)
def test_invalid_policy_and_fixture_fail_closed(lifecycle, overrides):
    assert actor(lifecycle, settings(**overrides), token(claims())) is None


class Reader:
    def __init__(self, chunks):
        self.chunks, self.released = list(chunks), False

    def read(self):
        if not self.chunks:
            return types.SimpleNamespace(done=True)
        data = self.chunks.pop(0)
        if isinstance(data, Exception):
            raise data
        return types.SimpleNamespace(done=False, value=types.SimpleNamespace(to_py=lambda: data))

    def releaseLock(self):
        self.released = True


class Controller:
    def __init__(self):
        self.signal = object()
        self.aborted = False

    def abort(self):
        self.aborted = True


def verify_through_network(lifecycle, *, status=200, chunks=(), network_error=None):
    reader, controller, calls = Reader(chunks), Controller(), []
    fake_js = types.ModuleType("js")
    fake_js.AbortController = types.SimpleNamespace(new=lambda: controller)

    def fetch(url, options):
        calls.append((url, options))
        if network_error:
            raise network_error
        return types.SimpleNamespace(status=status, body=types.SimpleNamespace(getReader=lambda: reader))

    fake_js.fetch = fetch

    async def await_value(value, _timeout):
        return value

    lifecycle.await_promise = await_value
    with patch.dict(sys.modules, {"js": fake_js}):
        result = actor(lifecycle, settings(REVIEWS_STAFF_TEST_JWKS=""), token(claims()))
    assert calls and calls[0][0] == ISSUER + "/cdn-cgi/access/certs"
    assert calls[0][1]["redirect"] == "error"
    assert controller.aborted
    if status == 200 and not network_error:
        assert reader.released
    return result


def test_bounded_network_jwks_success_and_failure(lifecycle):
    jwks = json.dumps({"keys": [{"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}]}).encode()
    assert verify_through_network(lifecycle, chunks=[jwks[:20], jwks[20:]]).startswith("staff:")
    assert verify_through_network(lifecycle, status=503) is None
    assert verify_through_network(lifecycle, network_error=TimeoutError("offline")) is None
    assert verify_through_network(lifecycle, chunks=[b"x" * 65537]) is None
    assert verify_through_network(lifecycle, chunks=[b"{"]) is None
    assert verify_through_network(lifecycle, chunks=[TimeoutError("read deadline")]) is None


def test_signed_staff_moderation_audit_is_scoped_and_replay_safe(lifecycle):
    db = Database()
    env = settings(REVIEWS_DB=db, REVIEWS_STAFF_MODE="test_admin")
    subject = {"subject_id": "policy", "kind": "product", "path": "/policy/"}
    assert invoke(lifecycle, env, "/reviews-admin/v1/subjects", subject, admin=True).status == 201
    cap = invoke(
        lifecycle,
        env,
        "/reviews-admin/v1/test-capabilities",
        {"subject_id": "policy", "test_reviewer": "x"},
        admin=True,
    )
    assert cap.status == 201
    submission = {
        "subject_id": "policy",
        "author": "Guest",
        "body": "Synthetic",
        "rating": 2,
        "test_capability": cap.body["test_capability"],
    }
    review = invoke(lifecycle, env, "/reviews/v1/contributions", submission, key="staff-request")
    assert review.status == 202
    env.REVIEWS_STAFF_MODE = "cloudflare_access"
    assertion = token(claims())
    path = "/reviews-admin/v1/contributions/" + review.body["review_id"] + "/approve"
    data = {"revision_id": review.body["revision_id"]}

    def moderate(value):
        req = types.SimpleNamespace(method="POST", headers={"Cf-Access-Jwt-Assertion": value}, data=data, body=None)
        return asyncio.run(lifecycle.handle_write(req, env, path))

    assert moderate(token(claims(sub="other"))).status == 401
    assert moderate(assertion).status == 200
    assert moderate(assertion).status == 200
    rows = db.db.execute("SELECT actor,action FROM native_audit WHERE action='approve'").fetchall()
    expected = "staff:" + hashlib.sha256((ISSUER + "\0staff-1").encode()).hexdigest()
    assert [tuple(row) for row in rows] == [(expected, "approve")]
    assert (
        db.db.execute("SELECT rated_count FROM native_publication_versions WHERE subject_id='policy'").fetchone()[0]
        == 1
    )
