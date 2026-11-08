"""Exercise staff evidence operations through Workerd service bindings and real local D1."""

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from test_staff_identity import AUDIENCE, ISSUER, KEY, token
from workerd_integration import MODULE, PORT, ROOT, start, stop

ORIGIN = "http://localhost:48974"
DELIVERY = "b" * 64
GENERATION = "a" * 64
BEARER = "private-reconciliation-token-0123456789"


def main():
    with tempfile.TemporaryDirectory(prefix="reviews-staff-provider-") as directory:
        temp = Path(directory)
        migrations = temp / "schema.sql"
        migrations.write_text("\n".join(path.read_text() for path in sorted((MODULE / "migrations").glob("*.sql"))))
        subprocess.run([
            str(ROOT / "node_modules/.bin/wrangler"), "d1", "execute", "cartray-reviews-local-test", "--config",
            str(MODULE / "wrangler.test.toml"), "--local", "--persist-to", directory, "--file", str(migrations),
        ], cwd=ROOT, check=True, capture_output=True)
        fixture = temp / "reconcile.mjs"
        fixture.write_text("""export default { async fetch(request) {
          if (request.headers.get('Authorization') !== 'Bearer private-reconciliation-token-0123456789')
            return new Response('', {status:401});
          const body=await request.json();
          if (body.acquisition_id !== 'purchase' || body.generation_id !== 'a'.repeat(64))
            return new Response('',{status:403});
          const url=new URL(request.url);
          if (url.pathname === '/discover') return Response.json({status:'unknown',reason:'no_match'});
          if (url.pathname === '/reconcile') return new Response('',{status:503});
          return new Response('',{status:404});
        }}""")
        service_config = temp / "service.toml"
        service_config.write_text(
            f'name = "reviews-test-reconcile"\nmain = "{fixture}"\ncompatibility_date = "2026-08-31"\n'
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            service_port = listener.getsockname()[1]
        service = subprocess.Popen(
            [
                str(ROOT / "node_modules/.bin/wrangler"),
                "dev",
                "--config",
                str(service_config),
                "--local",
                "--port",
                str(service_port),
                "--inspector-port",
                "0",
                "--log-level",
                "error",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            for _ in range(60):
                if service.poll() is not None:
                    raise RuntimeError("test service exited")
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{service_port}/", timeout=1)
                except urllib.error.HTTPError:
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError("test service did not start")
            config = temp / "reviews.toml"
            original = (MODULE / "wrangler.test.toml").read_text()
            original = original.replace('main = "src/worker.py"', f'main = "{MODULE / "src/worker.py"}"')
            original = original.replace('migrations_dir = "migrations"', f'migrations_dir = "{MODULE / "migrations"}"')
            config.write_text(
                original + '\n[[services]]\nbinding = "REVIEWS_RECONCILIATION"\nservice = "reviews-test-reconcile"\n'
            )

            def sql(statement):
                command = [
                    str(ROOT / "node_modules/.bin/wrangler"),
                    "d1",
                    "execute",
                    "cartray-reviews-local-test",
                    "--config",
                    str(config),
                    "--local",
                    "--persist-to",
                    directory,
                    "--json",
                    "--command",
                    statement,
                ]
                return json.loads(subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True).stdout)[
                    0
                ]["results"]

            sql(
                "INSERT INTO acquisition_invitations(store_key,environment,acquisition_id,source_event_id,"
                "recipient_context_hash,subject_set_hash,key_version,token_hash,expires_at) "
                "VALUES ('example-store','test','purchase','event','"
                + "1" * 64
                + "','"
                + "2" * 64
                + "','v1','"
                + "3" * 64
                + "',1)"
            )
            sql(
                "INSERT INTO invitation_generations VALUES ('example-store','test','purchase','"
                + GENERATION
                + "','request','"
                + "4" * 64
                + "','v1','"
                + "3" * 64
                + "',0,0,'"
                + DELIVERY
                + "','uncertain',1,NULL,NULL)"
            )
            sql(
                "INSERT INTO review_provider_identity_discoveries VALUES ('example-store','test','purchase','"
                + GENERATION
                + "','"
                + DELIVERY
                + "','smtp_1','delivered','2026-01-01T00:00:00Z',1)"
            )
            jwks = {"keys": [{"kty": "RSA", "kid": "key-1", "n": KEY["n"], "e": KEY["e"]}]}
            options = {
                "REVIEWS_STAFF_MODE": "cloudflare_access",
                "REVIEWS_STAFF_ISSUER": ISSUER,
                "REVIEWS_STAFF_AUDIENCE": AUDIENCE,
                "REVIEWS_STAFF_POLICY": json.dumps(
                    {"operator": ["reviews:operations"], "moderator": ["reviews:moderate"]}
                ),
                "REVIEWS_STAFF_TEST_JWKS": json.dumps(jwks),
                "REVIEWS_STAFF_CRYPTO": "webcrypto",
                "REVIEWS_STAFF_ORIGIN": ORIGIN,
                "RECONCILIATION_TOKEN": BEARER,
            }
            worker = start(str(config), directory, "collect", None, options)
            try:
                now = int(time.time())
                claims = {"iss": ISSUER, "aud": [AUDIENCE], "sub": "operator", "iat": now - 1, "exp": now + 120}
                ops, moderator = token(claims), token(claims | {"sub": "moderator"})
                base = f"http://127.0.0.1:{PORT}/reviews-admin/v1/deliveries/{DELIVERY}/"

                def post(action, body, assertion=ops, origin=ORIGIN):
                    request = urllib.request.Request(
                        base + action,
                        data=json.dumps(body).encode(),
                        method="POST",
                        headers={
                            "Content-Type": "application/json",
                            "Cf-Access-Jwt-Assertion": assertion,
                            "Origin": origin,
                        },
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=25) as response:
                            return response.status, json.loads(response.read())
                    except urllib.error.HTTPError as exc:
                        return exc.code, json.loads(exc.read())

                assert post("refresh", {}, assertion=moderator)[0] == 401
                assert post("refresh", {}, origin="https://evil.example")[0] == 403
                result = post("refresh", {})
                if result != (200, {"status": "unknown", "reason": "no_match"}):
                    raise AssertionError((result, sql("SELECT * FROM review_provider_operations_audit")))
                proof = {"provider_email_id": "smtp_1", "event_type": "delivered", "event_at": "2026-01-01T00:00:00Z"}
                assert post("acknowledge", proof)[0] == 200
                assert post("acknowledge", proof)[0] == 200
                assert post("acknowledge", proof | {"event_type": "rejected"})[0] == 409
                audit = sql(
                    "SELECT action,actor,count(*) AS n FROM review_provider_operations_audit "
                    "GROUP BY action,actor ORDER BY action"
                )
                actor = "staff:" + hashlib.sha256((ISSUER + "\0operator").encode()).hexdigest()
                assert audit == [
                    {"action": "evidence_acknowledged", "actor": actor, "n": 1},
                    {"action": "refresh_requested", "actor": actor, "n": 1},
                ]
                assert sql("SELECT state FROM invitation_generations")[0]["state"] == "uncertain"
                sql(
                    "INSERT INTO review_provider_deliveries VALUES ('example-store','test','purchase','"
                    + GENERATION
                    + "','"
                    + DELIVERY
                    + "','smtp2go','smtp_1',1)"
                )
                assert post("refresh", {})[0] == 503
                assert (
                    sql("SELECT count(*) AS n FROM review_provider_operations_audit WHERE action='refresh_requested'")[
                        0
                    ]["n"]
                    == 2
                )
                print("Staff provider service binding, D1 audit, authorization, origin, uncertainty and failure passed")
            finally:
                stop(worker)
        finally:
            os.killpg(service.pid, 15)
            service.wait(timeout=10)


if __name__ == "__main__":
    main()
