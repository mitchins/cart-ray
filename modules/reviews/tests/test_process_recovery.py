"""Exercise recovery from a bearer-only request to the trusted local mailbox."""

import asyncio
import hashlib
import importlib.util
import json
import mailbox
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cartray_reviews.invitations import revoke_purchase
from test_acquisition_invitations import issue, stores
from test_native_lifecycle import invoke
from test_native_lifecycle import lifecycle as lifecycle

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / "tools"))
spec = importlib.util.spec_from_file_location("review_recovery_processor", MODULE / "tools/process_recovery.py")
processor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(processor)


def test_local_recovery_delivers_only_to_the_bound_recipient(lifecycle, tmp_path):
    reviews, commerce = stores()
    recipient = "buyer@example.test"
    recipient_hash = hashlib.sha256(recipient.encode()).hexdigest()
    reviews.db.execute("UPDATE review_invitation_consents SET recipient_context_hash=?", (recipient_hash,))
    reviews.db.commit()
    original = issue(reviews, commerce, recipient_context_hash=recipient_hash)
    asyncio.run(revoke_purchase(reviews, store="store", environment="test", token=original["token"], now=100))
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_DELIVERY_TOKEN="f" * 40,
        REVIEWS_SUBJECT_MAPS=json.dumps({"v1": {"product_1": "policy"}}),
    )

    def post(path, data, token):
        req = SimpleNamespace(method="POST", headers={"Authorization": "Bearer " + token}, data=data, body=None)
        response = asyncio.run(lifecycle.handle_write(req, env, path))
        if response.status >= 400:
            raise ValueError("request unavailable")
        return response.body

    args = dict(
        issuer=env.REVIEWS_SETTLEMENT_TOKEN,
        delivery_token=env.REVIEWS_DELIVERY_TOKEN,
        mailbox_path=tmp_path / "mail",
        store_name="Example",
        landing_url="http://localhost/review/",
        clock=lambda: 101,
    )
    with patch.object(lifecycle.time, "time", return_value=101):
        assert (
            invoke(lifecycle, env, "/reviews/v1/invitation-recovery", {"invitation_token": original["token"]}).status
            == 202
        )
        queued = post("/reviews-internal/v1/invitations/recovery-next", {}, env.REVIEWS_SETTLEMENT_TOKEN)["pending"]
        prepared = post(
            "/reviews-internal/v1/invitations/recovery-prepare",
            {"event_id": queued["source_event_id"], "request_id": queued["request_id"]},
            env.REVIEWS_SETTLEMENT_TOKEN,
        )
        assert prepared["state"] == "prepared"
        assert processor.process_one(post, resolve_recipient=lambda _: None, **args) == {
            "status": "recipient temporarily unavailable"
        }
        assert reviews.db.execute("SELECT state FROM review_recovery_requests").fetchone()[0] == "pending"
        assert (
            post("/reviews-internal/v1/invitations/recovery-next", {}, env.REVIEWS_SETTLEMENT_TOKEN)["pending"] is None
        )
        reviews.db.execute("DELETE FROM review_recovery_deferrals")
        reviews.db.commit()

        def unavailable(_acquisition):
            raise ConnectionError("recipient adapter temporarily unavailable")

        assert processor.process_one(post, resolve_recipient=unavailable, **args) == {
            "status": "recipient temporarily unavailable"
        }
        assert reviews.db.execute("SELECT state FROM review_recovery_requests").fetchone()[0] == "pending"
        reviews.db.execute("DELETE FROM review_recovery_deferrals")
        reviews.db.commit()
        assert processor.process_one(post, resolve_recipient=lambda _: "redirect@example.test", **args) == {
            "status": "recipient unavailable"
        }
        assert not args["mailbox_path"].exists()
        assert reviews.db.execute("SELECT state FROM review_recovery_requests").fetchone()[0] == "failed"
        assert reviews.db.execute("SELECT state FROM invitation_generations").fetchone()[0] == "failed"
    args["clock"] = lambda: 86502
    with patch.object(lifecycle.time, "time", return_value=86502):
        assert (
            invoke(lifecycle, env, "/reviews/v1/invitation-recovery", {"invitation_token": original["token"]}).status
            == 202
        )
        assert processor.process_one(post, resolve_recipient=lambda _: recipient, **args) == {"status": "accepted"}
        assert processor.process_one(post, resolve_recipient=lambda _: recipient, **args) == {"status": "empty"}
    box = mailbox.Maildir(args["mailbox_path"], create=False)
    assert len(box) == 1
    message = next(iter(box.values()))
    assert message["To"] == recipient and len(str(message["X-Reviews-Delivery-Identity"])) == 64
    body = message.get_payload(decode=True).decode()
    url = next(line for line in body.splitlines() if line.startswith("http://"))
    new_token = parse_qs(urlsplit(url).fragment)["invitation"][0]
    assert new_token != original["token"]
    row = reviews.db.execute("SELECT token_hash,expires_at FROM acquisition_invitations").fetchone()
    assert row[0] == hashlib.sha256(new_token.encode()).hexdigest() and row[1] == 86502 + 60 * 86400
    assert reviews.db.execute("SELECT state FROM review_recovery_requests").fetchone()[0] == "fulfilled"


def test_unclaimed_delivery_is_never_sent(monkeypatch, tmp_path):
    recipient = "buyer@example.test"
    item = {
        "acquisition_id": "purchase",
        "request_id": "a" * 64,
        "source_event_id": "event",
        "recipient_context_hash": hashlib.sha256(recipient.encode()).hexdigest(),
    }

    def post(path, _data, _token):
        if path.endswith("recovery-next"):
            return {"pending": item}
        if path.endswith("recovery-prepare"):
            return {"state": "prepared", "token": "b" * 64, "generation_id": "c" * 64, "delivery_identity": "d" * 64}
        return {"state": "denied"}

    def no_send(*_args, **_kwargs):
        raise AssertionError("an unclaimed delivery must not be sent")

    monkeypatch.setattr(processor, "deliver_bound_invitation", no_send)
    result = processor.process_one(
        post,
        issuer="issuer",
        delivery_token="delivery",
        resolve_recipient=lambda _: recipient,
        mailbox_path=tmp_path / "mail",
        store_name="Example",
        landing_url="http://localhost/review/",
    )
    assert result == {"status": "reconciliation required"}


def test_reconciliation_uses_provider_receipt_without_resending(lifecycle):
    from cartray_reviews.invitations import claim_delivery, prepare_replacement

    reviews, commerce = stores()
    issue(reviews, commerce)
    draft = asyncio.run(
        prepare_replacement(
            reviews,
            store="store",
            environment="test",
            acquisition="order_1",
            request_key="receipt-test",
            key_version="v1",
            secret=b"c" * 40,
            now=101,
            random_token=lambda: "9" * 64,
        )
    )
    claim = {"acquisition_id": "order_1", **{key: draft[key] for key in ("generation_id", "delivery_identity")}}
    asyncio.run(
        claim_delivery(
            reviews,
            store="store",
            environment="test",
            acquisition=claim["acquisition_id"],
            generation=claim["generation_id"],
            delivery=claim["delivery_identity"],
            now=102,
        )
    )
    env = SimpleNamespace(
        REVIEWS_DB=reviews,
        CARTRAY_DB=commerce,
        REVIEWS_STORE_KEY="store",
        REVIEWS_ENVIRONMENT="test",
        REVIEWS_MODE="verified_acquisition_only",
        REVIEWS_TEST_ADMIN_TOKEN="a" * 40,
        REVIEWS_SETTLEMENT_TOKEN="b" * 40,
        REVIEWS_INVITATION_KEY="c" * 40,
        REVIEWS_REVIEWER_KEY="d" * 40,
        REVIEWS_DELIVERY_TOKEN="f" * 40,
    )

    def post(path, data, credential):
        request = SimpleNamespace(
            method="POST", headers={"Authorization": "Bearer " + credential}, data=data, body=None
        )
        response = asyncio.run(lifecycle.handle_write(request, env, path))
        if response.status >= 400:
            raise ValueError("request unavailable")
        return response.body

    args = dict(post=post, delivery_token=env.REVIEWS_DELIVERY_TOKEN, claim=claim)

    def provider_unavailable(_delivery):
        raise ConnectionError("provider unavailable")

    with patch.object(lifecycle.time, "time", return_value=200):
        assert processor.reconcile_delivery(**args, lookup=provider_unavailable) == {
            "status": "reconciliation required"
        }
        assert processor.reconcile_delivery(**args, lookup=lambda _: None) == {"status": "reconciliation required"}
        assert processor.reconcile_delivery(
            **args, lookup=lambda _: {"status": "accepted", "delivery_identity": "0" * 64, "accepted_at": 103}
        ) == {"status": "reconciliation required"}
        assert processor.reconcile_delivery(
            **args,
            lookup=lambda _: {"status": "failed", "delivery_identity": claim["delivery_identity"], "final": False},
        ) == {"status": "reconciliation required"}
        assert reviews.db.execute("SELECT state FROM invitation_generations").fetchone()[0] == "dispatching"
        receipt = {"status": "accepted", "delivery_identity": claim["delivery_identity"], "accepted_at": 103}
        assert processor.reconcile_delivery(**args, lookup=lambda _: receipt) == {"status": "accepted"}
        assert processor.reconcile_delivery(**args, lookup=lambda _: receipt) == {"status": "accepted"}
        assert processor.reconcile_delivery(**args, lookup=lambda _: receipt | {"accepted_at": 104}) == {
            "status": "reconciliation required"
        }
    expires = reviews.db.execute("SELECT expires_at FROM acquisition_invitations").fetchone()[0]
    assert expires == 103 + 60 * 86400
