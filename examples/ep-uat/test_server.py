#!/usr/bin/env python3
"""Loopback-only Stripe test checkout and dummy-download qualification server.

This is a test harness, not a production fulfilment or delivery implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from delivery_email import TEST_BODY, TEST_SUBJECT, TestDelivery
from prepare import APPROVED_SHA256, prepare, read_archive
from provision_test_prices import amount_minor, dummy_followups

from cartray.canonical import CanonicalItem
from cartray.catalogue import CsvCatalogueSourceAdapter, build_catalogue_from_source
from cartray.checkout import CheckoutService
from cartray.models import CHECKOUT_REQUEST_ID_RE, CheckoutRequest
from cartray.post_purchase import Action, SqliteDispatcher
from cartray.reconciliation import StripeReconciliationService
from cartray.store import ReconciliationCandidate, SqliteOrderStore
from cartray.stripe import (
    CheckoutMetadataSealer,
    CheckoutMetadataVerifier,
    StripeApiClient,
    StripeCheckoutGateway,
    StripeCheckoutSessionRetriever,
    StripePriceResolver,
)


class Network:
    async def request(self, method, path, *, headers, body=None):
        def send():
            request = urllib.request.Request(
                "https://api.stripe.com" + path,
                data=body.encode() if body else None,
                headers=dict(headers),
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        return await asyncio.to_thread(send)


class Ed25519:
    def __init__(self, directory):
        self.directory = directory
        self.private = directory / "uat-ed25519.pem"
        self.public = directory / "uat-ed25519-public.pem"
        if not self.private.exists():
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.private)],
                check=True,
                capture_output=True,
            )
            os.chmod(self.private, 0o600)
            subprocess.run(
                ["openssl", "pkey", "-in", str(self.private), "-pubout", "-out", str(self.public)],
                check=True,
                capture_output=True,
            )
        if not self.public.is_file():
            raise ValueError("test signing public key missing")

    async def sign(self, payload):
        with tempfile.NamedTemporaryFile(dir=self.directory) as source:
            source.write(payload)
            source.flush()
            result = subprocess.run(
                ["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(self.private), "-in", source.name],
                check=True,
                capture_output=True,
            )
        return result.stdout

    async def verify(self, payload, signature):
        with (
            tempfile.NamedTemporaryFile(dir=self.directory) as source,
            tempfile.NamedTemporaryFile(dir=self.directory) as sig,
        ):
            source.write(payload)
            source.flush()
            sig.write(signature)
            sig.flush()
            result = subprocess.run(
                [
                    "openssl",
                    "pkeyutl",
                    "-verify",
                    "-rawin",
                    "-pubin",
                    "-inkey",
                    str(self.public),
                    "-in",
                    source.name,
                    "-sigfile",
                    sig.name,
                ],
                capture_output=True,
            )
        return result.returncode == 0


class TestLane:
    def __init__(self, fixture, key, port, archive, no_return=False):
        self.fixture = fixture
        self.lock = threading.RLock()
        self.port = port
        mapping = json.loads((fixture / "test-price-map.json").read_text())
        if not key.startswith(("sk_test_", "rk_test_")) or mapping["livemode"] is not False:
            raise ValueError("test-only Stripe credentials and fixture required")
        raw = archive.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        if source_hash != APPROVED_SHA256 or mapping.get("source_archive_sha256") != source_hash:
            raise ValueError("test mapping does not match the approved archive")
        blobs, _manifest = read_archive(raw)
        source = prepare(blobs, source_hash)
        source_offers = {p["source_id"]: p for p in source["products"]}
        if not 1 <= len(mapping["offers"]) <= 2:
            raise ValueError("invalid test offer count")
        for row in mapping["offers"]:
            offer = source_offers.get(row["source_id"])
            if (
                not offer
                or offer["source_type"] != "simple"
                or offer["source_delivery_kind"] != "uat_dummy_transfer"
                or row["source_key"] != offer["key"]
                or row["source_sku"] != offer["source_sku"]
                or row["amount_minor"] != amount_minor(offer["source_price"])
                or row["currency"] != "aud"
                or row["cartray_product_key"] != f"EP-UAT-{row['source_id']}"
                or row["dummy_entitlement"] != f"EP-UAT-DUMMY-{row['source_id']}"
                or row["lookup_key"] != f"ep_uat_woo_{row['source_id']}_{source_hash[:12]}"
            ):
                raise ValueError("test fixture conflicts with approved source")
        self.mapping = {str(x["source_id"]): x for x in mapping["offers"]}
        self.reverse = {x["cartray_product_key"]: x for x in mapping["offers"]}
        if len(self.mapping) != len(mapping["offers"]) or len(self.reverse) != len(mapping["offers"]):
            raise ValueError("duplicate test fixture identity")
        self.followups = json.loads((fixture / "dummy-followups.json").read_text())
        if self.followups != {
            "send_enabled": False,
            "items": dummy_followups(blobs, source, [x["source_id"] for x in mapping["offers"]]),
        }:
            raise ValueError("follow-up sending must remain disabled")
        self.file = (fixture / "dummy-download.txt").read_bytes()
        if self.file != b"CartRay Effective Policy UAT dummy file. This contains no paid materials.\n":
            raise ValueError("invalid dummy payload")
        self.connection = sqlite3.connect(fixture / "uat-order-test.sqlite", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.store = SqliteOrderStore(self.connection)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS uat_browsers(
                token_hash TEXT PRIMARY KEY, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS uat_order_owner(
                order_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uat_checkout_owner(
                request_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uat_grants(
                order_id TEXT NOT NULL, product_key TEXT NOT NULL, quantity INTEGER NOT NULL,
                PRIMARY KEY(order_id, product_key));
            CREATE TABLE IF NOT EXISTS uat_settlement_origins(
                order_id TEXT PRIMARY KEY, origin TEXT NOT NULL);
        """)
        if not self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_purchase_executions'"
        ).fetchone():
            migration = Path(__file__).resolve().parents[2] / "migrations/0006_post_purchase_actions.sql"
            self.connection.executescript(
                "BEGIN;\n" + migration.read_text()
                + "\nINSERT INTO post_purchase_installation VALUES (1,'effective-policy-uat','test');\nCOMMIT;"
            )
        self.crypto = Ed25519(fixture)
        self.client = StripeApiClient(key, Network())
        account = asyncio.run(self.client.get("/v1/account", {}))
        if account.get("id") != mapping["stripe_account"]:
            raise ValueError("Stripe test account mismatch")
        self.catalogue = asyncio.run(
            build_catalogue_from_source(
                CsvCatalogueSourceAdapter(fixture / "catalogue.csv"),
                StripePriceResolver(self.client),
                {k: tuple(v) for k, v in json.loads((fixture / "fulfilment-expansions.json").read_text()).items()},
            )
        )
        for row in mapping["offers"]:
            remote = asyncio.run(self.client.get(f"/v1/prices/{row['stripe_price_id']}", {}))
            if (
                remote.get("livemode") is not False
                or remote.get("active") is not True
                or remote.get("type") != "one_time"
                or remote.get("recurring") is not None
                or remote.get("lookup_key") != row["lookup_key"]
                or remote.get("metadata", {}).get("uat_source_key") != row["source_key"]
                or remote.get("metadata", {}).get("uat_source_sha256") != source_hash
            ):
                raise ValueError("Stripe price provenance mismatch")
            resolved = self.catalogue.product(row["cartray_product_key"]).price
            if (resolved.stripe_price_id, resolved.amount_minor, resolved.currency) != (
                row["stripe_price_id"],
                row["amount_minor"],
                row["currency"],
            ):
                raise ValueError("runtime Stripe prices differ from verified test fixture")
            product = self.catalogue.product(row["cartray_product_key"])
            if product.source.stripe_lookup_key != row["lookup_key"] or product.fulfilment_resources != (
                row["dummy_entitlement"],
            ):
                raise ValueError("runtime fulfilment mapping differs from verified test fixture")
        if set(self.catalogue.products) != set(self.reverse):
            raise ValueError("runtime catalogue has unexpected products")
        self.checkout = CheckoutService(
            self.catalogue,
            self.store,
            StripeCheckoutGateway(self.client, CheckoutMetadataSealer("test", "ep-uat-local", self.crypto)),
            success_url=f"http://localhost:{port + 1}/" if no_return else f"http://localhost:{port}/uat-return/",
            cancel_url=f"http://localhost:{port}/cart/",
        )
        self.native_checkout = CheckoutService(
            self.catalogue,
            self.store,
            StripeCheckoutGateway(self.client, CheckoutMetadataSealer("test", "ep-uat-local", self.crypto)),
            success_url=(f"http://localhost:{port + 1}/" if no_return else
                         f"http://localhost:{port}/purchase-return/?checkout=complete"),
            cancel_url=f"http://localhost:{port}/purchase-return/?checkout=cancelled",
        )
        self.reconciler = StripeReconciliationService(
            self.store,
            StripeCheckoutSessionRetriever(self.client),
            CheckoutMetadataVerifier("test", {"ep-uat-local": self.crypto}),
        )

    def browser(self, token):
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = self.connection.execute(
            "SELECT token_hash,created FROM uat_browsers WHERE token_hash=?", (digest,)
        ).fetchone()
        return digest if row and row["created"] > int(time.time()) - 86400 else None

    def create_browser(self):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connection:
            self.connection.execute("INSERT INTO uat_browsers VALUES (?,?)", (digest, int(time.time())))
        return token

    def start(self, owner, payload, *, native=False):
        if (
            set(payload) != {"request_id", "items"}
            or not isinstance(payload["items"], list)
            or not 1 <= len(payload["items"]) <= 2
        ):
            raise ValueError("invalid request")
        items = []
        for item in payload["items"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"source_id", "quantity"}
                or str(item["source_id"]) not in self.mapping
            ):
                raise ValueError("offer not enabled for test checkout")
            quantity = item["quantity"]
            if type(quantity) is not int or not 1 <= quantity <= 10:
                raise ValueError("invalid quantity")
            items.append(CanonicalItem(self.mapping[str(item["source_id"])]["cartray_product_key"], quantity))
        if not isinstance(payload["request_id"], str) or not CHECKOUT_REQUEST_ID_RE.fullmatch(payload["request_id"]):
            raise ValueError("invalid request identity")
        request = CheckoutRequest(payload["request_id"], self.catalogue.version, tuple(items))
        # Bind request ownership before the external Stripe operation, including
        # the uncertain-response and process-restart paths.
        with self.connection:
            prior = self.connection.execute(
                "SELECT token_hash FROM uat_checkout_owner WHERE request_id=?", (payload["request_id"],)
            ).fetchone()
            if prior and not hmac.compare_digest(prior["token_hash"], owner):
                raise ValueError("checkout request already bound")
            self.connection.execute(
                "INSERT OR IGNORE INTO uat_checkout_owner VALUES (?,?)", (payload["request_id"], owner)
            )
        redirect = asyncio.run((self.native_checkout if native else self.checkout).checkout(request))
        row = self.connection.execute(
            "SELECT order_id FROM checkout_sessions WHERE external_session_id=?", (redirect.session_id,)
        ).fetchone()
        with self.connection:
            existing = self.connection.execute(
                "SELECT token_hash FROM uat_order_owner WHERE order_id=?", (row["order_id"],)
            ).fetchone()
            if existing and not hmac.compare_digest(existing["token_hash"], owner):
                raise ValueError("order already bound")
            self.connection.execute("INSERT OR IGNORE INTO uat_order_owner VALUES (?,?)", (row["order_id"], owner))
        return {"url": redirect.url, "session_id": redirect.session_id}

    def start_native(self, owner, payload):
        """Use the public CartRay checkout ABI while retaining UAT purchase authority."""
        if not isinstance(payload, dict) or set(payload) != {"checkout_request_id", "manifest_version", "items"}:
            raise ValueError("invalid checkout request")
        if payload["manifest_version"] != self.catalogue.version or not isinstance(payload["items"], list):
            raise ValueError("checkout catalogue version mismatch")
        items = []
        for item in payload["items"]:
            if not isinstance(item, dict) or set(item) != {"product_key", "quantity"}:
                raise ValueError("invalid checkout item")
            mapping = self.reverse.get(item["product_key"])
            if not mapping:
                raise ValueError("offer not enabled for test checkout")
            items.append({"source_id": mapping["source_id"], "quantity": item["quantity"]})
        redirect = self.start(owner, {"request_id": payload["checkout_request_id"], "items": items}, native=True)
        return {"checkout_url": redirect["url"], "session_id": redirect["session_id"]}

    def _owned_checkout(self, owner, session_id, *, confirmed=False):
        row = self.connection.execute(
            """SELECT o.checkout_request_id FROM checkout_sessions s JOIN orders o USING(order_id)
            JOIN uat_order_owner u USING(order_id)
            WHERE s.external_session_id=? AND u.token_hash=? AND (?=0 OR s.settlement_state='confirmed')""",
            (session_id, owner, int(confirmed)),
        ).fetchone()
        return row

    def status_native(self, owner, session_id):
        if not isinstance(session_id, str):
            raise ValueError("invalid session")
        row = self._owned_checkout(owner, session_id)
        if not row:
            raise ValueError("checkout not bound to browser")
        return {"state": self.reconcile(owner, row["checkout_request_id"])["state"]}

    def current_grants(self, owner, session_id):
        row = self._owned_checkout(owner, session_id, confirmed=True)
        if not row:
            raise ValueError("confirmed checkout not bound to browser")
        return self.grants(owner, row["checkout_request_id"])

    def reconcile(self, owner, request_id, origin="browser"):
        if not isinstance(request_id, str) or not CHECKOUT_REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("invalid request identity")
        bound = self.connection.execute(
            "SELECT token_hash FROM uat_checkout_owner WHERE request_id=?", (request_id,)
        ).fetchone()
        if not bound or not hmac.compare_digest(bound["token_hash"], owner):
            raise ValueError("checkout request not bound to browser")
        now = int(time.time())
        candidates = []
        with self.connection:
            rows = self.connection.execute(
                """SELECT s.order_id,s.external_session_id FROM checkout_sessions s
                JOIN uat_order_owner u USING(order_id) JOIN orders o USING(order_id)
                LEFT JOIN checkout_reconciliations r USING(order_id)
                WHERE u.token_hash=? AND o.checkout_request_id=? AND s.settlement_state='pending'
                  AND s.external_session_id IS NOT NULL
                  AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=?)
                  AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=?) ORDER BY s.order_id LIMIT 10""",
                (owner, request_id, now, now),
            ).fetchall()
            for row in rows:
                order_id, session_id = row["order_id"], row["external_session_id"]
                lease = secrets.token_hex(16)
                self.connection.execute(
                    """INSERT OR IGNORE INTO checkout_reconciliations
                    (order_id,session_id,next_attempt_at,updated_at) VALUES (?,?,?,?)""",
                    (order_id, session_id, now, now),
                )
                updated = self.connection.execute(
                    """UPDATE checkout_reconciliations SET attempt_count=attempt_count+1,
                    lease_token=?,lease_expires_at=?,last_attempt_at=?,updated_at=?
                    WHERE order_id=? AND session_id=? AND next_attempt_at<=?
                    AND (lease_expires_at IS NULL OR lease_expires_at<=?)""",
                    (lease, now + 300, now, now, order_id, session_id, now, now),
                )
                if updated.rowcount:
                    count = self.connection.execute(
                        "SELECT attempt_count FROM checkout_reconciliations WHERE order_id=?", (order_id,)
                    ).fetchone()["attempt_count"]
                    candidates.append(ReconciliationCandidate(order_id, session_id, lease, count))
        for candidate in candidates:
            outcome = asyncio.run(self.reconciler._reconcile_candidate(candidate, now=now))
            if outcome == "confirmed":
                with self.connection:
                    self.connection.execute("INSERT OR IGNORE INTO uat_settlement_origins VALUES (?,?)",
                                            (candidate.order_id, origin))
            # In this local UAT only, permit bounded retry after open or transient outcomes.
            with self.connection:
                self.connection.execute(
                    """UPDATE checkout_reconciliations SET next_attempt_at=?
                    WHERE order_id=? AND last_outcome IN ('open','transient_error') AND lease_token IS NULL""",
                    (now + 5, candidate.order_id),
                )
        with self.connection:
            rows = self.connection.execute(
                """SELECT i.order_id,i.product_key,i.quantity,i.fulfilment_resources_json
                FROM order_items i JOIN uat_order_owner u USING(order_id) JOIN checkout_sessions s USING(order_id)
                WHERE u.token_hash=? AND s.settlement_state='confirmed' AND EXISTS (
                SELECT 1 FROM outbox o WHERE o.order_id=i.order_id AND o.event_type='OrderConfirmed')""",
                (owner,),
            ).fetchall()
            for row in rows:
                expected = self.reverse.get(row["product_key"])
                if not expected or json.loads(row["fulfilment_resources_json"]) != [expected["dummy_entitlement"]]:
                    raise ValueError("immutable resource snapshot mismatch")
                self.connection.execute(
                    "INSERT OR IGNORE INTO uat_grants VALUES (?,?,?)",
                    (row["order_id"], row["product_key"], row["quantity"]),
                )
        # Optional test dispatcher never runs provider work under the checkout lock.
        if getattr(self, "test_delivery_config", None):
            order = self.connection.execute(
                """SELECT s.order_id FROM checkout_sessions s JOIN orders o USING(order_id)
                JOIN uat_order_owner u USING(order_id)
                WHERE o.checkout_request_id=? AND u.token_hash=? AND s.settlement_state='confirmed'
                  AND EXISTS (SELECT 1 FROM outbox x WHERE x.order_id=s.order_id AND x.event_type='OrderConfirmed')""",
                (request_id, owner),
            ).fetchone()
            if order:
                threading.Thread(target=self.send_test_delivery, args=(order["order_id"],), daemon=True).start()
        state = self.connection.execute(
            """SELECT s.settlement_state FROM checkout_sessions s
            JOIN orders o USING(order_id) JOIN uat_order_owner u USING(order_id)
            WHERE o.checkout_request_id=? AND u.token_hash=?""", (request_id, owner)
        ).fetchone()
        return {"state": state["settlement_state"] if state else "pending", "grants": self.grants(owner, request_id)}

    def run_test_automation_once(self):
        """Drive local sandbox reconciliation independently of the purchase browser."""
        with closing(sqlite3.connect(self.fixture / "uat-order-test.sqlite", timeout=5)) as connection:
            lane = copy.copy(self)
            lane.connection = connection
            lane.store = SqliteOrderStore(connection)
            lane.reconciler = StripeReconciliationService(
                lane.store, StripeCheckoutSessionRetriever(lane.client),
                CheckoutMetadataVerifier("test", {"ep-uat-local": lane.crypto}),
            )
            source_keys = {row["source_key"]: row["cartray_product_key"] for row in self.mapping.values()}
            pairs = sorted({(source_keys[x["source_key"]], x["workflow_key"]) for x in self.followups["items"]})
            missing = " OR ".join(
                "(i.product_key=? AND NOT EXISTS (SELECT 1 FROM post_purchase_executions p "
                "WHERE p.order_id=o.order_id AND p.workflow_key=?))" for _ in pairs
            ) or "0"
            parameters = [value for pair in pairs for value in pair]
            rows = connection.execute(
                """SELECT COALESCE(u.token_hash,c.token_hash) AS owner,o.order_id,o.checkout_request_id
                FROM orders o JOIN checkout_sessions s USING(order_id)
                LEFT JOIN uat_order_owner u USING(order_id)
                JOIN uat_checkout_owner c ON c.request_id=o.checkout_request_id
                LEFT JOIN checkout_reconciliations r USING(order_id)
                WHERE s.external_session_id IS NOT NULL AND (u.token_hash IS NULL OR u.token_hash=c.token_hash) AND
                (s.settlement_state='pending' AND (r.next_attempt_at IS NULL OR r.next_attempt_at<=?)
                    AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=?) OR
                 s.settlement_state='confirmed' AND NOT EXISTS
                    (SELECT 1 FROM uat_delivery_emails e WHERE e.order_id=o.order_id) AND EXISTS
                    (SELECT 1 FROM order_items i WHERE i.order_id=o.order_id AND (""" + missing + """)))
                ORDER BY COALESCE(r.next_attempt_at,0),o.created_at LIMIT 10""",
                [int(time.time()), int(time.time()), *parameters],
            ).fetchall()
            for row in rows:
                try:
                    with connection:
                        connection.execute("INSERT OR IGNORE INTO uat_order_owner VALUES (?,?)",
                                           (row["order_id"], row["owner"]))
                    lane.reconcile(row["owner"], row["checkout_request_id"], origin="background")
                except Exception:
                    continue  # A transient failure cannot stop later scheduled attempts.

    def run_test_automation(self):
        while True:
            try:
                self.run_test_automation_once()
            except Exception:
                pass  # The isolated test runner retries without impacting checkout.
            time.sleep(10)

    def send_test_delivery(self, order_id):
        try:
            with closing(sqlite3.connect(self.fixture / "uat-order-test.sqlite", timeout=5)) as connection:
                # Preserve the already sent order-level UAT claim on upgrade.
                if connection.execute("SELECT 1 FROM uat_delivery_emails WHERE order_id=?", (order_id,)).fetchone():
                    return
                mail = TestDelivery(connection, self.client, *self.test_delivery_config)
                source_keys = {row["source_key"]: row["cartray_product_key"] for row in self.mapping.values()}
                workflow_links = {}
                workflow_offers = {}
                for item in self.followups["items"]:
                    key = item["workflow_key"]
                    workflow_links.setdefault(key, set()).add(item["dummy_url"])
                    workflow_offers.setdefault(key, set()).add(source_keys[item["source_key"]])
                actions = tuple(
                    Action(
                        key, tuple(sorted(offers)), "transactional_email", key,
                        hashlib.sha256(json.dumps(
                            [TEST_SUBJECT, TEST_BODY, "uat@mitchellcurrie.com", "test-1034@mitchellcurrie.com",
                             sorted(workflow_links[key])],
                            separators=(",", ":"),
                        ).encode()).hexdigest(),
                    )
                    for key, offers in workflow_offers.items()
                )
                dispatcher = SqliteDispatcher(
                    connection, "effective-policy-uat", "test",
                    {"transactional_email": lambda purchase, execution: mail.send_verified(
                        purchase.session_id, sorted(workflow_links[execution.config_ref])
                    )},
                )
                dispatcher.dispatch(order_id, actions)
        except Exception:
            pass  # Provider failures must not affect checkout or expose private details.

    def grants(self, owner, request_id=None):
        rows = self.connection.execute(
            """SELECT g.product_key,g.quantity FROM uat_grants g
            JOIN uat_order_owner u USING(order_id) JOIN checkout_sessions s USING(order_id)
            JOIN orders o USING(order_id)
            WHERE u.token_hash=? AND s.settlement_state='confirmed'
              AND (? IS NULL OR o.checkout_request_id=?) ORDER BY g.product_key""",
            (owner, request_id, request_id),
        ).fetchall()
        result = []
        for row in rows:
            mapping = self.reverse[row["product_key"]]
            followups = [
                x["dummy_url"]
                for x in self.followups["items"]
                if x["source_key"] == mapping["source_key"] and not x["send_enabled"]
            ]
            result.append({"source_id": mapping["source_id"], "quantity": row["quantity"], "followups": followups})
        return result


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, lane=None, **kwargs):
        self.lane = lane
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):
        pass  # URLs and test capabilities must not enter access logs.

    def token(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie["ep_uat_context"].value
        except (KeyError, ValueError):
            return None

    def json_response(self, status, value, cookie=None):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header(
                "Set-Cookie", f"ep_uat_context={cookie}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=86400"
            )
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.headers.get("Origin") != f"http://localhost:{self.lane.port}":
            return self.json_response(403, {"error": "forbidden"})
        with self.lane.lock:
            owner = self.lane.browser(self.token())
            if not owner:
                return self.json_response(403, {"error": "forbidden"})
            try:
                if self.path in ("/__uat/checkout", "/checkout"):
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("invalid request")
                    payload = json.loads(self.rfile.read(length))
                    if self.path == "/checkout":
                        result = self.lane.start_native(owner, payload)
                    else:
                        result = self.lane.start(owner, payload)
                    return self.json_response(200, result)
                if self.path == "/__uat/reconcile":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("invalid request")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) != {"request_id"}:
                        raise ValueError("invalid request")
                    return self.json_response(200, self.lane.reconcile(owner, payload["request_id"]))
                return self.json_response(404, {"error": "not found"})
            except Exception:
                # This test harness may have reached Stripe before an error.
                # Do not label an unknown outcome as a definitive rejection.
                return self.json_response(503, {"error": "UAT checkout or confirmation unavailable"})

    def do_GET(self):
        path = urlsplit(self.path).path
        with self.lane.lock:
            owner = self.lane.browser(self.token())
            if path == "/__uat/context":
                if owner:
                    return self.json_response(200, {"ready": True})
                token = self.lane.create_browser()
                return self.json_response(200, {"ready": True}, cookie=token)
            if path == "/catalogue":
                return self.json_response(200, self.lane.catalogue.public_manifest())
            if path == "/checkout-status":
                if not owner:
                    return self.json_response(404, {"error": "not found"})
                requested = parse_qs(urlsplit(self.path).query).get("session_id", [])
                try:
                    if len(requested) != 1:
                        raise ValueError("invalid session")
                    return self.json_response(200, self.lane.status_native(owner, requested[0]))
                except Exception:
                    return self.json_response(404, {"error": "not found"})
            if path == "/__uat/current-grants":
                if not owner:
                    return self.json_response(404, {"error": "not found"})
                requested = parse_qs(urlsplit(self.path).query).get("session_id", [])
                if len(requested) != 1:
                    return self.json_response(404, {"error": "not found"})
                try:
                    grants = self.lane.current_grants(owner, requested[0])
                    for grant in grants:
                        key = self.lane.mapping[str(grant["source_id"])]["cartray_product_key"]
                        grant["title"] = self.lane.catalogue.product(key).source.title
                    return self.json_response(200, {"grants": grants})
                except ValueError:
                    return self.json_response(404, {"error": "not found"})
            if path == "/__uat/grants":
                if not owner:
                    return self.json_response(403, {"error": "forbidden"})
                return self.json_response(200, {"grants": self.lane.grants(owner)})
            if path == "/__uat/download":
                if not owner:
                    return self.json_response(404, {"error": "not found"})
                requested = parse_qs(urlsplit(self.path).query).get("source_id", [])
                if len(requested) != 1 or not any(str(x["source_id"]) == requested[0] for x in self.lane.grants(owner)):
                    return self.json_response(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="cartray-uat-dummy.txt"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(self.lane.file)))
                self.end_headers()
                self.wfile.write(self.lane.file)
                return
        return super().do_GET()


def read_secret(key_file, keychain_service, site):
    if key_file:
        if key_file.stat().st_mode & 0o077 or key_file.resolve().is_relative_to(site):
            raise ValueError("test key must be private and outside the site")
        return key_file.read_text().strip()
    if keychain_service:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", keychain_service, "-w"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValueError("test credential not available in the configured Keychain item") from error
        return result.stdout.strip()
    return None


class NoReturnHandler(SimpleHTTPRequestHandler):
    """A separate local landing for Stripe redirects; it never reconciles an order."""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"Test payment complete. You may close this page.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    stripe_secret = parser.add_mutually_exclusive_group(required=True)
    stripe_secret.add_argument("--key-file", type=Path)
    stripe_secret.add_argument("--stripe-keychain-service")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--port", type=int, default=45984)
    parser.add_argument("--no-return-test", action="store_true", help="Return test checkout outside the UAT store")
    mail_secret = parser.add_mutually_exclusive_group()
    mail_secret.add_argument("--smtp2go-key-file", type=Path, help="Enable the single-recipient test delivery")
    mail_secret.add_argument("--smtp2go-keychain-service", help="Enable test delivery using macOS Keychain")
    args = parser.parse_args()
    site = args.site.resolve(strict=True)
    fixture = args.fixture.resolve(strict=True)
    if not site.is_dir() or site.is_relative_to(fixture) or fixture.is_relative_to(site):
        raise ValueError("static site and private test fixture must be separate")
    if args.archive.resolve().is_relative_to(site):
        raise ValueError("credentials and source archive must not be inside the static site")
    if any(path.is_symlink() for path in site.rglob("*")):
        raise ValueError("static site must not contain symlinks")
    lane = TestLane(
        fixture, read_secret(args.key_file, args.stripe_keychain_service, site), args.port, args.archive,
        no_return=args.no_return_test,
    )
    mail_key = read_secret(args.smtp2go_key_file, args.smtp2go_keychain_service, site)
    if mail_key:
        lane.test_delivery_config = (mail_key, "uat@mitchellcurrie.com", "test-1034@mitchellcurrie.com")
        TestDelivery(lane.connection, lane.client, *lane.test_delivery_config)
        threading.Thread(target=lane.run_test_automation, daemon=True).start()
    if args.no_return_test:
        neutral = ThreadingHTTPServer(("127.0.0.1", args.port + 1), NoReturnHandler)
        threading.Thread(target=neutral.serve_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(site), lane=lane))
    print(f"Local-only UAT server on http://localhost:{args.port}; test email is restricted and opt-in.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
