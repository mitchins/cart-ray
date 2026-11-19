"""Operator-only prepared-fulfilment recovery through existing D1 and service bindings."""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlsplit

from cloud import CloudError

from cartray.post_purchase import D1Dispatcher


class _Statement:
    def __init__(self, database, sql):
        self.database, self.sql, self.params = database, sql, ()

    def bind(self, *params):
        self.params = params
        return self

    async def all(self):
        return self.database.execute([self])[0]

    async def run(self):
        return self.database.execute([self])[0]


class OperatorD1:
    """Adapt operator-held Cloudflare D1 batch API to the existing D1 contract."""

    def __init__(self, api, database_id):
        self.api, self.database_id = api, database_id

    def prepare(self, sql):
        return _Statement(self, sql)

    def execute(self, statements):
        result = self.api.request(
            f"d1/database/{self.database_id}/query",
            "POST",
            {"batch": [{"sql": item.sql, "params": list(item.params)} for item in statements]},
        )
        if (
            not isinstance(result, list)
            or len(result) != len(statements)
            or any(not isinstance(item, dict) or item.get("success") is not True for item in result)
        ):
            raise CloudError("D1 fulfilment operator batch failed; inspect the held claim before retrying")
        return result

    async def batch(self, statements):
        return self.execute(statements)


class FulfilmentOperator:
    def revoke_fulfilment(self, order_id, workflow_key, product_key, ordinal, operator, evidence, confirmed):
        """Audit and revoke a single test entitlement without touching a delivery claim."""
        if (
            not confirmed
            or self.state.get("environment") != "test"
            or not self.has_post_purchase
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value)
                for value in (order_id, workflow_key, product_key)
            )
            or type(ordinal) is not int
            or not 0 <= ordinal < 15
            or any(
                not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(char) < 32 for char in value)
                for value, limit in ((operator, 128), (evidence, 512))
            )
        ):
            raise ValueError("explicit test entitlement, operator and evidence are required")
        statement = (
            OperatorD1(self.api, self.db("commerce"))
            .prepare(
                "INSERT INTO fulfilment_revocation_audit "
                "(merchant_id,environment,order_id,workflow_key,product_key,ordinal,operator,evidence,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)"
            )
            .bind(
                self.config.get("store_key", "acorn-store"),
                "test",
                order_id,
                workflow_key,
                product_key,
                ordinal,
                operator,
                evidence,
                int(time.time()),
            )
        )
        OperatorD1(self.api, self.db("commerce")).execute([statement])
        return {
            "fulfilment_revocation": "recorded",
            "order": order_id,
            "workflow": workflow_key,
            "product": product_key,
            "ordinal": ordinal,
        }

    def recover_prepared_fulfilment(self, order_id, workflow_key, operator, evidence, confirmed):
        """Explicitly reissue a stale proven-unsent preparation and attempt test delivery once."""
        if not confirmed or self.state.get("environment") != "test" or not self.has_post_purchase:
            raise ValueError("explicit confirmation and a test post-purchase installation are required")
        if any(self.state.get("post_purchase_modes", {}).get(role) != "test" for role in ("source", "post-purchase")):
            raise ValueError("enable the authorized test post-purchase source and handler before recovery")
        self.check_post_purchase_handoff()
        origin = self.config.get("storefront_origin") or "https://" + self.state.get("pages_subdomain", "")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("configured storefront origin is invalid")
        origin = f"https://{parsed.netloc}"
        token = self.vault()["post_purchase_token"]

        def handler(purchase, execution):
            result = self.private(
                "POST_PURCHASE",
                "/",
                {
                    "merchant": execution.merchant,
                    "environment": execution.environment,
                    "workflow_key": execution.workflow_key,
                    "config_ref": execution.config_ref,
                    "config_digest": execution.config_digest,
                    "order_id": purchase.order_id,
                    "session_id": purchase.session_id,
                    "items": purchase.items,
                    "access_links": execution.access_links,
                },
                token,
            )
            return result.get("provider_reference")

        dispatcher = D1Dispatcher(
            OperatorD1(self.api, self.db("commerce")),
            self.config.get("store_key", "acorn-store"),
            "test",
            {"service": handler},
            pinned=True,
            fulfilment_origin=origin,
        )
        outcome = asyncio.run(dispatcher.recover_prepared(order_id, workflow_key, operator, evidence))
        return {"fulfilment_recovery": outcome, "order": order_id, "workflow": workflow_key}
