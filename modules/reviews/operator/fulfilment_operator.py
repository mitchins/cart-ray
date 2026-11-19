"""Operator-only prepared-fulfilment recovery through existing D1 and service bindings."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from cloud import CloudError
from topology import post_purchase_source_config

from cartray.fulfilment import Resource, checkout_plan, valid_external_config, valid_private_config
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
    def register_fulfilment(self, package, confirmed):
        """Register a bounded, immutable test resource package while delivery is off."""
        if not confirmed or self.state.get("environment") != "test" or not self.has_post_purchase:
            raise ValueError("explicit confirmation and a test post-purchase installation are required")
        if any(
            self.state.get("post_purchase_modes", {}).get(role, "disabled") != "disabled"
            for role in ("source", "post-purchase")
        ):
            raise ValueError("disable both post-purchase workers before registering resources")
        if self.state.get("deployment", {}).get("complete") and "fulfilment" not in self.state:
            raise ValueError("register fulfilment before the initial deployment")
        if not isinstance(package, dict) or set(package) != {
            "schema",
            "merchant",
            "environment",
            "bucket",
            "plan",
            "resources",
        }:
            raise ValueError("invalid fulfilment package")
        merchant = self.config.get("store_key", "acorn-store")
        if package["schema"] != 1 or package["merchant"] != merchant or package["environment"] != "test":
            raise ValueError("fulfilment package scope differs from installation")
        plan = package["plan"]
        if not isinstance(plan, dict) or plan.get("actions") != json.loads(
            post_purchase_source_config(self.post_purchase_actions())
        ):
            raise ValueError("fulfilment workflows differ from the installed action snapshot")
        plan_raw = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        checkout_plan(plan_raw, ())
        entries = package["resources"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError("fulfilment package needs 1 to 100 resources")
        provider_config, definitions, identities = {}, [], set()
        needs_bucket = False
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "id",
                "version",
                "provider",
                "config_ref",
                "filename",
                "mime_type",
                "config",
                "expires_at",
            }:
                raise ValueError("invalid fulfilment resource")
            config = entry["config"]
            if not isinstance(config, dict):
                raise ValueError("invalid fulfilment provider configuration")
            provider = entry["provider"]
            if provider == "external_link":
                valid = valid_external_config(config)
            elif provider == "private_object":
                valid = valid_private_config(config) and config.get("bucket_binding") == "FULFILMENT_PRIVATE_BUCKET"
                needs_bucket = True
            else:
                valid = False
            expiry = entry["expires_at"]
            if not valid or (expiry is not None and (type(expiry) is not int or expiry <= int(time.time()))):
                raise ValueError("invalid or expired fulfilment provider configuration")
            encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            resource = Resource(
                merchant,
                entry["id"],
                entry["version"],
                provider,
                entry["config_ref"],
                hashlib.sha256(encoded).hexdigest(),
                entry["filename"],
                entry["mime_type"],
            )
            identity = (resource.resource_id, resource.version)
            if identity in identities or (
                resource.config_ref in provider_config and provider_config[resource.config_ref] != config
            ):
                raise ValueError("duplicate fulfilment resource or conflicting private configuration")
            identities.add(identity)
            provider_config[resource.config_ref] = config
            definitions.append(
                (
                    merchant,
                    "test",
                    resource.resource_id,
                    resource.version,
                    resource.provider,
                    resource.config_ref,
                    resource.config_digest,
                    resource.filename,
                    resource.mime_type,
                    json.dumps({"expires_at": expiry}, separators=(",", ":")),
                )
            )
        referenced = {(r["id"], r["version"]) for b in plan["bindings"] for r in b["resources"]}
        if referenced != identities:
            raise ValueError("resource definitions must match the configured offer bindings")
        required = {
            (action["workflow_key"], offer) for action in plan["actions"]["actions"] for offer in action["offer_keys"]
        }
        if required != {(binding["workflow_key"], binding["product_key"]) for binding in plan["bindings"]}:
            raise ValueError("every configured workflow offer needs an explicit resource binding")
        bucket = package["bucket"]
        if needs_bucket:
            if not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
                raise ValueError("an existing private test bucket name is required")
            found = self.api.request("r2/buckets/" + bucket)
            if not isinstance(found, dict) or found.get("name") != bucket:
                raise ValueError("private bucket identity could not be verified")
        elif bucket is not None:
            raise ValueError("bucket must be null when no private objects are used")
        values = {
            "fulfilment_checkout_plan": plan_raw,
            "fulfilment_provider_config": json.dumps(
                {"merchant": merchant, "resources": provider_config},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        }
        old = self.vault()
        if any(key in old and old[key] != value for key, value in values.items()):
            raise ValueError("registered fulfilment configuration is immutable; preserve existing resource access")
        if self.state.get("fulfilment") not in (None, {"bucket": bucket}):
            raise ValueError("registered private bucket differs from the installation")
        database = OperatorD1(self.api, self.db("commerce"))

        def recorded_definition(definition):
            query = database.prepare(
                "SELECT * FROM fulfilment_resources WHERE merchant_id=? AND environment=? "
                "AND resource_id=? AND resource_version=?"
            ).bind(*definition[:4])
            rows = database.execute([query])[0]["results"]
            if not rows:
                return None
            keys = ("merchant_id", "environment", "resource_id", "resource_version", "provider", "config_ref",
                    "config_digest", "filename", "mime_type", "policy_json")
            return tuple(rows[0][key] for key in keys)

        for definition in definitions:
            existing = recorded_definition(definition)
            if existing is not None and existing != definition:
                raise ValueError("resource version already exists with different content")
        statements = [
            database.prepare("INSERT OR IGNORE INTO fulfilment_resources VALUES (?,?,?,?,?,?,?,?,?,?)").bind(*row)
            for row in definitions
        ]
        database.execute(statements)
        for definition in definitions:
            if recorded_definition(definition) != definition:
                raise ValueError("resource version changed during registration; resolve the conflict")
        from operate import atomic_json, digest

        old.update(values)
        atomic_json(self.vault_path, old)
        self.state["secret_fingerprints"].update({key: digest(value.encode()) for key, value in values.items()})
        self.state["fulfilment"] = {"bucket": bucket}
        self.save()
        return {"fulfilment": "registered", "resources": len(definitions), "environment": "test"}

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
