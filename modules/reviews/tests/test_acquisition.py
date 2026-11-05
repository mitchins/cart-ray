"""Exercise the CartRay settlement adapter against the actual commerce schema."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from cartray_reviews.acquisition import confirmed_purchase

ROOT = Path(__file__).resolve().parents[3]


class Statement:
    def __init__(self, connection, sql):
        self.connection, self.sql, self.values = connection, sql, ()

    def bind(self, *values):
        self.values = values
        return self

    async def all(self):
        return SimpleNamespace(results=[dict(row) for row in self.connection.execute(self.sql, self.values)])


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        for migration in sorted((ROOT / "migrations").glob("*.sql")):
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return Statement(self.connection, sql)

    def add_purchase(self, state="confirmed", payment="paid", amount=100, second=False):
        c = self.connection
        c.execute(
            "INSERT INTO orders VALUES ('order_1','request_1','fingerprint','v1','digest','aud',?,1)",
            (200 if second else 100,),
        )
        c.execute("INSERT INTO order_items VALUES ('order_1','product_1',1,'price_1',100,'[]')")
        if second:
            c.execute("INSERT INTO order_items VALUES ('order_1','product_2',1,'price_2',100,'[]')")
        c.execute(
            "INSERT INTO stripe_events (stripe_event_id,event_type,session_id,payload_json,received_at,"
            "processed_at,payload_sha256,processing_state) VALUES ('event_1','checkout.session.completed',"
            "'session_1','{}',1,1,'digest','confirmed')"
        )
        c.execute(
            "INSERT INTO checkout_sessions (order_id,state,external_session_id,projection_nonce,updated_at,"
            "settlement_state,settlement_session_id,settlement_event_id,settled_at,stripe_payment_status,"
            "stripe_amount_total_minor) VALUES ('order_1','redirect_issued','session_1','nonce',1,?,"
            "'session_1','event_1',1,?,?)",
            (state, payment, amount),
        )
        c.commit()


def read(db, **overrides):
    options = {
        "store": "store",
        "environment": "test",
        "event_id": "event_1",
        "subject_maps": {"v1": {"product_1": "policy"}},
    }
    return asyncio.run(confirmed_purchase(db, **(options | overrides)))


def test_confirmed_paid_purchase_uses_immutable_product_mapping():
    db = Database()
    db.add_purchase()
    acquisition = read(db)
    assert (acquisition.acquisition_id, acquisition.event_id, acquisition.subject_ids) == (
        "order_1",
        "event_1",
        ("policy",),
    )


@pytest.mark.parametrize(
    "state,payment,amount",
    [("pending", "paid", 100), ("confirmed", "unpaid", 100), ("confirmed", "no_payment_required", 0)],
)
def test_unconfirmed_unpaid_or_free_acquisition_not_labelled_purchase(state, payment, amount):
    db = Database()
    db.add_purchase(state, payment, amount)
    with pytest.raises(ValueError, match="purchase unavailable"):
        read(db)


def test_scope_and_pinned_mapping_are_required():
    db = Database()
    db.add_purchase()
    for options in (
        {"environment": "live"},
        {"store": ""},
        {"event_id": "missing"},
        {"subject_maps": {}},
        {"subject_maps": {"v1": {}}},
    ):
        with pytest.raises(ValueError):
            read(db, **options)


def test_session_mismatch_cannot_become_purchase_evidence():
    db = Database()
    db.add_purchase()
    db.connection.execute("UPDATE stripe_events SET session_id='different' WHERE stripe_event_id='event_1'")
    db.connection.commit()
    with pytest.raises(ValueError, match="purchase unavailable"):
        read(db)


def test_free_line_in_paid_order_is_not_labelled_a_purchase():
    db = Database()
    c = db.connection
    c.execute("INSERT INTO orders VALUES ('order_1','request_1','fingerprint','v1','digest','aud',100,1)")
    c.execute("INSERT INTO order_items VALUES ('order_1','product_1',1,'price_1',100,'[]')")
    c.execute("INSERT INTO order_items VALUES ('order_1','product_free',1,'price_free',0,'[]')")
    c.execute(
        "INSERT INTO stripe_events (stripe_event_id,event_type,session_id,payload_json,received_at,"
        "processed_at,payload_sha256,processing_state) VALUES ('event_1','checkout.session.completed',"
        "'session_1','{}',1,1,'digest','confirmed')"
    )
    c.execute(
        "INSERT INTO checkout_sessions (order_id,state,external_session_id,projection_nonce,updated_at,"
        "settlement_state,settlement_session_id,settlement_event_id,settled_at,stripe_payment_status,"
        "stripe_amount_total_minor) VALUES ('order_1','redirect_issued','session_1','nonce',1,'confirmed',"
        "'session_1','event_1',1,'paid',100)"
    )
    c.commit()
    acquisition = read(db, subject_maps={"v1": {"product_1": "paid-policy", "product_free": "free-policy"}})
    assert acquisition.subject_ids == ("paid-policy",)


def test_reconciled_purchase_requires_matching_confirmed_reconciliation():
    db = Database()
    c = db.connection
    c.execute("INSERT INTO orders VALUES ('order_1','request_1','fingerprint','v1','digest','aud',100,1)")
    c.execute("INSERT INTO order_items VALUES ('order_1','product_1',1,'price_1',100,'[]')")
    c.execute(
        "INSERT INTO checkout_sessions (order_id,state,external_session_id,projection_nonce,updated_at,"
        "settlement_state,settlement_session_id,settled_at,stripe_payment_status,stripe_amount_total_minor) "
        "VALUES ('order_1','redirect_issued','session_1','nonce',1,'confirmed','session_1',1,'paid',100)"
    )
    c.execute(
        "INSERT INTO checkout_reconciliations (order_id,session_id,next_attempt_at,last_outcome,observed_status,"
        "observed_payment_status,observed_amount_total_minor,updated_at) "
        "VALUES ('order_1','session_1',1,'confirmed','complete','paid',100,1)"
    )
    c.commit()
    assert read(db, event_id="recon_order_1").subject_ids == ("policy",)
    c.execute("UPDATE checkout_reconciliations SET observed_amount_total_minor=99 WHERE order_id='order_1'")
    c.commit()
    with pytest.raises(ValueError, match="purchase unavailable"):
        read(db, event_id="recon_order_1")
