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
        if (any(a["recipient"].lower() != self.credentials["approved_test_email"].lower()
                or a["sender"].lower() != self.credentials["test_sender"].lower() for a in actions)):
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

    def provider_migration_bundle(self, migration):
        from operate import digest, read_private

        path = self.directory / "inputs" / migration["bundle"]
        if path.parent != self.directory / "inputs" or path.name != migration["bundle"]:
            raise ValueError("unsafe email provider migration snapshot")
        raw = read_private(path, raw=True)
        if digest(raw) != migration["bundle_sha256"]:
            raise ValueError("protected email provider migration snapshot changed")
        return json.loads(raw)

    def post_purchase_reconfigure_provider(self, provider, allow_sender_change=False, allow_credential_change=False):
        """Journal an explicit local provider/sender change; never publish, send or write D1."""
        from operate import atomic_json, digest, read_private

        if not self.has_post_purchase or provider not in PROVIDER_KEYS or not self.credentials:
            raise ValueError("email reconfiguration requires a protected post-purchase installation and credentials")
        self.require_post_purchase_off()
        validate_email_credential(self.credentials, provider)
        key = "post_purchase_" + PROVIDER_KEYS[provider]
        value = self.credentials[PROVIDER_KEYS[provider]]
        checksum = digest(value.encode())
        migration = self.state.get("post_purchase_provider_migration")
        if migration and (migration["to"] != provider or migration["credential_sha256"] != checksum):
            raise ValueError("resume the original email provider migration with its pinned credential")
        if migration and (self.state.get("phase") in ("deploying", "rolling-back")
                          or not self.state.get("deployment", {}).get("complete")
                          or any(self.state["deployment"]["versions"].get(role)
                                 != migration["disabled_versions"].get(role) for role in POST_PURCHASE_ROLES)):
            raise ValueError("finish the interrupted OFF deployment, not email reconfiguration")
        if not migration:
            vault = self.vault()
            if any(self.credentials.get(field) != vault["post_purchase_" + field]
                   for field in ("stripe_test_key", "approved_test_email")):
                raise ValueError("email reconfiguration cannot change the pinned Stripe key or recipient")
            sender_changed = self.credentials["test_sender"] != vault["post_purchase_test_sender"]
            if sender_changed and not allow_sender_change:
                raise ValueError("sender change requires explicit --allow-sender-change")
            if sender_changed and self.config["reviews_enabled"]:
                raise ValueError("post-purchase sender changes are unavailable while Reviews shares the sender handoff")
            credential_changed = key in vault and vault[key] != value
            if credential_changed and not allow_credential_change:
                raise ValueError("provider credential change requires explicit --allow-credential-change")
            if credential_changed and self.config["reviews_enabled"]:
                raise ValueError("provider credential change blocked: Reviews shares an unpinned SMTP handoff")
            if not sender_changed and not credential_changed and provider == self.post_purchase_email_provider:
                self.check_post_purchase_handoff()
                return {"post_purchase": "provider-unchanged", "provider": provider}
            deployment, disabled, _ = self.post_purchase_runtime()
            if any(deployment["versions"][role] != disabled[role] for role in POST_PURCHASE_ROLES):
                raise ValueError("email reconfiguration requires both recorded OFF versions")
            self.check_current_post_purchase_off()
            if sender_changed:
                self.check_sender_change_checkpoint()
            for role in POST_PURCHASE_ROLES:
                if self.worker_visibility_errors(role):
                    raise ValueError("email reconfiguration requires both Workers and previews to remain private")
            actions = self.post_purchase_actions()
            action_value = {"schema": 1, "actions": [{**a, "sender": self.credentials["test_sender"]} for a in actions]}
            raw_actions = (json.dumps(action_value, indent=2) + "\n").encode()
            self.parse_post_purchase_snapshot(raw_actions, self.config, Path(self.config["merchant_inputs"]),
                                              json.loads(Path(self.config["preflight_lock"]).read_text()))
            bundle = {"old_vault": vault, "new_vault": {
                **vault, key: value, "post_purchase_test_sender": self.credentials["test_sender"]},
                "old_actions_raw": read_private(Path(self.config["post_purchase_config"]), raw=True).decode(),
                "new_actions": action_value}
            revision = len(self.state.get("post_purchase_provider_migrations", []))
            bundle_name = f"email-provider-{revision}.json"
            bundle_raw = (json.dumps(bundle, indent=2) + "\n").encode()
            if len(bundle_raw) > 32768:
                raise ValueError("protected email migration snapshot exceeds bounds")
            bundle_path = self.directory / "inputs" / bundle_name
            if bundle_path.exists() and read_private(bundle_path, raw=True) != bundle_raw:
                raise ValueError("unrecorded email migration snapshot differs; restore before continuing")
            atomic_json(bundle_path, bundle)
            migration = {"from": self.post_purchase_email_provider, "to": provider, "status": "staging",
                         "credential_sha256": checksum, "disabled_versions": dict(disabled),
                         "bundle": bundle_name, "bundle_sha256": digest(bundle_raw),
                         "old_action_sha256": self.config["post_purchase_config_sha256"],
                         "new_action_sha256": digest(raw_actions)}
            self.state["post_purchase_provider_migration"] = migration
            self.save()
        bundle = self.provider_migration_bundle(migration)
        old, new = bundle["old_vault"], bundle["new_vault"]
        if key in old and old[key] != new[key] and not allow_credential_change:
            raise ValueError("provider credential change requires explicit --allow-credential-change")
        if key in old and old[key] != new[key] and self.config["reviews_enabled"]:
            raise ValueError("provider credential change blocked: Reviews shares an unpinned SMTP handoff")
        if any(self.credentials.get(field) != new["post_purchase_" + field]
               for field in email_handoff_fields(provider)):
            raise ValueError("resume email migration with its unchanged protected handoff")
        if old["post_purchase_test_sender"] != new["post_purchase_test_sender"] and not allow_sender_change:
            raise ValueError("sender change requires explicit --allow-sender-change")
        current = read_private(self.vault_path)
        if current not in (old, new):
            raise ValueError("vault differs from both recorded email migration snapshots")
        actions_path = Path(self.config["post_purchase_config"])
        current_action_hash = digest(read_private(actions_path, raw=True))
        if current_action_hash not in (migration["old_action_sha256"], migration["new_action_sha256"]):
            raise ValueError("actions differ from both recorded email migration snapshots")
        self.check_current_post_purchase_off()
        if old["post_purchase_test_sender"] != new["post_purchase_test_sender"]:
            if self.config["reviews_enabled"]:
                raise ValueError("post-purchase sender changes are unavailable while Reviews shares the sender handoff")
            self.check_sender_change_checkpoint()
        for role in POST_PURCHASE_ROLES:
            if self.worker_visibility_errors(role):
                raise ValueError("email reconfiguration requires both Workers and previews to remain private")
        # The write-ahead bundle authenticates both sides of each local rename.
        # Only this explicit command may resume across the multi-file boundary.
        atomic_json(self.vault_path, new)
        atomic_json(actions_path, bundle["new_actions"])
        if digest(read_private(actions_path, raw=True)) != migration["new_action_sha256"]:
            raise ValueError("email migration action serialization differs")
        self.state["secret_fingerprints"].update({
            key: checksum, "post_purchase_test_sender": digest(new["post_purchase_test_sender"].encode())})
        self.config["post_purchase_email_provider"] = provider
        self.state["config"]["post_purchase_email_provider"] = provider
        self.config["post_purchase_config_sha256"] = migration["new_action_sha256"]
        self.state["config"]["post_purchase_config_sha256"] = migration["new_action_sha256"]
        migration["status"] = "awaiting-off-deploy"
        self.state.pop("build", None)
        self.save()
        return {"post_purchase": "provider-configured", "provider": provider,
                "next": "build a different reviewed commit and deploy OFF"}

    def check_sender_change_checkpoint(self):
        # A terminal execution still carries the old immutable action digest.
        # It must not be encountered again after the sender is changed.
        rows = self.api.query(self.db("commerce"), """SELECT COUNT(*) AS pending FROM post_purchase_executions e
            WHERE e.merchant_id=? AND e.environment='test' AND NOT EXISTS (
              SELECT 1 FROM outbox o JOIN post_purchase_checkpoint c ON c.singleton=1
              WHERE c.merchant_id=e.merchant_id AND c.environment=e.environment
                AND o.order_id=e.order_id AND o.event_type='OrderConfirmed' AND o.id<=c.last_outbox_id)""",
                              [self.config["store_key"]])
        if len(rows) != 1 or rows[0]["pending"]:
            raise ValueError("sender change blocked: reconcile executions beyond the post-purchase checkpoint")

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
        if allow_legacy_admission and role == "source":
            present = {item["name"] for item in actual["resources"]["bindings"]}
            for name in ("CHECKOUT_STORE_RATE_LIMITER", "CHECKOUT_STATUS_RATE_LIMITER"):
                if name not in present:
                    missing = f"source.bindings.{name}: missing/wrong type"
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
            migration = self.state.get("post_purchase_provider_migration", {})
            previous = (migration.get("from") if actual["id"] == migration.get("disabled_versions", {}).get(role)
                        else None)
            actions = ([dict(sorted(a.items())) for a in
                        json.loads(self.provider_migration_bundle(migration)["old_actions_raw"])["actions"]]
                       if previous else None)
            self.check_post_purchase_version(role, "disabled", actual, actual["id"], previous, actions,
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

    def set_post_purchase_version(self, role, target, mode, deployment, transition):
        current = self.post_purchase_live_version(role, deployment, transition)
        legacy = target == deployment["post_purchase_disabled_versions"][role]
        if current != target:
            staged = self.api.request(f"workers/scripts/{self.name(role)}/versions/{target}")
            self.check_post_purchase_version(role, mode, staged, target, allow_legacy_admission=legacy)
        self.set_worker_visibility(role, False)
        if current != target:
            self.api.publish(self.name(role), target)
            self.set_worker_visibility(role, False)
        self.check_post_purchase_version(role, mode, self.api.version(self.name(role)), target,
                                         allow_legacy_admission=legacy)
        errors = self.worker_visibility_errors(role)
        if errors:
            raise ValueError("; ".join(errors))
        deployment["versions"][role] = target
        self.save()

    def finish_post_purchase_transition(self, deployment, operation, modes):
        self.state["post_purchase_modes"] = dict(modes)
        self.save()
        transition = self.state["post_purchase_transition"]
        revision = 0
        if self.config["reviews_enabled"]:
            nodes, _ = graph(self, deployment["versions"], sending=deployment.get("sending", False))
            if "evidence_version" not in transition:
                build = json.loads((self.directory / "releases" / deployment["commit"] / "build.json").read_text())
                transition["evidence_version"] = self.upload_worker("evidence", nodes["evidence"], build)["id"]
                self.save()
            evidence_version = transition["evidence_version"]
            actual = self.api.version(self.name("evidence"))
            if actual["id"] not in {
                deployment["versions"]["evidence"], evidence_version, transition.get("previous_evidence_version")
            }:
                raise ValueError("evidence: unrecorded deployed version; resolve drift before resuming Reviews")
            if actual["id"] != evidence_version:
                candidate = self.api.request(f"workers/scripts/{self.name('evidence')}/versions/{evidence_version}")
                errors = self.worker_binding_errors("evidence", nodes["evidence"], candidate, evidence_version)
                if errors:
                    raise ValueError("; ".join(errors))
                self.api.publish(self.name("evidence"), evidence_version)
            actual = self.api.version(self.name("evidence"))
            errors = self.worker_binding_errors("evidence", nodes["evidence"], actual, evidence_version)
            if errors:
                raise ValueError("; ".join(errors))
            deployment["versions"]["evidence"] = evidence_version
            self.save()
        if transition.get("reviews_phase"):
            revision = self.verifier("record")["revision"]
            if transition["reviews_phase"] == "active":
                self.recover_paused_hints()
            self.configure_queues(pause_processing=transition["reviews_phase"] != "active")
            self.state["queue_recovery_required"] = transition["reviews_phase"] != "active"
            self.state["phase"] = transition["reviews_phase"]
        if modes == OFF:
            build = json.loads((self.directory / "releases" / deployment["commit"] / "build.json").read_text())
            release = {**build, "versions": dict(deployment["versions"]), "record_revision": revision,
                       "sending": deployment.get("sending", False), "post_purchase_modes": dict(OFF)}
            if not self.state["releases"] or self.state["releases"][-1] != release:
                self.state["releases"].append(release)
        self.state.pop("post_purchase_transition")
        self.save()
        return {"post_purchase": operation, "modes": modes, "versions": dict(deployment["versions"]),
                "activation_watermark": self.state.get("post_purchase_watermark")}

    def post_purchase_activate(self, enable_test_delivery=False):
        if self.state.get("post_purchase_provider_migration"):
            raise ValueError("finish a fresh OFF build/deploy after email provider reconfiguration before activation")
        if not enable_test_delivery:
            raise ValueError("post-purchase activation requires explicit --enable-test-delivery")
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
            if self.config["reviews_enabled"] and self.state.get("activation_authorized"):
                transition["reviews_phase"] = self.state["phase"]
                self.state["phase"] = "post-purchase-transition"
            self.state["post_purchase_transition"] = transition
            self.state["post_purchase_watermark"] = fence["activation_watermark"]
            self.save()
        if transition.get("reviews_phase"):
            self.prepare_queue_transition()
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
            errors = [error for role in POST_PURCHASE_ROLES for error in self.worker_visibility_errors(role)]
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
        if transition.get("reviews_phase"):
            self.prepare_queue_transition()
        self.set_post_purchase_version("source", disabled["source"], "disabled", deployment, transition)
        # Restart the entire interval on resume rather than trusting a persisted
        # wall clock or guessing when a publish with an uncertain response landed.
        time.sleep(SETTLE_SECONDS)
        self.set_post_purchase_version("post-purchase", disabled["post-purchase"], "disabled", deployment, transition)
        return self.finish_post_purchase_transition(deployment, "disabled", OFF)
