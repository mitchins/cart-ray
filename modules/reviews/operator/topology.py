"""Explicit configuration graph for the existing test-only Acorn adapters."""

import hashlib
import json
import re

from cartray.order_access import resource_config_bindings

SUBJECTS = {"ACORN-KIT": "acorn-growing-kit", "ACORN-PLANTER": "woodland-planter"}


def validate_post_purchase(value, config, commerce_keys):
    """Validate explicit operator-approved test actions, never infer workflows from copy."""
    if isinstance(value, dict) and value.get("schema") == 2:
        if set(value) != {"schema", "sender", "recipient"}:
            raise ValueError("invalid test confirmation configuration")
        for key in ("sender", "recipient"):
            address = value[key]
            if (
                not isinstance(address, str)
                or not address.isascii()
                or len(address) > 254
                or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}", address)
            ):
                raise ValueError("test confirmation requires one approved sender and recipient")
        return value
    raise ValueError("per-workflow email configuration is retired; use schema 2 order confirmation")


POST_PURCHASE_SECRET_BINDINGS = {
    "source": ("CARTRAY_POST_PURCHASE_TOKEN",),
    "post-purchase": (
        "HANDLER_TOKEN",
        "DELIVERY_CONFIG",
        "APPROVED_TEST_RECIPIENT",
        "APPROVED_TEST_SENDER",
        "STRIPE_API_KEY",
        "SMTP2GO_API_KEY",
    ),
}


def post_purchase_secret_bindings(role, provider="smtp2go"):
    names = POST_PURCHASE_SECRET_BINDINGS[role]
    if role == "post-purchase" and provider == "cloudflare":
        return tuple("CLOUDFLARE_EMAIL_API_TOKEN" if name == "SMTP2GO_API_KEY" else name for name in names)
    return names


def review_requests_enabled(config):
    return bool(config.get('reviews_enabled') and config.get('post_purchase_config')
                and config.get('review_requests', {}).get('timing', 'receipt') != 'off')


def catalogue_subject_maps(installation, versions=None):
    """Retain the manifest mappings purchased orders use, including retired catalogues."""
    config, state = installation.config, installation.state
    releases = state.get('releases', [])
    snapshots = [release.get('catalogue_config') for release in releases]
    current = config
    source_version = (versions or {}).get('source')
    if source_version:
        for index in range(len(releases)):
            if releases[index].get('versions', {}).get('source') == source_version:
                snapshots = snapshots[:index + 1]
                current = releases[index].get('catalogue_config') or config
                break
    mappings = {}
    for snapshot in [*snapshots, current]:
        if not snapshot:
            continue
        path = installation.directory / snapshot['preflight_lock']
        version = json.loads(path.read_text())['catalogue_version']
        subjects = snapshot.get('subject_map', SUBJECTS)
        if version in mappings and mappings[version] != subjects:
            raise ValueError('one catalogue version cannot reinterpret its review subject mapping')
        mappings[version] = subjects
    return dict(sorted(mappings.items()))


def graph(
    installation, versions=None, sending=False, post_purchase_modes=None, email_provider=None, actions_override=None
):
    config, state = installation.config, installation.state
    versions = versions or {}
    db = installation.db
    worker = installation.name

    if any(key.startswith("queue:") for key in state.get("resources", {})):
        raise ValueError("retained queue-era installation: use its pinned release; no automatic handover or resend")
    source_origin = f"https://{worker('source')}.{state['workers_subdomain']}.workers.dev"
    storefront = config.get("storefront_origin") or "https://" + state["pages_subdomain"]
    private_source = config.get("source_mode", "public") == "private"
    if private_source:
        source_origin = storefront + config["source_api_path"]
    return_path = "/purchase-return/" if "merchant_inputs" in config else "/"
    store = config.get("store_key", "acorn-store")
    mapping = json.dumps(catalogue_subject_maps(installation, versions), separators=(",", ":"))
    nodes = {
        "source": {
            "main": "src/cartray/worker.py",
            "public": not private_source,
            "python": True,
            "vars": {
                "CARTRAY_ENVIRONMENT": "test",
                "CARTRAY_SIGNING_KEY_ID": config["namespace"],
                "CARTRAY_STOREFRONT_ORIGIN": storefront,
                "CARTRAY_SUCCESS_URL": storefront + config.get("success_path", return_path) + "?checkout=complete",
                "CARTRAY_CANCEL_URL": storefront + config.get("cancel_path", return_path) + "?checkout=cancelled",
                "CARTRAY_MERCHANT_ID": store,
                "CARTRAY_INSTALLATION_ID": config["namespace"],
                "CARTRAY_SOURCE_API_PATH": config.get("source_api_path", ""),
                "CARTRAY_RECIPIENT_HASH_KEY_VERSION": "key1",
            },
            "db": {"DB": "commerce"},
            "ratelimits": {
                "CHECKOUT_RATE_LIMITER": ("20261110", 20),
                "CHECKOUT_STORE_RATE_LIMITER": ("20261122", 300),
                "CHECKOUT_STATUS_RATE_LIMITER": ("20261120", 60),
            },
        }
    }
    if config["reviews_enabled"]:
        from cartray.order_reviews import ReviewSettings

        preferences = config.get("review_requests", {})
        ReviewSettings.parse(json.dumps(preferences))
        nodes["source"]["vars"].update(
            CARTRAY_REVIEW_INVITATION_MODE="automatic" if review_requests_enabled(config) else "disabled",
            CARTRAY_REVIEW_INVITATION_DEFAULT=(
                "checked" if config.get("review_invitation_default_checked", True) else "unchecked"
            ),
            CARTRAY_REVIEW_SETTINGS=json.dumps(preferences),
            CARTRAY_REVIEW_SUBJECT_MAPS=mapping,
        )
        nodes["reviews"] = {
            "main": "modules/reviews/src/worker.py",
            "python": True,
            "vars": {
                "REVIEWS_ENVIRONMENT": "test",
                "REVIEWS_MODE": "verified_acquisition_only",
                "REVIEWS_STORE_KEY": store,
                "REVIEWS_INVITATION_LIFETIME": "never",
                "REVIEWS_PUBLIC_STORE_NAME": config["merchant_name"],
                "REVIEWS_PUBLIC_ADMISSION": "edge",
                "REVIEWS_INSTALLATION_ID": config["namespace"],
                "REVIEWS_STAFF_MODE": "test_admin",
                "REVIEWS_RECOVERY_MODE": "order_notifications",
            },
            "db": {"REVIEWS_DB": "reviews", "CARTRAY_DB": "commerce"},
            "ratelimits": {"REVIEWS_INGRESS": ("20261111", 1000), "REVIEWS_PUBLIC_INGRESS": ("20261121", 120)},
        }
    if "post_purchase_config" in config:
        actions = actions_override if actions_override is not None else installation.post_purchase_actions()
        if not isinstance(actions, dict):
            raise ValueError("per-workflow notification deployment is retired")
        modes = post_purchase_modes if post_purchase_modes is not None else state.get("post_purchase_modes", {})
        nodes["source"]["vars"]["CARTRAY_POST_PURCHASE_MODE"] = modes.get("source", "disabled")
        nodes["source"]["services"] = {"POST_PURCHASE_HANDLER": "post-purchase"}
        nodes["post-purchase"] = {
            "main": "modules/post-purchase/transactional-email/worker.mjs",
            "public": False,
            "vars": {
                "ENVIRONMENT": "test",
                "DELIVERY_MODE": modes.get("post-purchase", "disabled"),
                "STRIPE_ACCOUNT_ID": config["stripe_account_id"],
                "MERCHANT_ID": store,
                "MERCHANT_NAME": config["merchant_name"],
                "MERCHANT_CONTACT": config["merchant_contact"],
                "STOREFRONT_ORIGIN": storefront,
            },
        }
        provider = email_provider or config.get("post_purchase_email_provider", "smtp2go")
        if provider == "cloudflare":
            nodes["post-purchase"]["vars"].update(
                EMAIL_PROVIDER="cloudflare", CLOUDFLARE_EMAIL_ACCOUNT_ID=config["account_id"]
            )
    if "fulfilment" in config:
        nodes["source"]["vars"].update(
            CARTRAY_ORDER_ACCESS_MODE="test",
            CARTRAY_ORDER_ACCESS_PATH=config.get("source_api_path", "") + "/order-access",
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
        bindings += [
            {"name": name, "type": "r2_bucket", "bucket_name": bucket}
            for name, bucket in node.get("buckets", {}).items()
        ]
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
    return nodes, {
        "source": source_origin,
        "storefront": storefront,
        "unsubscribe": source_origin + "/review-unsubscribe",
    }


def secret_bindings(installation, role):
    vault, credentials = installation.vault(), installation.credentials
    names = {
        "source": {},
        "post-purchase": {},
        "reviews": {
            "REVIEWS_SETTLEMENT_TOKEN": "settlement",
            "REVIEWS_TEST_ADMIN_TOKEN": "admin",
            "REVIEWS_INVITATION_KEY": "invitation",
            "REVIEWS_REVIEWER_KEY": "reviewer",
            "REVIEWS_DELIVERY_TOKEN": "delivery",
            "REVIEWS_UNSUBSCRIBE_KEY": "unsubscribe",
        },
    }
    if "post_purchase_config" in installation.config and role in ("source", "post-purchase"):
        names[role]["CARTRAY_POST_PURCHASE_TOKEN" if role == "source" else "HANDLER_TOKEN"] = "post_purchase_token"
    result = {name: vault[key] for name, key in names[role].items()}
    if role == "source" and getattr(installation, "consolidated", False) and installation.config["reviews_enabled"]:
        result["CARTRAY_RECIPIENT_HASH_KEY"] = vault["recipient_hash"]
        result["CARTRAY_REVIEW_INVITATION_KEY"] = vault["invitation"]
    if role == "source" and "fulfilment" in installation.config:
        if "order_access" not in vault:
            raise ValueError("order access key missing; restore the original installation key")
        result["CARTRAY_ORDER_ACCESS_KEY"] = vault["order_access"]
        result.update(resource_config_bindings(credentials["fulfilment_resources"]))
    if role == "source":
        result.update(
            STRIPE_API_KEY=credentials["stripe_test_key"],
            STRIPE_WEBHOOK_SECRET=vault["webhook_secret"],
            CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64=vault["signing_private"],
            CARTRAY_PROJECTION_PUBLIC_KEYS_JSON=json.dumps({installation.config["namespace"]: vault["signing_public"]}),
        )
    if role == "post-purchase":
        result.update(
            DELIVERY_CONFIG="{}",
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
