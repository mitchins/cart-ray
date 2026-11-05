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
