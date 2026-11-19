"""EP operator coverage with a provider fake, real fence SQL, and the existing email handler."""

import importlib.util
import json
import sqlite3
import stat
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from cloud import CloudError
from operate import ROOT, Installation, atomic_json, digest, validate_config
from post_purchase_operator import OFF, ON, SETTLE_SECONDS
from test_external_storefront import EXTERNAL, merchant
from topology import graph, secret_bindings, validate_post_purchase

ACCOUNT = "acct_1Ud2XZBKRMfYblCU"
MERCHANT = "effective-policy-uat"
LINK = "https://www.dropbox.com/t/N86okpWEWSXWDx9X"
CREDS = {"cloudflare_api_token": "cloud-fixture", "stripe_test_key": "sk_test_fixture",
         "smtp2go_api_key": "api-fixture", "approved_test_email": "test@mitchellcurrie.com",
         "test_sender": "approved@example.test"}
ACTION = {"environment": "test", "merchant": MERCHANT, "workflow_key": "ep:woo:workflow:8681",
          "config_ref": "ep/workflow/8681", "offer_keys": ["EP-UAT-171"], "links": [LINK],
          "recipient": CREDS["approved_test_email"], "sender": CREDS["test_sender"],
          "subject": "EP test materials", "stripe_account": ACCOUNT}
OFF_VISIBILITY = {"enabled": False, "previews_enabled": False}


def handoff(tmp_path):
    package = merchant(tmp_path / "package")
    for name in ("storefront-config.js", "site-config.js", "products/one/index.html"):
        path = package / "storefront" / name
        path.write_text(path.read_text().replace("MERCHANT-ONE", "EP-UAT-171"))
    (package / "commerce/catalogue.csv").write_text(
        "product_key,title,stripe_lookup_key,active\nEP-UAT-171,Product,ep-171-test,true\n")
    lock = tmp_path / "lock.json"
    atomic_json(lock, {"stripe_mode": "test", "catalogue_version": "sha256:" + "b" * 64,
                       "prices": [{"stripe_lookup_key": "ep-171-test", "amount_minor": 100}]})
    actions = tmp_path / "private-workflows.json"
    atomic_json(actions, {"schema": 1, "actions": [ACTION]})
    return {
        **EXTERNAL, "namespace": "ep-uat-test", "account_id": "a" * 32, "stripe_account_id": ACCOUNT,
        "preflight_lock": str(lock), "reviews_enabled": False, "merchant_name": "Merchant",
        "store_key": MERCHANT, "merchant_inputs": str(package), "subject_map": {}, "subject_details": [],
        "post_purchase_config": str(actions),
    }


@pytest.mark.parametrize("overrides", [
    {"stripe_account_id": "acct_NotApproved"}, {"store_key": "acorn-store"},
    {"source_mode": "public", "source_api_path": None},
])
def test_plan_rejects_wrong_installation_lane(tmp_path, overrides):
    options = handoff(tmp_path)
    options.update(overrides)
    if options["source_api_path"] is None:
        del options["source_api_path"]
    with pytest.raises(ValueError, match="approved EP"):
        validate_config(options)


def test_ep_test_lane_can_enable_reviews_independently(tmp_path):
    options = handoff(tmp_path)
    options["reviews_enabled"] = True
    config = validate_config(options)
    assert config["reviews_enabled"] is True
    assert config["source_mode"] == "private"


def test_combined_topology_preserves_independent_delivery_controls(tmp_path):
    options = handoff(tmp_path)
    options["reviews_enabled"] = True
    install = Installation.plan(tmp_path / "installation", options)
    install.state["workers_subdomain"] = "example"
    install.state["resources"]["database:commerce"] = {"id": "db"}
    for role in ("events", "schedule", "due", "dlq"):
        install.state["resources"]["queue:" + role] = {"id": role}
    nodes, _ = graph(install, sending=False, post_purchase_modes=OFF)
    assert set(nodes) == set(install.roles)
    assert nodes["source"]["services"]["REVIEWS_SOURCE_TRIGGER"] == "producer"
    assert nodes["source"]["services"]["POST_PURCHASE_HANDLER"] == "post-purchase"
    assert nodes["source"]["vars"]["CARTRAY_POST_PURCHASE_MODE"] == "disabled"
    assert nodes["sender"]["vars"]["INITIAL_SEND_MODE"] == "disabled"
    nodes, _ = graph(install, sending=True, post_purchase_modes=OFF)
    assert nodes["source"]["vars"]["CARTRAY_POST_PURCHASE_MODE"] == "disabled"
    assert nodes["sender"]["vars"]["INITIAL_SEND_MODE"] == "test"
    nodes, _ = graph(install, sending=False, post_purchase_modes=ON)
    assert nodes["source"]["vars"]["CARTRAY_POST_PURCHASE_MODE"] == "test"
    assert nodes["sender"]["vars"]["INITIAL_SEND_MODE"] == "disabled"


def test_post_purchase_transition_reverifies_before_resuming_reviews(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from post_purchase_operator import PostPurchaseOperator

    calls = []
    release = tmp_path / "releases" / "fixture"
    release.mkdir(parents=True)
    (release / "build.json").write_text('{"commit":"fixture"}')
    monkeypatch.setattr("post_purchase_operator.graph", lambda *args, **kwargs: ({"evidence": {}}, {}))
    live = {"id": "old-evidence"}

    def publish(name, version):
        calls.append(("publish", name, version))
        live["id"] = version

    subject = SimpleNamespace(
        state={"post_purchase_transition": {"reviews_phase": "active"}, "post_purchase_watermark": 42,
               "phase": "post-purchase-transition"},
        config={"reviews_enabled": True},
        directory=tmp_path,
        name=lambda role: role,
        upload_worker=lambda *args: {"id": "new-evidence"},
        api=SimpleNamespace(version=lambda name: dict(live), request=lambda path: {"id": "new-evidence"},
                            publish=publish),
        worker_binding_errors=lambda role, node, actual, version: [] if actual["id"] == version else ["drift"],
        save=lambda: calls.append("save"),
        verifier=lambda mode: calls.append("verify:" + mode) or {"revision": 1},
        recover_paused_hints=lambda: calls.append("recover"),
        configure_queues=lambda pause_processing: calls.append(("queue", pause_processing)),
    )
    deployment = {"versions": {"evidence": "old-evidence"}, "commit": "fixture"}
    result = PostPurchaseOperator.finish_post_purchase_transition(subject, deployment, "active", ON)
    assert deployment["versions"]["evidence"] == "new-evidence"
    assert calls.index(("publish", "evidence", "new-evidence")) < calls.index("verify:record")
    assert calls.index("verify:record") < calls.index(("queue", False))
    assert calls.index("recover") < calls.index(("queue", False))
    assert subject.state["phase"] == "active"
    assert subject.state["post_purchase_watermark"] == 42
    assert result["modes"] == ON

    calls.clear()
    live["id"] = "old-evidence"
    deployment["versions"]["evidence"] = "old-evidence"
    subject.state["post_purchase_transition"] = {}
    PostPurchaseOperator.finish_post_purchase_transition(subject, deployment, "active", ON)
    assert deployment["versions"]["evidence"] == "new-evidence"
    assert "verify:record" not in calls
    assert not any(isinstance(call, tuple) and call[0] == "queue" for call in calls)

    calls.clear()
    live["id"] = "unrecorded"
    subject.state["post_purchase_transition"] = {"reviews_phase": "active", "evidence_version": "new-evidence"}
    with pytest.raises(ValueError, match="unrecorded deployed version"):
        PostPurchaseOperator.finish_post_purchase_transition(subject, deployment, "active", ON)
    assert not any(isinstance(call, tuple) and call[0] == "queue" for call in calls)


@pytest.mark.parametrize("change", [
    {"offer_keys": ["EP-UAT-131"]}, {"offer_keys": ["EP-UAT-171", "EP-UAT-131"]},
    {"workflow_key": "ep:woo:workflow:131"}, {"recipient": "someone@example.test"},
    {"links": ["https://www.dropbox.com/t/other"]}, {"environment": "live"},
    {"stripe_account": "acct_Wrong"}, {"sender": "a@example.test\r\nBcc:x@example.test"},
])
def test_actions_cannot_exceed_approved_scope(change):
    with pytest.raises(ValueError):
        validate_post_purchase({"schema": 1, "actions": [{**ACTION, **change}]},
                               {"store_key": MERCHANT, "stripe_account_id": ACCOUNT},
                               {"EP-UAT-131", "EP-UAT-171"})


def test_action_snapshot_protection_and_fingerprint(tmp_path):
    options = handoff(tmp_path)
    target, input_path = tmp_path / "installation", Path(options["post_purchase_config"])
    input_path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        Installation.plan(target, options)
    input_path.chmod(0o600)
    symlink = tmp_path / "linked.json"
    symlink.symlink_to(input_path)
    with pytest.raises(OSError):
        Installation.plan(target, {**options, "post_purchase_config": str(symlink)})
    install = Installation.plan(target, options)
    snapshot = Path(install.config["post_purchase_config"])
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert install.config["post_purchase_config_sha256"] == digest(snapshot.read_bytes())
    assert ACTION["recipient"] not in install.state_path.read_text()
    assert LINK not in install.state_path.read_text()
    input_path.unlink()
    assert Installation(target).post_purchase_actions() == [ACTION]
    snapshot.write_bytes(snapshot.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="input changed"):
        install.post_purchase_actions()


def test_plan_requires_a_paid_active_offer_in_the_test_lock(tmp_path):
    options = handoff(tmp_path)
    path = Path(options["preflight_lock"])
    lock = json.loads(path.read_text())
    lock["prices"][0]["amount_minor"] = 0
    atomic_json(path, lock)
    with pytest.raises(ValueError, match="authorised commerce subset"):
        Installation.plan(tmp_path / "installation", options)


class Backend:
    def __init__(self, db):
        self.db = db
        self.active, self.versions, self.visibility, self.events = {}, {}, {}, []
        self.fail_publish = None

    def request(self, path, method="GET", body=None):
        if path == "d1/database/database":
            return {"name": "ep-uat-test-commerce"}
        if path.endswith("/schedules"):
            return {}
        _, _, worker, resource, *tail = path.split("/")
        if resource == "versions":
            return self.versions[tail[0]]
        assert resource == "subdomain"
        if method == "POST":
            self.visibility[worker] = body
        return self.visibility.get(worker, {})

    def query(self, database, sql, params=()):
        assert database == "database" and sql.startswith("SELECT")
        return [dict(row) for row in self.db.execute(sql, params)]

    def upload(self, name, modules, metadata):
        identifier = f"v{len(self.versions) + 1}"
        assert modules
        self.versions[identifier] = {"id": identifier, "resources": {"bindings": [
            {**b, **({"database_id": b["id"]} if b["type"] == "d1" else {})}
            for b in deepcopy(metadata["bindings"])]}}
        self.events.append(("upload", name))
        return {"id": identifier}

    def publish(self, name, identifier):
        assert self.visibility[name] == OFF_VISIBILITY
        self.active[name] = identifier
        self.events.append(("publish", name))
        if self.fail_publish == name:
            self.fail_publish = None
            raise CloudError("publish reply lost")

    def version(self, name):
        return self.versions[self.active[name]]


def capture_build(install, commit):
    release = install.directory / "releases" / commit
    source = release / "source/bundle/worker.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# retained source")
    handler = release / "post-purchase.mjs"
    handler.write_bytes((ROOT / "modules/post-purchase/transactional-email/worker.mjs").read_bytes())
    build = {"commit": commit, "artifacts": {"source": {"worker.py": digest(source.read_bytes())},
                                           "post-purchase": {"post-purchase.mjs": digest(handler.read_bytes())}},
             "schema": install.state["migration_ledgers"],
             "suppression_key_fingerprint": install.state["secret_fingerprints"]["recipient_hash"],
             "merchant_config_sha256": install.merchant_config_hash(),
             "merchant_inputs_sha256": install.config["merchant_inputs_sha256"],
             "merchant_lock_sha256": digest(Path(install.config["preflight_lock"]).read_bytes()),
             "post_purchase_config_sha256": install.config["post_purchase_config_sha256"],
             "post_purchase_vault_fingerprints": {
                 key: install.state["secret_fingerprints"][key] for key in install.post_purchase_vault_fields}}
    atomic_json(release / "build.json", build)
    install.state["build"] = commit
    install.save()
    return build


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    options = handoff(tmp_path)
    path = Installation.plan(tmp_path / "installation", options).directory
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE orders(order_id TEXT PRIMARY KEY); "
                     "CREATE TABLE outbox(id INTEGER PRIMARY KEY, order_id TEXT, event_type TEXT);"
                     "CREATE TABLE cartray_operator_migrations(namespace TEXT, position INT, path TEXT, checksum TEXT);"
                     "INSERT INTO cartray_operator_migrations VALUES ('commerce', 1, 'fixture', 'hash');")
    for migration in ("0006_post_purchase_actions.sql", "0007_post_purchase_checkpoint.sql"):
        db.executescript((ROOT / "migrations" / migration).read_text())
    api = Backend(db)
    install = Installation(path, deepcopy(CREDS), api)
    install.vault(create=True)
    install.state.update(workers_subdomain="account", phase="migrated", migration_ledgers={
        "commerce": [{"position": 1, "path": "fixture", "checksum": "hash"}]})
    install.state["resources"]["database:commerce"] = {"id": "database", "name": install.name("commerce")}
    for role in install.roles:
        install.state["resources"]["worker:" + role] = {"id": install.name(role), "name": install.name(role)}
    monkeypatch.setattr("operate.stripe", lambda key, path, *args, **kwargs: (
        {"id": ACCOUNT} if path == "account" else {"id": "we_fixture", "secret": "whsec_fixture"}))
    install.provision_webhook()
    monkeypatch.setattr("post_purchase_operator.time.sleep", lambda seconds: api.events.append(("settle", seconds)))
    capture_build(install, "first")
    install.deploy()
    yield install, api, db
    db.close()


def initialise(install, db):
    spec = importlib.util.spec_from_file_location(
        "post_purchase_installation", ROOT / "scripts/post_purchase_installation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with db:
        db.execute("INSERT INTO outbox(id) VALUES (7)")
        for query in module.statements(install.config["store_key"], apply=True):
            db.execute(query["sql"], query["params"]).fetchall()


def test_combined_delivery_transition_keeps_reviews_active_and_fence(deployed, monkeypatch):
    install, api, db = deployed
    initialise(install, db)
    install.config["reviews_enabled"] = True
    install.state.update(phase="active", activation_authorized=True, activation_watermark=17,
                         automation_paused=False, queue_recovery_required=False)
    for role in ("events", "schedule", "due", "dlq"):
        install.state["resources"]["queue:" + role] = {"id": role, "name": install.name(role)}
    build_path = install.directory / "releases" / install.state["build"] / "build.json"
    build = json.loads(build_path.read_text())
    build["merchant_config_sha256"] = install.merchant_config_hash()
    for role in install.roles:
        if role not in build["artifacts"]:
            filename = "worker.py" if role == "reviews" else role + ".mjs"
            path = (build_path.parent / role / "bundle" / filename if role == "reviews"
                    else build_path.parent / filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic")
            build["artifacts"][role] = {filename: digest(path.read_bytes())}
    atomic_json(build_path, build)
    nodes, _ = graph(install, sending=True, post_purchase_modes=OFF)
    versions = {}
    for role in install.roles:
        name = install.name(role)
        api.visibility[name] = OFF_VISIBILITY
        metadata = {"bindings": nodes[role]["bindings"] + secret_bindings(install, role)}
        versions[role] = api.upload(name, [("fixture", b"fixture", "text/plain")], metadata)["id"]
        api.active[name] = versions[role]
    install.state["deployment"] = {"complete": True, "commit": install.state["build"], "versions": versions,
                                   "sending": True, "post_purchase_disabled_versions": {
                                       role: versions[role] for role in ("source", "post-purchase")}}
    install.state["post_purchase_modes"] = dict(OFF)
    install.state["releases"] = []
    events = []
    monkeypatch.setattr(install, "assert_resources", lambda: None)
    monkeypatch.setattr(install, "check_schema", lambda: None)
    monkeypatch.setattr(install, "verify_merchant_inputs", lambda: None)
    monkeypatch.setattr(install, "provider_ready", lambda: None)
    monkeypatch.setattr(install, "doctor", lambda: {"healthy": True})
    monkeypatch.setattr(install, "configure_queues",
                        lambda pause_processing=False: events.append(("queue", pause_processing)))
    monkeypatch.setattr(install, "recover_paused_hints", lambda: events.append(("recover",)))
    monkeypatch.setattr(install, "verifier", lambda mode: events.append(("verify", mode)) or {"revision": 9})
    sender = versions["sender"]
    old_evidence = versions["evidence"]
    assert install.post_purchase_activate(True)["modes"] == ON
    assert install.state["activation_watermark"] == 17
    assert install.state["deployment"]["versions"]["sender"] == sender
    assert install.state["deployment"]["versions"]["evidence"] != old_evidence
    assert events.index(("verify", "record")) < events.index(("queue", False))
    assert install.post_purchase_disable()["modes"] == OFF
    assert install.state["activation_watermark"] == 17
    assert install.state["deployment"]["versions"]["sender"] == sender
    assert install.state["releases"][-1]["post_purchase_modes"] == OFF
    before = list(api.events), list(events), list(install.state["releases"])
    assert install.post_purchase_disable()["post_purchase"] == "already-disabled"
    assert (api.events, events, install.state["releases"]) == before


def test_recorded_legacy_source_can_upgrade_but_new_source_requires_rate_bindings(deployed):
    install, api, _ = deployed
    name = install.name("source")
    actual = api.version(name)
    bindings = actual["resources"]["bindings"]
    bindings[:] = [item for item in bindings if item["name"] not in {
        "CHECKOUT_STATUS_RATE_LIMITER", "CHECKOUT_STORE_RATE_LIMITER"
    }]
    install.check_current_post_purchase_off()
    with pytest.raises(ValueError, match="CHECKOUT_.*_RATE_LIMITER"):
        install.check_post_purchase_version("source", "disabled", actual, actual["id"])
    bindings.append({"name": "CHECKOUT_STATUS_RATE_LIMITER", "type": "plain_text", "text": "wrong"})
    with pytest.raises(ValueError, match="CHECKOUT_STATUS_RATE_LIMITER"):
        install.check_current_post_purchase_off()


CF_CREDS = {**CREDS, "cloudflare_email_api_token": "v1.0-account-email-fixture-" * 3,
            "test_sender": "enquiry@effectivepolicy.com.au"}


def email_migration_install(install, api, credentials=None):
    handoff = deepcopy(CF_CREDS if credentials is None else credentials)
    handoff.pop("smtp2go_api_key", None)
    return Installation(install.directory, handoff, api, credential_provider="cloudflare")


def test_explicit_off_provider_sender_migration_then_fresh_off_deploy(deployed):
    original, api, db = deployed
    original.post_purchase_fence()
    initialise(original, db)
    before_db = list(db.iterdump())
    before_vault = original.vault()
    previous_actions = original.post_purchase_actions()
    previous_hash = original.config["post_purchase_config_sha256"]
    before_events = list(api.events)
    install = email_migration_install(original, api)
    with pytest.raises(ValueError, match="allow-sender-change"):
        install.post_purchase_reconfigure_provider("cloudflare")
    assert install.post_purchase_reconfigure_provider("cloudflare", True)["next"] == (
        "build a different reviewed commit and deploy OFF")
    assert list(db.iterdump()) == before_db
    assert api.events == before_events  # Reads only: no upload, publish, send or D1 write.
    assert "build" not in install.state
    assert install.state["post_purchase_modes"] == OFF
    assert install.config["post_purchase_config_sha256"] != previous_hash
    assert install.post_purchase_actions() == [{**a, "sender": CF_CREDS["test_sender"]} for a in previous_actions]
    for key, value in before_vault.items():
        assert install.vault()[key] == (CF_CREDS["test_sender"] if key == "post_purchase_test_sender" else value)
    assert CF_CREDS["cloudflare_email_api_token"] not in install.state_path.read_text()
    assert CF_CREDS["test_sender"] not in install.state_path.read_text()
    secrets = {b["name"]: b["text"] for b in secret_bindings(install, "post-purchase")}
    assert secrets["CLOUDFLARE_EMAIL_API_TOKEN"] == CF_CREDS["cloudflare_email_api_token"]
    assert "SMTP2GO_API_KEY" not in secrets
    assert graph(install)[0]["post-purchase"]["vars"]["EMAIL_PROVIDER"] == "cloudflare"
    install.check_current_post_purchase_off()  # Retained old provider/action digest is still verifiable.
    with pytest.raises(ValueError, match="fresh OFF"):
        install.post_purchase_activate(True)
    assert install.post_purchase_reconfigure_provider("cloudflare", True)["provider"] == "cloudflare"
    capture_build(install, "cloudflare-new-clean-commit")
    install.deploy()
    assert "post_purchase_provider_migration" not in install.state
    assert install.state["post_purchase_provider_migrations"][-1]["status"] == "complete"
    assert install.doctor()["healthy"]
    assert install.post_purchase_activate(True)["modes"] == ON
    install.post_purchase_disable()
    with pytest.raises(ValueError, match="retained release differs"):
        install.rollback(-2)  # Cannot restore a mismatched provider/sender/digest contract.


@pytest.mark.parametrize("execution_state", ["claimed", "accepted", "uncertain"])
def test_sender_change_rejects_execution_ahead_of_checkpoint(deployed, execution_state):
    original, api, db = deployed
    initialise(original, db)
    with db:
        db.execute("INSERT INTO orders VALUES ('purchase')")
        db.execute("INSERT INTO outbox VALUES (8,'purchase','OrderConfirmed')")
        db.execute("INSERT INTO post_purchase_executions VALUES (?,?,?,?,?,?,NULL,0,0)",
                   (MERCHANT, "test", "purchase", "workflow", "digest", execution_state))
    install = email_migration_install(original, api)
    with pytest.raises(ValueError, match="beyond the post-purchase checkpoint"):
        install.post_purchase_reconfigure_provider("cloudflare", True)
    assert "post_purchase_provider_migration" not in install.state


def test_shared_reviews_sender_cannot_change_through_delivery_reconfiguration(deployed):
    original, api, db = deployed
    initialise(original, db)
    install = email_migration_install(original, api)
    install.config["reviews_enabled"] = True
    with pytest.raises(ValueError, match="Reviews shares the sender"):
        install.post_purchase_reconfigure_provider("cloudflare", True)
    assert "post_purchase_provider_migration" not in install.state


@pytest.mark.parametrize("boundary", ["vault", "actions", "inventory"])
def test_email_migration_resumes_after_each_local_write_boundary(deployed, monkeypatch, boundary):
    import operate

    original, api, db = deployed
    before_db = list(db.iterdump())
    install = email_migration_install(original, api)
    write = operate.atomic_json
    failed = False

    def interrupted(path, value):
        nonlocal failed
        is_target = ((boundary == "vault" and path == install.vault_path)
                     or (boundary == "actions" and path == Path(install.config["post_purchase_config"]))
                     or (boundary == "inventory" and path == install.state_path
                         and value.get("post_purchase_provider_migration", {}).get("status") == "awaiting-off-deploy"))
        if is_target and not failed:
            failed = True
            if boundary != "inventory":
                write(path, value)
            raise OSError("simulated local rename interruption")
        write(path, value)

    monkeypatch.setattr(operate, "atomic_json", interrupted)
    with pytest.raises(OSError, match="interruption"):
        install.post_purchase_reconfigure_provider("cloudflare", True)
    monkeypatch.setattr(operate, "atomic_json", write)
    resumed = email_migration_install(original, api)
    assert resumed.state["post_purchase_provider_migration"]["status"] == "staging"
    assert resumed.post_purchase_reconfigure_provider("cloudflare", True)["provider"] == "cloudflare"
    assert resumed.vault()["post_purchase_test_sender"] == CF_CREDS["test_sender"]
    resumed.check_current_post_purchase_off()
    assert list(db.iterdump()) == before_db


@pytest.mark.parametrize("change", [{"approved_test_email": "other@example.test"},
                                   {"stripe_test_key": "sk_test_different"}])
def test_email_migration_cannot_change_recipient_or_stripe_identity(deployed, change):
    original, api, _ = deployed
    install = email_migration_install(original, api, {**CF_CREDS, **change})
    with pytest.raises(ValueError, match="cannot change"):
        install.post_purchase_reconfigure_provider("cloudflare", True)
    assert "post_purchase_provider_migration" not in install.state


def test_email_migration_rejects_active_runtime_and_public_visibility(deployed):
    original, api, db = deployed
    original.post_purchase_fence()
    initialise(original, db)
    original.post_purchase_activate(True)
    install = email_migration_install(original, api)
    with pytest.raises(ValueError, match="post-purchase-disable"):
        install.post_purchase_reconfigure_provider("cloudflare", True)
    original.post_purchase_disable()
    api.visibility[original.name("source")] = {"enabled": True, "previews_enabled": False}
    install = email_migration_install(original, api)
    with pytest.raises(ValueError, match="remain private"):
        install.post_purchase_reconfigure_provider("cloudflare", True)
    assert "post_purchase_provider_migration" not in install.state


def test_email_migration_rejects_modified_snapshot_and_changed_resume_credential(deployed):
    original, api, _ = deployed
    install = email_migration_install(original, api)
    install.post_purchase_reconfigure_provider("cloudflare", True)
    install.credentials["cloudflare_email_api_token"] = "another-token-value" * 3
    with pytest.raises(ValueError, match="pinned credential"):
        install.post_purchase_reconfigure_provider("cloudflare", True)
    install.credentials = deepcopy(CF_CREDS)
    bundle = install.directory / "inputs" / install.state["post_purchase_provider_migration"]["bundle"]
    bundle.write_bytes(bundle.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="snapshot changed"):
        install.post_purchase_reconfigure_provider("cloudflare", True)


def test_email_migration_cannot_restart_during_replacement_deployment(deployed):
    original, api, _ = deployed
    install = email_migration_install(original, api)
    install.post_purchase_reconfigure_provider("cloudflare", True)
    install.state["phase"] = "deploying"
    with pytest.raises(ValueError, match="interrupted OFF deployment"):
        install.post_purchase_reconfigure_provider("cloudflare", True)


def test_disabled_deploy_and_real_handler_digest(deployed):
    install, api, _ = deployed
    nodes, urls = graph(install)
    assert set(install.roles) == {"source", "post-purchase"}
    assert install.queues == ()
    assert all(not node["public"] for node in nodes.values())
    assert install.doctor()["post_purchase"] == {"modes": OFF, "fence": {
        "status": "uninitialised", "activation_watermark": None, "last_outbox_id": None, "execution_count": 0}}
    assert urls["source"] == EXTERNAL["storefront_origin"] + EXTERNAL["source_api_path"]
    source = nodes["source"]
    assert source["vars"]["CARTRAY_POST_PURCHASE_MODE"] == "disabled"
    assert source["services"] == {"POST_PURCHASE_HANDLER": "post-purchase"}
    source_secrets = {b["name"]: b["text"] for b in secret_bindings(install, "source")}
    handler_secrets = {b["name"]: b["text"] for b in secret_bindings(install, "post-purchase")}
    assert source_secrets["CARTRAY_POST_PURCHASE_TOKEN"] == handler_secrets["HANDLER_TOKEN"]
    assert not any("REVIEWS" in key for key in source_secrets)
    assert handler_secrets["STRIPE_API_KEY"] == CREDS["stripe_test_key"]
    assert handler_secrets["SMTP2GO_API_KEY"] == CREDS["smtp2go_api_key"]
    assert LINK not in source["vars"]["CARTRAY_POST_PURCHASE_CONFIG"]
    assert CREDS["smtp2go_api_key"] not in install.state_path.read_text()
    assert api.events == [("upload", install.name("post-purchase")), ("publish", install.name("post-purchase")),
                          ("upload", install.name("source")), ("publish", install.name("source"))]

    handler = ROOT / "modules/post-purchase/transactional-email/worker.mjs"
    subprocess.run(["node", "--input-type=module", "-e", """
        import assert from 'node:assert/strict';
        import {readFileSync} from 'node:fs';
        const {handle} = await import(process.argv[1]);
        const {env, source} = JSON.parse(readFileSync(0, 'utf8'));
        const action = {...source.actions[0], merchant: source.merchant, environment: source.environment,
                        order_id: 'order-fixture', session_id: 'cs_test_fixture', items: [['EP-UAT-171', 1]]};
        const calls = [];
        const fake = async (url, options) => {
            calls.push(url);
            if (url.endsWith('/v1/account')) return Response.json({id: env.STRIPE_ACCOUNT_ID});
            if (url.endsWith('/cs_test_fixture')) return Response.json({
              id: action.session_id, livemode: false, mode: 'payment', status: 'complete', payment_status: 'paid',
              metadata: {cr_order_id: action.order_id}, customer_details: {email: env.APPROVED_TEST_RECIPIENT},
            });
            assert.deepEqual(JSON.parse(options.body).to, [env.APPROVED_TEST_RECIPIENT]);
            return Response.json({data: {succeeded: 1, failed: 0, email_id: 'provider-fixture'}});
        };
        const req = body => new Request('https://handler.invalid/', {method: 'POST',
          headers: {authorization: 'Bearer ' + env.HANDLER_TOKEN}, body: JSON.stringify(body)});
        assert.equal((await handle(req(action), env, fake)).status, 403);
        env.DELIVERY_MODE = 'test';
        assert.equal((await handle(req({...action, items: [['EP-UAT-131', 1]]}), env, fake)).status, 400);
        assert.equal(calls.length, 0);
        assert.deepEqual(await (await handle(req(action), env, fake)).json(), {provider_reference: 'provider-fixture'});
        assert.equal(calls.length, 3);
    """, handler.as_uri()], input=json.dumps({
        "env": {**nodes["post-purchase"]["vars"], **handler_secrets},
        "source": json.loads(source["vars"]["CARTRAY_POST_PURCHASE_CONFIG"]),
    }), text=True, check=True, cwd=ROOT)


def test_activation_requires_flag_fence_and_unchanged_test_credential(deployed):
    install, api, db = deployed
    api.events.clear()
    with pytest.raises(ValueError, match="explicit"):
        install.post_purchase_activate()
    with pytest.raises(ValueError, match="no-backfill fence"):
        install.post_purchase_activate(True)
    initialise(install, db)
    install.credentials["stripe_test_key"] = "sk_test_different"
    with pytest.raises(ValueError, match="pinned test vault"):
        install.post_purchase_activate(True)
    assert api.events == []
    assert "post_purchase_transition" not in install.state


@pytest.mark.parametrize("interrupted_role", ["post-purchase", "source"])
def test_activation_resumes_a_published_version_without_reupload(deployed, interrupted_role):
    install, api, db = deployed
    initialise(install, db)
    before = list(db.iterdump())
    api.events.clear()
    api.fail_publish = install.name(interrupted_role)
    with pytest.raises(CloudError, match="reply lost"):
        install.post_purchase_activate(True)
    reload = Installation(install.directory, CREDS, api)
    assert reload.doctor()["post_purchase"]["pending"] == "activate"
    assert reload.post_purchase_activate(True)["modes"] == ON
    assert reload.doctor()["healthy"]
    assert reload.state["activation_authorized"] is False
    assert reload.post_purchase_activate(True)["post_purchase"] == "already-active"
    assert api.events == [("upload", install.name("post-purchase")), ("publish", install.name("post-purchase")),
                          ("upload", install.name("source")), ("publish", install.name("source"))]
    assert list(db.iterdump()) == before


def test_disable_resumes_source_first_and_never_touches_the_database(deployed, monkeypatch):
    install, api, db = deployed
    initialise(install, db)
    install.post_purchase_activate(True)
    with db:
        db.execute("INSERT INTO outbox(id) VALUES (8)")
        db.execute("UPDATE post_purchase_checkpoint SET last_outbox_id=8")
        db.execute("INSERT INTO orders VALUES ('order-fixture')")
        db.execute("INSERT INTO post_purchase_executions VALUES (?,?,?,?,?,'claimed',NULL,0,0)",
                   (MERCHANT, "test", "order-fixture", ACTION["workflow_key"], "a" * 64))
    before = list(db.iterdump())
    with pytest.raises(ValueError, match="finish post-purchase-disable"):
        install.rollback(0)
    with pytest.raises(ValueError, match="finish post-purchase-disable"):
        install.deploy()
    api.events.clear()
    api.fail_publish = install.name("source")
    with pytest.raises(CloudError, match="reply lost"):
        install.post_purchase_disable()
    monkeypatch.setattr("operate.stripe", lambda *_: pytest.fail("disable must not depend on Stripe"))
    reload = Installation(install.directory, CREDS, api)
    assert reload.post_purchase_disable()["modes"] == OFF
    assert reload.doctor()["post_purchase"]["fence"]["last_outbox_id"] == 8
    assert api.events == [("publish", install.name("source")), ("settle", SETTLE_SECONDS),
                          ("publish", install.name("post-purchase"))]
    assert list(db.iterdump()) == before
    events, releases = list(api.events), list(reload.state["releases"])
    assert reload.post_purchase_disable()["post_purchase"] == "already-disabled"
    assert api.events == events
    assert reload.state["releases"] == releases


def test_pending_activation_can_be_disabled_and_reactivation_cannot_skip_settle(deployed):
    install, api, db = deployed
    initialise(install, db)
    api.fail_publish = install.name("post-purchase")
    with pytest.raises(CloudError):
        install.post_purchase_activate(True)
    api.fail_publish = install.name("post-purchase")
    with pytest.raises(CloudError):
        install.post_purchase_disable()
    resumed = Installation(install.directory, CREDS, api)
    with pytest.raises(ValueError, match="finish post-purchase-disable"):
        resumed.post_purchase_activate(True)
    assert resumed.post_purchase_disable()["modes"] == OFF
    assert api.events.count(("settle", SETTLE_SECONDS)) == 2
    assert resumed.doctor()["healthy"]


def test_doctor_reports_private_visibility_token_and_fence_scope_drift(deployed):
    install, api, db = deployed
    initialise(install, db)
    handler = install.name("post-purchase")
    api.visibility[handler] = {**OFF_VISIBILITY, "previews_enabled": True}
    bindings = api.version(handler)["resources"]["bindings"]
    bindings[:] = [b for b in bindings if b["name"] != "HANDLER_TOKEN"]
    with db:
        db.execute("INSERT INTO orders VALUES ('order-foreign')")
        db.execute("INSERT INTO post_purchase_executions VALUES (?,?,?,?,?,'claimed',NULL,0,0)",
                   ("foreign", "test", "order-foreign", ACTION["workflow_key"], "a" * 64))
    health = install.doctor()
    assert not health["healthy"]
    assert any("post-purchase.visibility.previews_enabled" in error for error in health["errors"])
    assert any("HANDLER_TOKEN" in error for error in health["errors"])
    assert any("scope/checkpoint mismatch" in error for error in health["errors"])


def test_rollback_only_to_compatible_off_versions(deployed):
    install, api, db = deployed
    initialise(install, db)
    before = list(db.iterdump())
    first = dict(install.state["deployment"]["versions"])
    capture_build(install, "second")
    install.deploy()
    release = install.state["releases"][0]
    original = release["post_purchase_config_sha256"]
    release["post_purchase_config_sha256"] = "0" * 64
    api.events.clear()
    with pytest.raises(ValueError, match="retained release differs"):
        install.rollback(0)
    assert api.events == []
    release["post_purchase_config_sha256"] = original
    assert not install.rollback(0)["database_rewind"]
    assert install.state["deployment"]["versions"] == first
    assert install.doctor()["post_purchase"]["modes"] == OFF
    assert all(api.visibility[install.name(role)] == OFF_VISIBILITY for role in install.roles)
    assert list(db.iterdump()) == before


@pytest.mark.parametrize("enabled_role,mode_var", [
    ("source", "CARTRAY_POST_PURCHASE_MODE"), ("post-purchase", "DELIVERY_MODE"),
])
def test_rollback_rejects_a_test_version_even_if_the_release_is_labelled_off(deployed, enabled_role, mode_var):
    install, api, db = deployed
    initialise(install, db)
    active = install.post_purchase_activate(True)["versions"]
    install.post_purchase_disable()
    before = list(db.iterdump())
    api.events.clear()
    install.state["releases"][0]["versions"][enabled_role] = active[enabled_role]
    with pytest.raises(ValueError, match=mode_var):
        install.rollback(0)
    assert not api.events
    assert install.doctor()["post_purchase"]["modes"] == OFF
    assert list(db.iterdump()) == before


def test_doctor_detects_remote_action_digest_and_modes_drift(deployed):
    install, api, _ = deployed
    for binding in api.version(install.name("source"))["resources"]["bindings"]:
        if binding["name"] == "CARTRAY_POST_PURCHASE_CONFIG":
            binding["text"] = binding["text"].replace('"config_digest":"', '"config_digest":"0')
        if binding["name"] == "CARTRAY_POST_PURCHASE_MODE":
            binding["text"] = "test"
    health = install.doctor()
    assert not health["healthy"]
    assert "source.vars.CARTRAY_POST_PURCHASE_CONFIG: differs from inventory" in health["errors"]
    assert "source.vars.CARTRAY_POST_PURCHASE_MODE: differs from inventory" in health["errors"]
