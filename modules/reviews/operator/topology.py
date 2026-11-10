"""Explicit configuration graph for the existing test-only Acorn adapters."""

import hashlib
import json

SUBJECTS = {"ACORN-KIT": "acorn-growing-kit", "ACORN-PLANTER": "woodland-planter"}


def graph(installation, versions=None, sending=False):
    config, state = installation.config, installation.state
    versions = versions or {}
    db = installation.db
    worker = installation.name

    def queue(role):
        return state["resources"]["queue:" + role]["id"]

    source_origin = f"https://{worker('source')}.{state['workers_subdomain']}.workers.dev"
    sender_origin = f"https://{worker('sender')}.{state['workers_subdomain']}.workers.dev"
    storefront = f"https://{worker('store')}.pages.dev"
    catalogue = json.loads(open(config["preflight_lock"]).read())["catalogue_version"]
    mapping = json.dumps({catalogue: SUBJECTS}, separators=(",", ":"))
    mapping_hash = hashlib.sha256(mapping.encode()).hexdigest()
    common = {"REVIEWS_ENVIRONMENT": "test"}
    producer = {
        **common,
        "REVIEWS_SOURCE_PRODUCER": "test",
        "CF_ACCOUNT_ID": config["account_id"],
        "SOURCE_DATABASE_ID": db("commerce"),
        "STRIPE_ACCOUNT_ID": config["stripe_account_id"],
        "SOURCE_WORKER": worker("source"),
        "PRODUCER_WORKER": worker("producer"),
        "REVIEWS_WORKER": worker("reviews"),
        "CONSUMER_WORKER": worker("consumer"),
        "AUTHORITY_WORKER": worker("evidence"),
        "AUTHORITY_CONTEXT_DB_ID": db("context"),
        "EVENT_QUEUE_NAME": worker("events"),
        "EVENT_QUEUE_ID": queue("events"),
        "DEAD_LETTER_QUEUE_NAME": worker("dlq"),
        "DEAD_LETTER_QUEUE_ID": queue("dlq"),
        "SOURCE_STORE": "acorn-store",
        "SOURCE_ISSUER": config["namespace"],
        "SOURCE_INSTALLATION": config["namespace"],
        "CATALOGUE_VERSION": catalogue,
        "MAPPING_SHA256": mapping_hash,
        "POLICY_VERSION": "operator-v1",
        "DELAY_SECONDS": config["delay_seconds"],
        "MAX_LATENESS": config["max_lateness"],
    }
    nodes = {
        "source": {
            "main": "src/cartray/worker.py",
            "public": True,
            "python": True,
            "vars": {
                "CARTRAY_ENVIRONMENT": "test",
                "CARTRAY_REVIEWS_SOURCE": "test",
                "CARTRAY_REVIEWS_PRODUCER": "test",
                "CARTRAY_SIGNING_KEY_ID": config["namespace"],
                "CARTRAY_STOREFRONT_ORIGIN": storefront,
                "CARTRAY_SUCCESS_URL": storefront + "/?checkout=complete",
                "CARTRAY_CANCEL_URL": storefront + "/?checkout=cancelled",
                "CARTRAY_REVIEW_INVITATION_MODE": "automatic",
                "CARTRAY_REVIEW_INVITATION_DEFAULT": "checked",
            },
            "db": {"DB": "commerce"},
            "services": {"REVIEWS_SOURCE_TRIGGER": "producer"},
            "ratelimits": {"CHECKOUT_RATE_LIMITER": ("20261110", 20)},
        },
        "reviews": {
            "main": "modules/reviews/src/worker.py",
            "python": True,
            "vars": {
                **common,
                "REVIEWS_MODE": "verified_acquisition_only",
                "REVIEWS_STORE_KEY": "acorn-store",
                "REVIEWS_AUTOMATION_INTAKE": "test",
                "REVIEWS_SUBJECT_MAPS": mapping,
                "REVIEWS_AUTOMATION_SENDING": "test",
                "REVIEWS_INVITATION_LIFETIME": "never",
                "REVIEWS_PUBLIC_STORE_NAME": config["merchant_name"],
                "REVIEWS_STAFF_MODE": "test_admin",
            },
            "db": {"REVIEWS_DB": "reviews", "CARTRAY_DB": "commerce"},
            "ratelimits": {"REVIEWS_INGRESS": ("20261111", 1000)},
        },
        "producer": {
            "main": "modules/reviews/automation-intake-producer/worker.mjs",
            "vars": producer,
            "db": {"COMMERCE_DB": "commerce"},
            "services": {"SOURCE_API": "source", "REVIEWS_API": "reviews", "CONSUMER_API": "consumer"},
            "produces": {"EVENT_QUEUE": "events"},
        },
        "consumer": {
            "main": "modules/reviews/automation-intake-queue/worker.mjs",
            "vars": {
                **common,
                "REVIEWS_ORDER_SUBSCRIBER": "test",
                "SOURCE_STORE": "acorn-store",
                "SOURCE_ISSUER": config["namespace"],
                "SOURCE_INSTALLATION": config["namespace"],
                "AUTOMATION_EVIDENCE_ENABLED": "test",
                "AUTOMATION_DUE_SCHEDULING_ENABLED": "test",
            },
            "services": {"REVIEWS_API": "reviews", "PURCHASE_EVIDENCE": "evidence", "DUE_SCHEDULER": "scheduler"},
            "consumes": "events",
        },
        "evidence": {
            "main": "modules/reviews/consent/purchase_evidence.mjs",
            "vars": {
                "PURCHASE_EVIDENCE_MODE": "test",
                "STORE_KEY": "acorn-store",
                "ENVIRONMENT": "test",
                "INSTALLATION_ID": config["namespace"],
                "STRIPE_ACCOUNT_ID": config["stripe_account_id"],
                "CATALOGUE_VERSION": catalogue,
                "MAPPING_SHA256": mapping_hash,
                "COMMERCE_DB_ID": db("commerce"),
                "CONTEXT_DB_ID": db("context"),
                "RECIPIENT_HASH_KEY_VERSION": "key1",
                **{
                    role.upper() + "_WORKER_VERSION": versions.get(role, "not-deployed")
                    for role in ("source", "reviews", "producer")
                },
            },
            "db": {"COMMERCE_DB": "commerce", "CONTEXT_DB": "context"},
            "services": {
                "SOURCE_IDENTITY": "source",
                "CHECKOUT": "source",
                "ATTESTOR": "producer",
                "REVIEWS_API": "reviews",
            },
        },
        "scheduler": {
            "main": "modules/reviews/automation-due-producer/worker.mjs",
            "vars": {**common, "REVIEWS_DUE_SCHEDULER": "test", "REVIEWS_STORE": "acorn-store"},
            "db": {"REVIEWS_DB": "reviews"},
            "consumes": "schedule",
            "produces": {"SCHEDULE_QUEUE": "schedule", "DUE_QUEUE": "due"},
        },
        "sender": {
            "main": "modules/reviews/initial-dispatch/worker.mjs",
            "public": True,
            "vars": {
                **common,
                "INITIAL_SEND_MODE": "test" if sending else "disabled",
                "REVIEWS_LANDING_URL": storefront + "/review/",
                "UNSUBSCRIBE_BASE_URL": sender_origin + "/unsubscribe",
                "MERCHANT_NAME": config["merchant_name"],
                "MERCHANT_CONTACT": config["merchant_contact"],
            },
            "services": {"REVIEWS_API": "reviews", "PURCHASE_EVIDENCE": "evidence"},
            "consumes": "due",
        },
    }
    for node in nodes.values():
        bindings = [
            {
                "name": key,
                "type": "plain_text" if isinstance(value, str) else "json",
                "text" if isinstance(value, str) else "json": value,
            }
            for key, value in node["vars"].items()
        ]
        bindings.append({"name": "CF_VERSION_METADATA", "type": "version_metadata"})
        bindings += [{"name": name, "type": "d1", "id": db(role)} for name, role in node.get("db", {}).items()]
        bindings += [
            {"name": name, "type": "service", "service": worker(role), "environment": "production"}
            for name, role in node.get("services", {}).items()
        ]
        bindings += [
            {"name": name, "type": "queue", "queue_name": worker(role)}
            for name, role in node.get("produces", {}).items()
        ]
        bindings += [
            {
                "name": name,
                "type": "ratelimit",
                "namespace_id": str(
                    int(hashlib.sha256((config["namespace"] + name).encode()).hexdigest()[:8], 16) % 1000000000 + 1
                ),
                "simple": {"limit": values[1], "period": 60},
            }
            for name, values in node.get("ratelimits", {}).items()
        ]
        node["bindings"] = bindings
    return nodes, {"source": source_origin, "storefront": storefront, "unsubscribe": sender_origin + "/unsubscribe"}


def secret_bindings(installation, role):
    vault, credentials = installation.vault(), installation.credentials
    shared = {"PURCHASE_ATTESTATION_KEY": "attestation_key", "PURCHASE_ATTESTATION_TOKEN": "attestation_token"}
    names = {
        "source": {"CARTRAY_REVIEWS_IDENTITY_TOKEN": "source_identity", "CARTRAY_REVIEWS_TRIGGER_TOKEN": "trigger"},
        "reviews": {
            "REVIEWS_SETTLEMENT_TOKEN": "settlement",
            "REVIEWS_TEST_ADMIN_TOKEN": "admin",
            "REVIEWS_INVITATION_KEY": "invitation",
            "REVIEWS_REVIEWER_KEY": "reviewer",
            "REVIEWS_AUTOMATION_ACTIVATION_TOKEN": "activation",
            "REVIEWS_AUTOMATION_INGRESS_TOKEN": "ingress",
            "REVIEWS_RECIPIENT_EVIDENCE_SECRET": "recipient_evidence",
            "REVIEWS_CONSENT_SECRET": "consent",
            "REVIEWS_DELIVERY_TOKEN": "delivery",
            "REVIEWS_UNSUBSCRIBE_KEY": "unsubscribe",
        },
        "producer": {
            **shared,
            "SOURCE_IDENTITY_TOKEN": "source_identity",
            "SOURCE_EVENT_KEY": "event_key",
            "REVIEWS_ACTIVATION_TOKEN": "activation",
            "TRIGGER_TOKEN": "trigger",
        },
        "consumer": {
            "SOURCE_EVENT_KEY": "event_key",
            "REVIEWS_AUTOMATION_TOKEN": "ingress",
            "AUTOMATION_EVIDENCE_TOKEN": "automation_evidence",
            "DUE_SCHEDULER_TOKEN": "scheduler",
        },
        "evidence": {
            **shared,
            "RECIPIENT_HASH_KEY": "recipient_hash",
            "RECIPIENT_RESOLVE_TOKEN": "recipient_resolve",
            "REVIEWS_AUTOMATION_INGRESS_TOKEN": "ingress",
            "REVIEWS_CONSENT_SECRET": "consent",
            "REVIEWS_RECIPIENT_EVIDENCE_SECRET": "recipient_evidence",
            "AUTOMATION_EVIDENCE_TOKEN": "automation_evidence",
            "AUTOMATION_RECIPIENT_TOKEN": "automation_recipient",
        },
        "scheduler": {"REVIEWS_DUE_SCHEDULER_TOKEN": "scheduler"},
        "sender": {
            "REVIEWS_AUTOMATION_TOKEN": "ingress",
            "REVIEWS_DELIVERY_TOKEN": "delivery",
            "AUTOMATION_RECIPIENT_TOKEN": "automation_recipient",
        },
    }
    result = {name: vault[key] for name, key in names[role].items()}
    if role == "source":
        result.update(
            STRIPE_API_KEY=credentials["stripe_test_key"],
            STRIPE_WEBHOOK_SECRET=vault["webhook_secret"],
            CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64=vault["signing_private"],
            CARTRAY_PROJECTION_PUBLIC_KEYS_JSON=json.dumps({installation.config["namespace"]: vault["signing_public"]}),
        )
    if role == "evidence":
        result.update(
            STRIPE_TEST_KEY=credentials["stripe_test_key"], APPROVED_TEST_EMAIL=credentials["approved_test_email"]
        )
    if role == "sender":
        result.update(
            SMTP2GO_API_KEY=credentials["smtp2go_api_key"],
            APPROVED_TEST_EMAIL=credentials["approved_test_email"],
            TEST_SENDER=credentials["test_sender"],
        )
    return [{"type": "secret_text", "name": name, "text": value} for name, value in result.items()]


def wrangler_config(installation, role, node):
    q = json.dumps
    lines = [
        f"name = {q(installation.name(role))}",
        f"account_id = {q(installation.config['account_id'])}",
        f"main = {q(node['main'])}",
        'compatibility_date = "2026-08-31"',
        f"workers_dev = {str(node.get('public', False)).lower()}",
    ]
    if node.get("python"):
        flags = ["python_workers"] + (["disable_python_external_sdk"] if role == "reviews" else [])
        lines += [f"compatibility_flags = {q(flags)}"]
    if role == "source":
        lines += ["[build]", 'command = "uv run python scripts/sync_worker_source.py"']
    lines += ["[version_metadata]", 'binding = "CF_VERSION_METADATA"', "[vars]"]
    lines += [f"{name} = {q(value)}" for name, value in node["vars"].items()]
    for binding in node["bindings"]:
        kind, name = binding["type"], binding["name"]
        if kind == "d1":
            lines += [
                "[[d1_databases]]",
                f"binding = {q(name)}",
                f"database_name = {q(binding['id'])}",
                f"database_id = {q(binding['id'])}",
            ]
        elif kind == "service":
            lines += ["[[services]]", f"binding = {q(name)}", f"service = {q(binding['service'])}"]
        elif kind == "queue":
            lines += ["[[queues.producers]]", f"binding = {q(name)}", f"queue = {q(binding['queue_name'])}"]
        elif kind == "ratelimit":
            lines += [
                "[[ratelimits]]",
                f"name = {q(name)}",
                f"namespace_id = {q(binding['namespace_id'])}",
                "simple = { limit = " + str(binding["simple"]["limit"]) + ", period = 60 }",
            ]
    return "\n".join(lines) + "\n"
