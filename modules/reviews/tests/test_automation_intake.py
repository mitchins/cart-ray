"""Intake decisions use real commerce and Reviews SQL, without a sender."""

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from cartray_reviews.automation_intake import intake_order

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "modules/reviews"


class Statement:
    def __init__(self, db, sql):
        self.db, self.sql, self.params = db, sql, ()

    def bind(self, *params):
        self.params = params
        return self

    async def all(self):
        return SimpleNamespace(results=[dict(x) for x in self.db.execute(self.sql, self.params)])


class Database:
    def __init__(self, migrations):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        for path in sorted(migrations.glob("*.sql")):
            self.db.executescript(path.read_text())

    def prepare(self, sql):
        return Statement(self.db, sql)

    async def batch(self, statements):
        try:
            self.db.execute("BEGIN")
            for statement in statements:
                self.db.execute(statement.sql, statement.params)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


def setup(*, activation=50, watermark=5, confirmed=100):
    checkout, reviews = Database(ROOT / "migrations"), Database(MODULE / "migrations")
    c = checkout.db
    c.execute("INSERT INTO orders VALUES ('order_1','request_1','fingerprint','v1','digest','aud',100,1)")
    c.execute("INSERT INTO order_items VALUES ('order_1','product_1',1,'price_1',100,'[]')")
    c.execute(
        "INSERT INTO stripe_events (stripe_event_id,event_type,session_id,payload_json,received_at,"
        "processed_at,payload_sha256,processing_state) VALUES ('event_1','checkout.session.completed',"
        "'session_1','{}',1,1,'digest','confirmed')"
    )
    c.execute(
        "INSERT INTO checkout_sessions (order_id,state,external_session_id,projection_nonce,updated_at,"
        "settlement_state,settlement_session_id,settlement_event_id,settled_at,stripe_payment_status,"
        "stripe_amount_total_minor) VALUES ('order_1','redirect_issued','session_1','nonce',1,'confirmed',"
        "'session_1','event_1',?,'paid',100)",
        (confirmed,),
    )
    payload = json.dumps({"order_id": "order_1", "session_id": "session_1", "items_digest": "digest"})
    c.execute(
        "INSERT INTO outbox(id,order_id,event_type,payload_json,created_at) VALUES (10,'order_1','OrderConfirmed',?,?)",
        (payload, confirmed),
    )
    c.commit()
    reviews.db.execute(
        "INSERT INTO review_automation_activation VALUES ('store','test',?,?,10,100,'v1')", (activation, watermark)
    )
    reviews.db.commit()
    return checkout, reviews


def run(checkout, reviews, **kwargs):
    return asyncio.run(
        intake_order(
            reviews,
            checkout,
            store=kwargs.get("store", "store"),
            environment=kwargs.get("environment", "test"),
            outbox_id=kwargs.get("outbox_id", 10),
            subject_maps=kwargs.get("subject_maps", {"v1": {"product_1": "policy"}}),
            now=kwargs.get("now", 120),
        )
    )


def test_confirmed_purchase_creates_only_held_intent_and_deduplicates():
    checkout, reviews = setup()
    assert run(checkout, reviews) == {"status": "accepted", "due_at": 110}
    assert run(checkout, reviews, now=1000) == {"status": "accepted", "due_at": 110}
    assert reviews.db.execute("SELECT state,due_at FROM review_automation_intents").fetchall()[0][:] == (
        "held_eligibility_recipient_consent",
        110,
    )
    assert reviews.db.execute("SELECT count(*) FROM review_automation_intake").fetchone()[0] == 1
    assert checkout.db.execute("SELECT delivered_at FROM outbox WHERE id=10").fetchone()[0] is None
    assert reviews.db.execute("SELECT count(*) FROM invitation_generations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "activation,watermark,now,status",
    [(100, 5, 120, "pre_activation"), (50, 10, 120, "pre_activation"), (50, 5, 211, "late")],
)
def test_fence_and_lateness_never_create_intent(activation, watermark, now, status):
    checkout, reviews = setup(activation=activation, watermark=watermark)
    assert run(checkout, reviews, now=now)["status"] == status
    assert reviews.db.execute("SELECT count(*) FROM review_automation_intents").fetchone()[0] == 0


def test_conflicting_replay_rolls_back_without_changing_ack():
    checkout, reviews = setup()
    run(checkout, reviews)
    with pytest.raises(ValueError, match="intake conflict"):
        run(checkout, reviews, subject_maps={"v1": {"product_1": "different"}})
    assert reviews.db.execute("SELECT count(*) FROM review_automation_intake").fetchone()[0] == 1
    assert reviews.db.execute("SELECT count(*) FROM review_automation_intents").fetchone()[0] == 1


@pytest.mark.parametrize(
    "options", [{"environment": "live"}, {"store": "other"}, {"outbox_id": 999}, {"subject_maps": {}}, {"now": 99}]
)
def test_invalid_scope_event_mapping_or_time_fail_closed(options):
    checkout, reviews = setup()
    with pytest.raises(ValueError):
        run(checkout, reviews, **options)
    assert reviews.db.execute("SELECT count(*) FROM review_automation_intents").fetchone()[0] == 0


def test_reconciliation_confirmation_uses_the_same_intake_contract():
    checkout, reviews = setup()
    checkout.db.execute("UPDATE checkout_sessions SET settlement_event_id=NULL WHERE order_id='order_1'")
    checkout.db.execute(
        "INSERT INTO checkout_reconciliations(order_id,session_id,next_attempt_at,last_outcome,"
        "observed_status,observed_payment_status,observed_amount_total_minor,updated_at) "
        "VALUES('order_1','session_1',0,'confirmed','complete','paid',100,100)"
    )
    checkout.db.commit()
    assert run(checkout, reviews) == {"status": "accepted", "due_at": 110}
    assert run(checkout, reviews) == {"status": "accepted", "due_at": 110}


def test_activation_cannot_be_replaced_to_reenroll_old_events():
    checkout, reviews = setup(activation=100)
    assert run(checkout, reviews)["status"] == "pre_activation"
    with pytest.raises(sqlite3.IntegrityError):
        reviews.db.execute("INSERT OR REPLACE INTO review_automation_activation VALUES ('store','test',1,0,0,100,'v2')")
    reviews.db.rollback()
    assert run(checkout, reviews)["status"] == "pre_activation"
    assert reviews.db.execute("SELECT count(*) FROM review_automation_intents").fetchone()[0] == 0


def test_forged_outbox_payload_is_not_acknowledged():
    checkout, reviews = setup()
    checkout.db.execute(
        "UPDATE outbox SET payload_json=? WHERE id=10",
        (json.dumps({"order_id": "order_1", "session_id": "other", "items_digest": "digest"}),),
    )
    checkout.db.commit()
    with pytest.raises(ValueError, match="confirmation conflict"):
        run(checkout, reviews)
    assert reviews.db.execute("SELECT count(*) FROM review_automation_intake").fetchone()[0] == 0
