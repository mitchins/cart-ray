"""Explicit configuration graph for the existing test-only Acorn adapters."""

import hashlib
import json
import re

SUBJECTS = {"ACORN-KIT": "acorn-growing-kit", "ACORN-PLANTER": "woodland-planter"}


def validate_post_purchase(value, config, commerce_keys):
    """Validate explicit operator-approved test actions, never infer workflows from copy."""
    if isinstance(value, dict) and value.get("schema") == 2:
        if set(value) != {"schema", "sender", "recipient"}:
            raise ValueError("invalid test confirmation configuration")
        for key in ("sender", "recipient"):
            address = value[key]
            if (not isinstance(address, str) or not address.isascii() or len(address) > 254
                    or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}", address)):
                raise ValueError("test confirmation requires one approved sender and recipient")
        return value
    if config.get("store_key") != "effective-policy-uat" or config.get("stripe_account_id") != "acct_1Ud2XZBKRMfYblCU":
        raise ValueError("legacy workflow delivery is restricted to the existing EP test installation")
    fields = {"environment", "merchant", "workflow_key", "config_ref", "offer_keys", "links",
              "recipient", "sender", "subject", "stripe_account"}
    if (not isinstance(value, dict) or set(value) != {"schema", "actions"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or not isinstance(value["actions"], list) or not 1 <= len(value["actions"]) <= 100):
        raise ValueError("invalid protected post-purchase action file")
    actions, workflows, recipients, senders = [], set(), set(), set()
    for action in value["actions"]:
        if not isinstance(action, dict) or set(action) != fields:
            raise ValueError("invalid post-purchase action fields")
        if (action["environment"] != "test" or action["merchant"] != config["store_key"]
                or action["stripe_account"] != config["stripe_account_id"]):
            raise ValueError("post-purchase action scope differs from installation")
        for key in ("workflow_key", "config_ref"):
            pattern = (r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}" if key == "workflow_key"
                       else r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}")
            if (not isinstance(action[key], str) or not re.fullmatch(pattern, action[key])
                    or any(part in ("", ".", "..") for part in action[key].split("/"))):
                raise ValueError("invalid post-purchase workflow/config reference")
        if action["workflow_key"] in workflows:
            raise ValueError("duplicate post-purchase workflow")
        workflows.add(action["workflow_key"])
        offers = action["offer_keys"]
        if (not isinstance(offers, list) or not offers or len(offers) > 100
                or any(not isinstance(key, str) or key not in commerce_keys for key in offers)
                or len(set(offers)) != len(offers)):
            raise ValueError("post-purchase offers must be an authorised commerce subset")
        links = action["links"]
        if (not isinstance(links, list) or not 1 <= len(links) <= 20
                or any(not isinstance(link, str) or not re.fullmatch(r"https://www\.dropbox\.com/t/[A-Za-z0-9]+", link)
                       for link in links)):
            raise ValueError("only explicit dummy Dropbox Transfer links are supported")
        for key in ("recipient", "sender"):
            if (not isinstance(action[key], str) or not action[key].isascii() or len(action[key]) > 254
                    or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}", action[key])):
                raise ValueError("post-purchase requires explicit approved recipient and preflighted sender")
        if not isinstance(action["subject"], str) or not re.fullmatch(r"[ -~]{1,180}", action["subject"]):
            raise ValueError("post-purchase subject must be bounded ASCII text")
        recipients.add(action["recipient"].lower())
        senders.add(action["sender"].lower())
        # Bound the EP lane to the explicitly approved source flow, not product titles.
        if config["store_key"] == "effective-policy-uat" and (
                action["workflow_key"] != "ep:woo:workflow:8681" or offers != ["EP-UAT-171"]
                or links != ["https://www.dropbox.com/t/N86okpWEWSXWDx9X"]
                or action["recipient"].lower() != "test@mitchellcurrie.com"):
            raise ValueError("EP test delivery is restricted to the approved 171/AW8681 dummy flow")
        actions.append(dict(sorted(action.items())))
    if len(recipients) != 1 or len(senders) != 1:
        raise ValueError("post-purchase requires one approved recipient and sender")
    if max(len(post_purchase_handler_config(actions)), len(post_purchase_source_config(actions))) > 16384:
        raise ValueError("post-purchase action configuration exceeds runtime bounds")
    return actions


def post_purchase_source_config(actions):
    # These action values contain only strings and string arrays; serialization matches JSON.stringify.
    return json.dumps({"merchant": actions[0]["merchant"], "environment": "test", "actions": [
        {"workflow_key": a["workflow_key"], "offer_keys": a["offer_keys"], "handler": "service",
         "config_ref": a["config_ref"],
         "config_digest": hashlib.sha256(json.dumps(a, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()}
        for a in actions]}, separators=(",", ":"))


def post_purchase_handler_config(actions):
    return json.dumps({"actions": actions}, ensure_ascii=False, separators=(",", ":"))


POST_PURCHASE_SECRET_BINDINGS = {
    "source": ("CARTRAY_POST_PURCHASE_TOKEN",),
    "post-purchase": ("HANDLER_TOKEN", "DELIVERY_CONFIG", "APPROVED_TEST_RECIPIENT", "APPROVED_TEST_SENDER",
                      "STRIPE_API_KEY", "SMTP2GO_API_KEY"),
}


def post_purchase_secret_bindings(role, provider="smtp2go"):
    names = POST_PURCHASE_SECRET_BINDINGS[role]
    if role == "post-purchase" and provider == "cloudflare":
        return tuple("CLOUDFLARE_EMAIL_API_TOKEN" if name == "SMTP2GO_API_KEY" else name for name in names)
    return names


def graph(installation, versions=None, sending=False, post_purchase_modes=None,
          email_provider=None, actions_override=None):
    config, state = installation.config, installation.state
    versions = versions or {}
    db = installation.db
    worker = installation.name

    def queue(role):
        return state["resources"]["queue:" + role]["id"] if config["reviews_enabled"] else "disabled"

    source_origin = f"https://{worker('source')}.{state['workers_subdomain']}.workers.dev"
    sender_origin = f"https://{worker('sender')}.{state['workers_subdomain']}.workers.dev"
    storefront = config["storefront_origin"] if "storefront_origin" in config else "https://" + state["pages_subdomain"]
    private_source = config.get("source_mode", "public") == "private"
    if private_source:
        source_origin = storefront + config["source_api_path"]
    return_path = "/purchase-return/" if "merchant_inputs" in config else "/"
    success_path = config.get("success_path", return_path)
    cancel_path = config.get("cancel_path", return_path)
    catalogue = json.loads(open(config["preflight_lock"]).read())["catalogue_version"]
    store = config.get("store_key", "acorn-store")
    mapping = json.dumps({catalogue: config.get("subject_map", SUBJECTS)}, separators=(",", ":"))
    mapping_hash = hashlib.sha256(mapping.encode()).hexdigest()
    common = {"REVIEWS_ENVIRONMENT": "test"}
    if not config["reviews_enabled"]:
        source = {
            "main": "src/cartray/worker.py",
            "public": not private_source,
            "python": True,
            "vars": {
                "CARTRAY_ENVIRONMENT": "test",
                "CARTRAY_SIGNING_KEY_ID": config["namespace"],
                "CARTRAY_STOREFRONT_ORIGIN": storefront,
                "CARTRAY_SUCCESS_URL": storefront + success_path + "?checkout=complete",
                "CARTRAY_CANCEL_URL": storefront + cancel_path + "?checkout=cancelled",
            },
            "db": {"DB": "commerce"},
            "ratelimits": {"CHECKOUT_RATE_LIMITER": ("20261110", 20),
                           "CHECKOUT_STORE_RATE_LIMITER": ("20261122", 300),
                           "CHECKOUT_STATUS_RATE_LIMITER": ("20261120", 60)},
        }
        nodes = {"source": source}
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
        "SOURCE_STORE": store,
        "SOURCE_ISSUER": config["namespace"],
        "SOURCE_INSTALLATION": config["namespace"],
        "CATALOGUE_VERSION": catalogue,
        "MAPPING_SHA256": mapping_hash,
        "POLICY_VERSION": "operator-v1",
        "DELAY_SECONDS": config["delay_seconds"],
        "MAX_LATENESS": config["max_lateness"],
    }
    review_nodes = {
        "source": {
            "main": "src/cartray/worker.py",
            "public": not private_source,
            "python": True,
            "vars": {
                "CARTRAY_ENVIRONMENT": "test",
                "CARTRAY_REVIEWS_SOURCE": "test",
                "CARTRAY_REVIEWS_PRODUCER": "test",
                "CARTRAY_SIGNING_KEY_ID": config["namespace"],
                "CARTRAY_STOREFRONT_ORIGIN": storefront,
                "CARTRAY_SUCCESS_URL": storefront + success_path + "?checkout=complete",
                "CARTRAY_CANCEL_URL": storefront + cancel_path + "?checkout=cancelled",
                "CARTRAY_REVIEW_INVITATION_MODE": "automatic",
                "CARTRAY_REVIEW_INVITATION_DEFAULT": (
                    "checked" if config.get("review_invitation_default_checked", True) else "unchecked"
                ),
            },
            "db": {"DB": "commerce"},
            "services": {"REVIEWS_SOURCE_TRIGGER": "producer"},
            "ratelimits": {"CHECKOUT_RATE_LIMITER": ("20261110", 20),
                           "CHECKOUT_STORE_RATE_LIMITER": ("20261122", 300),
                           "CHECKOUT_STATUS_RATE_LIMITER": ("20261120", 60)},
        },
        "reviews": {
            "main": "modules/reviews/src/worker.py",
            "python": True,
            "vars": {
                **common,
                "REVIEWS_MODE": "verified_acquisition_only",
                "REVIEWS_STORE_KEY": store,
                "REVIEWS_AUTOMATION_INTAKE": "test",
                "REVIEWS_SUBJECT_MAPS": mapping,
                "REVIEWS_AUTOMATION_SENDING": "test",
                "REVIEWS_INVITATION_LIFETIME": "never",
                "REVIEWS_PUBLIC_STORE_NAME": config["merchant_name"],
                "REVIEWS_PUBLIC_ADMISSION": "edge",
                "REVIEWS_INSTALLATION_ID": config["namespace"],
                "REVIEWS_STAFF_MODE": "test_admin",
            },
            "db": {"REVIEWS_DB": "reviews", "CARTRAY_DB": "commerce"},
            "ratelimits": {"REVIEWS_INGRESS": ("20261111", 1000), "REVIEWS_PUBLIC_INGRESS": ("20261121", 120)},
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
                "SOURCE_STORE": store,
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
                "STORE_KEY": store,
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
            "vars": {**common, "REVIEWS_DUE_SCHEDULER": "test", "REVIEWS_STORE": store},
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
    if config["reviews_enabled"]:
        nodes = review_nodes
    if "post_purchase_config" in config:
        nodes["source"]["vars"].update(CARTRAY_MERCHANT_ID=store)
        actions = actions_override if actions_override is not None else installation.post_purchase_actions()
        modes = post_purchase_modes if post_purchase_modes is not None else state.get("post_purchase_modes", {})
        nodes["source"]["vars"].update(CARTRAY_POST_PURCHASE_MODE=modes.get("source", "disabled"))
        if not isinstance(actions, dict):
            nodes["source"]["vars"]["CARTRAY_POST_PURCHASE_CONFIG"] = post_purchase_source_config(actions)
        nodes["source"].setdefault("services", {})["POST_PURCHASE_HANDLER"] = "post-purchase"
        nodes["post-purchase"] = {
            "main": "modules/post-purchase/transactional-email/worker.mjs", "public": False,
            "vars": {"ENVIRONMENT": "test", "DELIVERY_MODE": modes.get("post-purchase", "disabled"),
                     "STRIPE_ACCOUNT_ID": config["stripe_account_id"], "MERCHANT_ID": store,
                     "MERCHANT_NAME": config["merchant_name"], "STOREFRONT_ORIGIN": storefront},
        }
        provider = email_provider or config.get("post_purchase_email_provider", "smtp2go")
        if provider == "cloudflare":
            nodes["post-purchase"]["vars"].update(
                EMAIL_PROVIDER="cloudflare", CLOUDFLARE_EMAIL_ACCOUNT_ID=config["account_id"])
    if "fulfilment" in config:
        nodes["source"]["vars"].update(
            CARTRAY_ORDER_ACCESS_MODE="test", CARTRAY_MERCHANT_ID=store,
            CARTRAY_ORDER_ACCESS_PATH=(config.get("source_api_path", "") + "/order-access"),
        )
        nodes["source"]["buckets"] = config["fulfilment"]["buckets"]
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
        bindings += [{"name": name, "type": "r2_bucket", "bucket_name": bucket}
                     for name, bucket in node.get("buckets", {}).items()]
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
        "post-purchase": {},
    }
    if role == "source" and not installation.config["reviews_enabled"]:
        names[role] = {}
    if "post_purchase_config" in installation.config and role in ("source", "post-purchase"):
        names[role]["CARTRAY_POST_PURCHASE_TOKEN" if role == "source" else "HANDLER_TOKEN"] = "post_purchase_token"
    result = {name: vault[key] for name, key in names[role].items()}
    if role == "source" and "fulfilment" in installation.config:
        if "order_access" not in vault:
            raise ValueError("order access key missing; restore the original installation key")
        result["CARTRAY_ORDER_ACCESS_KEY"] = vault["order_access"]
        result["CARTRAY_RESOURCE_CONFIG"] = credentials["fulfilment_resources"]
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
    if role == "post-purchase":
        result.update(
            DELIVERY_CONFIG=("{}" if isinstance(installation.post_purchase_actions(), dict)
                             else post_purchase_handler_config(installation.post_purchase_actions())),
            APPROVED_TEST_RECIPIENT=vault["post_purchase_approved_test_email"],
            APPROVED_TEST_SENDER=vault["post_purchase_test_sender"],
            STRIPE_API_KEY=vault["post_purchase_stripe_test_key"],
        )
        if installation.config.get("post_purchase_email_provider", "smtp2go") == "cloudflare":
            result["CLOUDFLARE_EMAIL_API_TOKEN"] = vault["post_purchase_cloudflare_email_api_token"]
        else:
            result["SMTP2GO_API_KEY"] = vault["post_purchase_smtp2go_api_key"]
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
        elif kind == "r2_bucket":
            lines += ["[[r2_buckets]]", f"binding = {q(name)}", f"bucket_name = {q(binding['bucket_name'])}"]
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
