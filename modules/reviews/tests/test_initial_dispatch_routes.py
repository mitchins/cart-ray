"""Exercise initial invitation HTTP authorization against actual local SQL."""

import asyncio
import importlib.util
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import test_native_lifecycle as native
from cartray_reviews.automation_initial import unsubscribe_token
from test_automation_intake import run, setup
from test_automation_recipient import RECIPIENT, bind, consent

lifecycle = native.lifecycle


def environment(checkout, reviews):
    return SimpleNamespace(
        REVIEWS_DB=reviews, CARTRAY_DB=checkout, REVIEWS_STORE_KEY="store", REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only", REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40, REVIEWS_INVITATION_KEY="c" * 40, REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_AUTOMATION_INGRESS_TOKEN="e" * 40, REVIEWS_CONSENT_SECRET="f" * 40,
        REVIEWS_DELIVERY_TOKEN="g" * 40, REVIEWS_RECIPIENT_EVIDENCE_SECRET="h" * 40,
        REVIEWS_UNSUBSCRIBE_KEY="i" * 40, REVIEWS_AUTOMATION_INTAKE="test", REVIEWS_AUTOMATION_SENDING="test",
        REVIEWS_SUBJECT_MAPS=json.dumps({"v1": {"product_1": "policy"}}),
    )


def request(lifecycle, env, endpoint, data, token=None):
    req = SimpleNamespace(method="POST", headers={"Authorization": "Bearer " + (token or "e" * 40)},
                          data=data, body=None)
    with patch.object(lifecycle.time, "time", return_value=120):
        return asyncio.run(lifecycle.handle_write(req, env, endpoint,))


PREPARE = "/reviews-internal/v1/automation-initial-prepare"
CLAIM = "/reviews-internal/v1/automation-initial-claim"


def test_initial_prepare_claim_and_retry_are_authorized_and_scoped(lifecycle):
    checkout, reviews = setup()
    run(checkout, reviews)
    reviews.db.execute("INSERT INTO review_subjects VALUES ('store','test','policy','product','/policy/')")
    reviews.db.commit()
    bind(reviews, data={"outbox_id": 10, "recipient_context_hash": RECIPIENT, "binding_version": "v2",
                        "evidence_reference": "contact", "verified_at": 100})
    consent(checkout, reviews)
    env = environment(checkout, reviews)
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10}, "wrong").status == 401
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10, "recipient": "attacker"}).status == 400
    prepared = request(lifecycle, env, PREPARE, {"outbox_id": 10})
    assert prepared.status == 200
    assert prepared.body["recipient_context_hash"] == RECIPIENT
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10}).body == prepared.body
    claim = {"outbox_id": 10, "acquisition_id": "order_1", "generation_id": prepared.body["generation_id"],
             "delivery_identity": prepared.body["delivery_identity"]}
    assert request(lifecycle, env, CLAIM, claim | {"acquisition_id": "other"}).status == 409
    assert request(lifecycle, env, CLAIM, claim | {"generation_id": "0" * 64}).status == 409
    assert request(lifecycle, env, CLAIM, claim).body == {"state": "dispatching"}
    assert reviews.db.execute("SELECT COUNT(*) FROM invitation_generations").fetchone()[0] == 1
    assert reviews.db.execute("SELECT COUNT(*) FROM native_reviews").fetchone()[0] == 0


def test_initial_route_rejects_disabled_sender_missing_evidence_and_suppression(lifecycle):
    checkout, reviews = setup()
    run(checkout, reviews)
    env = environment(checkout, reviews)
    env.REVIEWS_AUTOMATION_SENDING = "disabled"
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10}).status == 503
    env.REVIEWS_AUTOMATION_SENDING = "test"
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10}).status == 409
    bind(reviews, data={"outbox_id": 10, "recipient_context_hash": RECIPIENT, "binding_version": "v2",
                        "evidence_reference": "contact", "verified_at": 100})
    consent(checkout, reviews)
    reviews.db.execute("INSERT INTO review_recipient_suppressions VALUES "
                       "('store','test',?,'review_invitation',100)", (RECIPIENT,))
    reviews.db.commit()
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10}).body == {"status": "suppressed"}
    assert reviews.db.execute("SELECT COUNT(*) FROM invitation_generations").fetchone()[0] == 0


def test_initial_prepare_fails_closed_on_key_or_trusted_source_mismatch(lifecycle):
    checkout, reviews = setup()
    run(checkout, reviews)
    reviews.db.execute("INSERT INTO review_subjects VALUES ('store','test','policy','product','/policy/')")
    reviews.db.commit()
    bind(reviews, data={"outbox_id": 10, "recipient_context_hash": RECIPIENT, "binding_version": "v2",
                        "evidence_reference": "contact", "verified_at": 100})
    consent(checkout, reviews)
    env = environment(checkout, reviews)
    env.REVIEWS_UNSUBSCRIBE_KEY = env.REVIEWS_INVITATION_KEY
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10}).status == 409
    env.REVIEWS_UNSUBSCRIBE_KEY = "i" * 40
    env.REVIEWS_SUBJECT_MAPS = "not-json"
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10}).status == 409
    env.REVIEWS_SUBJECT_MAPS = json.dumps({"v1": {"product_1": "policy"}})
    checkout.db.execute("DELETE FROM outbox WHERE id=10")
    checkout.db.commit()
    assert request(lifecycle, env, PREPARE, {"outbox_id": 10}).status == 409
    assert reviews.db.execute("SELECT COUNT(*) FROM invitation_generations").fetchone()[0] == 0


def test_unsubscribe_endpoint_requires_isolated_key_and_records_real_suppression(lifecycle):
    checkout, reviews = setup()
    run(checkout, reviews)
    bind(reviews, data={"outbox_id": 10, "recipient_context_hash": RECIPIENT, "binding_version": "v2",
                        "evidence_reference": "contact", "verified_at": 100})
    consent(checkout, reviews)
    env = environment(checkout, reviews)
    workers = types.ModuleType("workers")
    workers.Response = native.TestResponse
    workers.WorkerEntrypoint = object
    spec = importlib.util.spec_from_file_location("unsubscribe_reviews_worker", native.MODULE / "src/worker.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"workers": workers, "native_lifecycle": lifecycle}):
        spec.loader.exec_module(module)
    instance = module.Default()
    instance.env = env
    path = "https://reviews.example/reviews/v1/invitation-unsubscribe"

    def fetch(method="POST", data=None, url=path):
        req = SimpleNamespace(method=method, url=url, data=data or {}, body=None)
        return asyncio.run(instance.fetch(req))

    assert fetch("GET").status == 405
    assert fetch(url=path + "?token=bad").status == 405
    env.REVIEWS_UNSUBSCRIBE_KEY = env.REVIEWS_INVITATION_KEY
    assert fetch(data={"token": "bad"}).status == 503
    env.REVIEWS_UNSUBSCRIBE_KEY = "i" * 40
    assert fetch(data={"token": "bad"}).status == 409
    assert fetch(data={"token": "bad", "extra": True}).status == 409
    token = unsubscribe_token(secret=b"i" * 40, store="store", environment="test", outbox_id=10)
    env.REVIEWS_MODE = "disabled"
    assert fetch(data={"token": token}).body == {"status": "unsubscribed"}
    assert fetch(data={"token": token}).body == {"status": "unsubscribed"}
    assert reviews.db.execute("SELECT COUNT(*) FROM review_recipient_suppressions").fetchone()[0] == 1
    assert reviews.db.execute("SELECT revoked_at FROM review_invitation_consents").fetchone()[0] is not None
