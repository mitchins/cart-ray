"""Test-only signed staff moderation on local Workerd/D1."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from native_workerd import ADMIN, BASE, call
from test_staff_identity import AUDIENCE, ISSUER, KEY, token
from workerd_integration import MODULE, ROOT, start, stop


def signed_call(path, payload=None, *, assertion=None, bearer=None):
    headers = {"Content-Type": "application/json"}
    if assertion:
        headers["Cf-Access-Jwt-Assertion"] = assertion
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main():
    with tempfile.TemporaryDirectory(prefix="reviews-staff-d1-") as directory:
        env = os.environ | {"REVIEWS_LOCAL_PERSIST_DIR": directory}
        subprocess.run([sys.executable, str(MODULE / "tools/install.py")], cwd=ROOT, env=env, check=True)
        process = start("wrangler.test.toml", directory, "collect", ADMIN)
        try:
            assert (
                call(
                    "/reviews-admin/v1/subjects",
                    {"subject_id": "example-policy", "kind": "product", "path": "/example-policy/"},
                    admin=True,
                )[0]
                == 201
            )
            status, issued = call(
                "/reviews-admin/v1/test-capabilities",
                {"subject_id": "example-policy", "test_reviewer": "guest"},
                admin=True,
            )
            assert status == 201
            status, review = call(
                "/reviews/v1/contributions",
                {
                    "subject_id": "example-policy",
                    "author": "Example",
                    "title": "",
                    "body": "Synthetic staff test",
                    "rating": 2,
                    "test_capability": issued["test_capability"],
                },
                key="staff-test",
            )
            assert status == 202, (status, review)
        finally:
            stop(process)
        jwks = {"keys": [{"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}]}
        options = {
            "REVIEWS_STAFF_MODE": "cloudflare_access",
            "REVIEWS_STAFF_ISSUER": ISSUER,
            "REVIEWS_STAFF_AUDIENCE": AUDIENCE,
            "REVIEWS_STAFF_POLICY": json.dumps({"staff-1": ["reviews:moderate"]}),
            "REVIEWS_STAFF_TEST_JWKS": json.dumps(jwks),
        }
        process = start("wrangler.test.toml", directory, "collect", None, options)
        try:
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": [AUDIENCE], "sub": "staff-1", "iat": now - 1, "exp": now + 120}
            assertion = token(claims)
            assert signed_call("/reviews-admin/v1/pending", bearer=ADMIN)[0] == 401
            assert signed_call("/reviews-admin/v1/pending", assertion=token(claims | {"exp": now - 1}))[0] == 401
            assert signed_call("/reviews-admin/v1/pending", assertion=token(claims | {"aud": ["wrong"]}))[0] == 401
            assert signed_call("/reviews-admin/v1/pending", assertion=token(claims | {"sub": "other"}))[0] == 401
            assert signed_call("/reviews-admin/v1/pending", assertion=assertion)[0] == 200
            assert (
                signed_call(
                    "/reviews-admin/v1/subjects",
                    {"subject_id": "another", "kind": "product", "path": "/another/"},
                    assertion=assertion,
                )[0]
                == 401
            )
            assert (
                signed_call(
                    "/reviews-admin/v1/test-capabilities",
                    {"subject_id": "example-policy", "test_reviewer": "other"},
                    assertion=assertion,
                )[0]
                == 401
            )
            action = "/reviews-admin/v1/contributions/" + review["review_id"] + "/approve"
            assert signed_call(action, {"revision_id": review["revision_id"]}, assertion=assertion)[0] == 200
            assert signed_call(action, {"revision_id": review["revision_id"]}, assertion=assertion)[0] == 200
            summary = call("/reviews/v1/subjects/example-policy/summary")
            assert summary[1]["rating"] == {"average": 2.0, "rated_count": 1}
        finally:
            stop(process)
        result = subprocess.run(
            [
                str(ROOT / "node_modules/.bin/wrangler"),
                "d1",
                "execute",
                "cartray-reviews-local-test",
                "--config",
                str(MODULE / "wrangler.test.toml"),
                "--local",
                "--persist-to",
                directory,
                "--json",
                "--command",
                "SELECT actor FROM native_audit WHERE action='approve'",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        rows = json.loads(result.stdout)[0]["results"]
        expected = "staff:" + hashlib.sha256((ISSUER + "\x00staff-1").encode()).hexdigest()
        assert rows == [{"actor": expected}], rows
        print("verified staff approval, permission separation and authenticated audit actor passed")


if __name__ == "__main__":
    main()
