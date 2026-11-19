import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "modules/reviews/operator"))
from fulfilment_operator import FulfilmentOperator  # noqa: E402
from test_fulfilment_access import D1, database  # noqa: E402

from cartray.fulfilment import prepare_notification
from cartray.post_purchase import Action


class CloudD1:
    def __init__(self, db):
        self.db = db

    def request(self, path, method, body):
        assert path == "d1/database/test-database/query" and method == "POST"
        self.db.commit()
        try:
            self.db.execute("BEGIN")
            result = []
            for query in body["batch"]:
                cursor = self.db.execute(query["sql"], query["params"])
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                result.append({"success": True, "results": rows, "meta": {"changes": max(0, cursor.rowcount)}})
            self.db.commit()
            return result
        except sqlite3.DatabaseError:
            self.db.rollback()
            raise


class Installation(FulfilmentOperator):
    has_post_purchase = True
    state = {"environment": "test", "post_purchase_modes": {"source": "test", "post-purchase": "test"}}
    config = {"store_key": "store", "storefront_origin": "https://store.example.test"}

    def __init__(self, db):
        self.api = CloudD1(db)
        self.sent = []

    def check_post_purchase_handoff(self):
        pass

    def vault(self):
        return {"post_purchase_token": "x" * 32}

    def db(self, role):
        assert role == "commerce"
        return "test-database"

    def private(self, service, path, body, token):
        assert service == "POST_PURCHASE" and path == "/" and token == "x" * 32
        self.sent.append(body)
        return {"provider_reference": "test-receipt"}


def test_operator_recovery_uses_shared_fence_and_never_prints_capabilities(capsys):
    db = database(prepopulate=False)
    db.execute("UPDATE checkout_sessions SET external_session_id='cs_test_demo' WHERE order_id='order'")
    db.commit()
    action = Action("workflow", ("offer",), "service", "private/config", "a" * 64)
    asyncio.run(prepare_notification(D1(db), "store", "test", "order", action, int(time.time()) - 1201))
    installation = Installation(db)
    with pytest.raises(ValueError, match="confirmation"):
        installation.recover_prepared_fulfilment("order", "workflow", "operator", "no send started", False)
    result = installation.recover_prepared_fulfilment("order", "workflow", "operator", "no send started", True)
    assert result["fulfilment_recovery"] == "accepted"
    assert len(installation.sent) == 1 and len(installation.sent[0]["access_links"]) == 3
    assert capsys.readouterr().out == ""
    assert db.execute("SELECT state FROM post_purchase_executions").fetchone()[0] == "accepted"
    with pytest.raises(ValueError, match="stale"):
        installation.recover_prepared_fulfilment("order", "workflow", "operator", "unknown", True)
    assert len(installation.sent) == 1


def test_operator_revocation_is_audited_and_cannot_be_undone():
    db = database()
    installation = Installation(db)
    with pytest.raises(ValueError, match="explicit test"):
        installation.revoke_fulfilment("order", "workflow", "offer", 0, "operator", "requested", False)
    result = installation.revoke_fulfilment("order", "workflow", "offer", 0, "operator", "requested", True)
    assert result["fulfilment_revocation"] == "recorded"
    assert db.execute("SELECT revoked_at FROM fulfilment_entitlements WHERE ordinal=0").fetchone()[0] is not None
    assert db.execute("SELECT operator,evidence FROM fulfilment_revocation_audit").fetchone()[:] == (
        "operator",
        "requested",
    )
    with pytest.raises(sqlite3.IntegrityError, match="revoked once"):
        db.execute("UPDATE fulfilment_entitlements SET revoked_at=NULL WHERE ordinal=0")
    assert installation.sent == []
