"""Independent EP UAT delivery controls; all fence access here is read-only."""

import csv
import hashlib
import json
import re
import secrets
import sys
import time
from pathlib import Path

from topology import graph

POST_PURCHASE_ROLES = ("post-purchase", "source")
OFF = dict.fromkeys(POST_PURCHASE_ROLES, "disabled")
ON = dict.fromkeys(POST_PURCHASE_ROLES, "test")
# Source drain has a 22s timeout plus bounded cancellation cleanup; the handler
# has a shared 7s provider budget inside that drain.
SETTLE_SECONDS = 30
HANDOFF_FIELDS = ("stripe_test_key", "smtp2go_api_key", "approved_test_email", "test_sender")
PROVIDER_KEYS = {"smtp2go": "smtp2go_api_key", "cloudflare": "cloudflare_email_api_token"}
COMMON_HANDOFF_FIELDS = ("stripe_test_key", "approved_test_email", "test_sender")


def email_handoff_fields(provider):
    return (*COMMON_HANDOFF_FIELDS, PROVIDER_KEYS[provider])


def validate_email_credential(credentials, provider):
    value = credentials.get(PROVIDER_KEYS[provider], "")
    valid = (isinstance(value, str) and value.startswith("api-") if provider == "smtp2go" else
             isinstance(value, str) and re.fullmatch(r"[!-~]{20,1024}", value))
    if not valid:
        raise ValueError("invalid test email provider credential")


def eligible_offers(merchant_inputs, lock):
    prices = lock.get("prices")
    if lock.get("stripe_mode") != "test" or not isinstance(prices, list):
        raise ValueError("post-purchase requires a Stripe test preflight price lock")
    paid = {row["stripe_lookup_key"] for row in prices if isinstance(row, dict)
            and isinstance(row.get("stripe_lookup_key"), str)
            and type(row.get("amount_minor")) is int and row["amount_minor"] > 0}
    with (merchant_inputs / "commerce/catalogue.csv").open(newline="") as stream:
        return {row["product_key"] for row in csv.DictReader(stream)
                if row.get("active", "").lower() == "true" and row.get("stripe_lookup_key") in paid}


class PostPurchaseOperator:
    @property
    def has_post_purchase(self):
        return "post_purchase_config" in self.config

    @property
    def consolidated(self):
        return (not any(key.startswith('queue:') for key in self.state.get('resources', {}))
                and (not self.has_post_purchase or isinstance(self.post_purchase_actions(), dict)))

    @property
    def post_purchase_email_provider(self):
        return self.config.get("post_purchase_email_provider", "smtp2go")

    @property
    def post_purchase_vault_fields(self):
        if not self.has_post_purchase:
            return ()
        return ("post_purchase_token", *("post_purchase_" + field
                                        for field in email_handoff_fields(self.post_purchase_email_provider)))

    def initial_post_purchase_secrets(self):
        actions = self.post_purchase_actions()
        entries = [actions] if isinstance(actions, dict) else actions
        if (any(a["recipient"].lower() != self.credentials["approved_test_email"].lower()
                or a["sender"].lower() != self.credentials["test_sender"].lower() for a in entries)):
            raise ValueError("protected post-purchase sender/recipient differ from the approved credential handoff")
        return {"post_purchase_token": secrets.token_hex(32),
                **{"post_purchase_" + key: self.credentials[key]
                   for key in email_handoff_fields(self.post_purchase_email_provider)}}

    def check_post_purchase_handoff(self):
        if self.has_post_purchase:
            vault = self.vault()
            if not self.credentials or any(self.credentials.get(key) != vault["post_purchase_" + key]
                                           for key in email_handoff_fields(self.post_purchase_email_provider)):
                raise ValueError("post-purchase credential handoff differs from the pinned test vault")

    def require_post_purchase_off(self):
        if self.has_post_purchase and (self.state.get("post_purchase_transition")
                                       or self.state.get("post_purchase_modes", OFF) != OFF):
            raise ValueError("finish post-purchase-disable before changing the commerce release")

    def post_purchase_fence(self):
        # Import lazily: the standalone initializer itself imports Installation.
        root = str(Path(__file__).resolve().parents[3])
        if root not in sys.path:
            sys.path.insert(0, root)
        from scripts.post_purchase_installation import classify, statements

        merchant = self.config["store_key"]
        query, = statements(merchant)
        rows = self.api.query(self.db("commerce"), query["sql"], query["params"])
        if len(rows) != 1:
            raise ValueError("post-purchase fence status is unavailable")
        status, = rows
        classification = classify(status, merchant)
        watermark = self.state.get("post_purchase_watermark")
        if watermark is not None and (classification != "retained" or status["activation_watermark"] != watermark):
            raise ValueError("post-purchase activation watermark differs from the retained fence")
        return {"status": classification, **{key: status[key] for key in (
            "activation_watermark", "last_outbox_id", "execution_count")}}

    def check_post_purchase_release(self, build):
        if (build.get("post_purchase_config_sha256") != self.config["post_purchase_config_sha256"]
                or build.get("merchant_inputs_sha256") != self.config["merchant_inputs_sha256"]
                or build.get("merchant_config_sha256") != self.merchant_config_hash()
                or build.get("merchant_lock_sha256") != hashlib.sha256(
                    Path(self.config["preflight_lock"]).read_bytes()).hexdigest()
                or build.get("schema") != self.state.get("migration_ledgers")
                or build.get("post_purchase_vault_fingerprints") != {
                    key: self.state["secret_fingerprints"][key] for key in self.post_purchase_vault_fields}
                or set(build.get("artifacts", {})) != set(self.roles)):
            raise ValueError("retained release differs from the post-purchase input, vault or schema contract")

    def record_post_purchase_off(self, build):
        deployment = self.state["deployment"]
        deployment["post_purchase_disabled_versions"] = {
            role: deployment["versions"][role] for role in POST_PURCHASE_ROLES
        }
        self.state["post_purchase_modes"] = dict(OFF)
        release = {**build, "versions": dict(deployment["versions"]), "record_revision": 0,
                   "sending": deployment.get("sending", False), "post_purchase_modes": dict(OFF)}
        if not self.state["releases"] or self.state["releases"][-1] != release:
            self.state["releases"].append(release)

    def post_purchase_runtime(self):
        if not self.has_post_purchase:
            raise ValueError("this installation has no protected post-purchase configuration")
        if self.api is None:
            raise ValueError("post-purchase operations require the documented protected credentials")
        deployment = self.state.get("deployment", {})
        disabled = deployment.get("post_purchase_disabled_versions", {})
        if (not deployment.get("complete") or set(disabled) != set(POST_PURCHASE_ROLES)
                or set(deployment.get("versions", {})) != set(self.roles)
                or self.state["phase"] in ("deploying", "rolling-back")):
            raise ValueError("finish the retained OFF deployment before changing post-purchase delivery")
        self.assert_resources()
        self.vault()
        build = json.loads((self.directory / "releases" / deployment["commit"] / "build.json").read_text())
        self.check_post_purchase_release(build)
        return deployment, disabled, build

    def check_post_purchase_version(self, role, mode, actual, version, email_provider=None, actions_override=None,
                                    allow_legacy_admission=False):
        nodes, _ = graph(self, post_purchase_modes={role: mode}, email_provider=email_provider,
                         actions_override=actions_override)
        errors = self.worker_binding_errors(role, nodes[role], actual, version)
        if allow_legacy_admission:
            present = {item["name"] for item in actual["resources"]["bindings"]}
            legacy_names = (("CHECKOUT_STORE_RATE_LIMITER", "CHECKOUT_STATUS_RATE_LIMITER", "CARTRAY_MERCHANT_ID")
                            if role == "source" else ("MERCHANT_ID", "MERCHANT_NAME", "STOREFRONT_ORIGIN"))
            for name in legacy_names:
                if name not in present:
                    missing = f"{role}.bindings.{name}: missing/wrong type"
                    errors = [error for error in errors if error != missing]
        if errors:
            raise ValueError("; ".join(errors))

    def check_current_post_purchase_off(self, rollback_targets=None):
        deployment = self.state.get("deployment", {})
        if not deployment.get("complete"):
            return
        for role in POST_PURCHASE_ROLES:
            actual = self.api.version(self.name(role))
            known = deployment.get("post_purchase_disabled_versions", {}).get(role)
            recovering = (rollback_targets or {}).get(role)
            if actual["id"] not in {known, recovering}:
                raise ValueError(f"{role}: not running a recorded OFF version; resolve drift before release changes")
            self.check_post_purchase_version(role, "disabled", actual, actual["id"],
                                             allow_legacy_admission=True)

    def post_purchase_live_version(self, role, deployment, transition):
        actual = self.api.version(self.name(role))
        disabled = deployment["post_purchase_disabled_versions"][role]
        recorded = deployment["versions"][role]
        candidate = transition.get("enable_versions", {}).get(role)
        if actual["id"] not in {disabled, recorded, candidate}:
            raise ValueError(f"{role}: active version is not part of the retained post-purchase transition")
        mode = "disabled" if actual["id"] == disabled else "test"
        self.check_post_purchase_version(role, mode, actual, actual["id"],
                                         allow_legacy_admission=actual["id"] in {disabled, recorded})
        return actual["id"]

    def post_purchase_visibility_errors(self, role):
        if role == 'post-purchase' or self.config.get('source_mode', 'public') == 'private':
            return self.worker_visibility_errors(role)
        actual = self.api.request(f"workers/scripts/{self.name(role)}/subdomain")
        return [] if actual.get('enabled') is True and actual.get('previews_enabled') is False else [
            f'{role}: public checkout endpoint is disabled or preview exposure differs from configuration']

    def set_post_purchase_version(self, role, target, mode, deployment, transition):
        current = self.post_purchase_live_version(role, deployment, transition)
        legacy = target == deployment["post_purchase_disabled_versions"][role]
        if current != target:
            staged = self.api.request(f"workers/scripts/{self.name(role)}/versions/{target}")
            self.check_post_purchase_version(role, mode, staged, target, allow_legacy_admission=legacy)
        private = role == 'post-purchase' or self.config.get('source_mode','public')=='private'
        self.set_worker_visibility(role, not private)
        if current != target:
            self.api.publish(self.name(role), target)
            self.set_worker_visibility(role, not private)
        self.check_post_purchase_version(role, mode, self.api.version(self.name(role)), target,
                                         allow_legacy_admission=legacy)
        errors = self.post_purchase_visibility_errors(role)
        if errors:
            raise ValueError("; ".join(errors))
        deployment["versions"][role] = target
        self.save()

    def finish_post_purchase_transition(self, deployment, operation, modes):
        self.state["post_purchase_modes"] = dict(modes)
        self.save()
        revision = 0
        if modes == OFF:
            build = json.loads((self.directory / "releases" / deployment["commit"] / "build.json").read_text())
            release = {**build, "versions": dict(deployment["versions"]), "record_revision": revision,
                       "sending": deployment.get("sending", False), "post_purchase_modes": dict(OFF)}
            if not self.state["releases"] or self.state["releases"][-1] != release:
                self.state["releases"].append(release)
        self.state.pop("post_purchase_transition")
        if getattr(self, 'consolidated', False) and self.config['reviews_enabled']:
            self.state['phase'] = 'active' if modes == ON else 'paused'
            self.state['activation_authorized'] = modes == ON
            self.state['automation_paused'] = modes != ON
        self.save()
        return {"post_purchase": operation, "modes": modes, "versions": dict(deployment["versions"]),
                "activation_watermark": self.state.get("post_purchase_watermark")}

    def post_purchase_activate(self, enable_test_delivery=False):
        if self.state.get("post_purchase_provider_migration"):
            raise ValueError("finish a fresh OFF build/deploy after email provider reconfiguration before activation")
        if not enable_test_delivery:
            raise ValueError("post-purchase activation requires explicit --enable-test-delivery")
        if not isinstance(self.post_purchase_actions(), dict):
            raise ValueError("legacy workflow delivery needs an explicit schema-2 order-confirmation handover")
        deployment, disabled, build = self.post_purchase_runtime()
        transition = self.state.get("post_purchase_transition")
        if transition and transition["operation"] != "activate":
            raise ValueError("finish post-purchase-disable before authorizing test delivery")
        self.check_schema()
        self.verify_merchant_inputs()
        self.check_post_purchase_handoff()
        self.provider_ready()
        fence = self.post_purchase_fence()
        if fence["status"] != "retained":
            raise ValueError("initialize the retained test no-backfill fence before post-purchase activation")
        if not transition:
            health = self.doctor()
            if not health["healthy"]:
                raise ValueError("post-purchase deployment differs; resolve doctor findings before activation")
            if self.state.get("post_purchase_modes") == ON:
                return {"post_purchase": "already-active", "versions": dict(deployment["versions"]),
                        "activation_watermark": self.state["post_purchase_watermark"]}
            if any(deployment["versions"][role] != disabled[role] for role in POST_PURCHASE_ROLES):
                raise ValueError("initial post-purchase activation requires both retained OFF versions")
            for role in POST_PURCHASE_ROLES:
                self.artifact_modules(role, build)
            transition = {"operation": "activate", "enable_versions": {}}
            self.state["post_purchase_transition"] = transition
            self.state["post_purchase_watermark"] = fence["activation_watermark"]
            self.save()
        # Validate both sides before any publish. Resume may observe a journalled
        # version already live if the preceding publish succeeded but its reply did not.
        for role in POST_PURCHASE_ROLES:
            self.post_purchase_live_version(role, deployment, transition)
        nodes, _ = graph(self, post_purchase_modes=ON)
        for role in POST_PURCHASE_ROLES:
            versions = transition["enable_versions"]
            if role not in versions:
                versions[role] = self.upload_worker(role, nodes[role], build)["id"]
                self.save()
            self.set_post_purchase_version(role, versions[role], "test", deployment, transition)
        return self.finish_post_purchase_transition(deployment, "active", ON)

    def post_purchase_disable(self):
        deployment, disabled, _ = self.post_purchase_runtime()
        transition = self.state.get("post_purchase_transition")
        if not transition and self.state.get("post_purchase_modes", OFF) == OFF:
            self.check_current_post_purchase_off()
            errors = [error for role in POST_PURCHASE_ROLES for error in self.post_purchase_visibility_errors(role)]
            if errors:
                raise ValueError("; ".join(errors))
            return {"post_purchase": "already-disabled", "modes": dict(OFF),
                    "versions": dict(deployment["versions"]),
                    "activation_watermark": self.state.get("post_purchase_watermark")}
        if not transition or transition["operation"] != "disable":
            previous = transition or {}
            transition = {"operation": "disable", "enable_versions": previous.get("enable_versions", {})}
            if previous.get("evidence_version") or previous.get("previous_evidence_version"):
                transition["previous_evidence_version"] = (
                    previous.get("evidence_version") or previous["previous_evidence_version"]
                )
            if self.config["reviews_enabled"] and self.state.get("activation_authorized"):
                transition["reviews_phase"] = previous.get("reviews_phase", self.state["phase"])
                self.state["phase"] = "post-purchase-transition"
            self.state["post_purchase_transition"] = transition
            self.save()
        self.set_post_purchase_version("source", disabled["source"], "disabled", deployment, transition)
        # Restart the entire interval on resume rather than trusting a persisted
        # wall clock or guessing when a publish with an uncertain response landed.
        time.sleep(SETTLE_SECONDS)
        self.set_post_purchase_version("post-purchase", disabled["post-purchase"], "disabled", deployment, transition)
        return self.finish_post_purchase_transition(deployment, "disabled", OFF)
