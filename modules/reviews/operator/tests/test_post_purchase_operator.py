"""EP operator coverage with a provider fake, real fence SQL, and the existing email handler."""

import importlib.util
import json
import sqlite3
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
    atomic_json(actions, {"schema": 2, "sender": CREDS["test_sender"], "recipient": CREDS["approved_test_email"]})
    return {
        **EXTERNAL, "namespace": "ep-uat-test", "account_id": "a" * 32, "stripe_account_id": ACCOUNT,
        "preflight_lock": str(lock), "reviews_enabled": False, "merchant_name": "Merchant",
        "store_key": MERCHANT, "merchant_inputs": str(package), "subject_map": {}, "subject_details": [],
        "post_purchase_config": str(actions),
    }




def test_other_merchant_can_plan_test_order_confirmation(tmp_path):
    options = handoff(tmp_path)
    options.update(store_key="other-merchant", stripe_account_id="acct_other")
    atomic_json(Path(options["post_purchase_config"]),
                {"schema": 2, "sender": CREDS["test_sender"], "recipient": CREDS["approved_test_email"]})
    install = Installation.plan(tmp_path / "installation", options)
    assert install.post_purchase_actions()["schema"] == 2
    install.state["workers_subdomain"] = "example"
    install.state["resources"]["database:commerce"] = {"id": "db"}
    nodes, _ = graph(install, post_purchase_modes=OFF)
    assert nodes["source"]["vars"]["CARTRAY_POST_PURCHASE_MODE"] == "disabled"
    assert "CARTRAY_POST_PURCHASE_CONFIG" not in nodes["source"]["vars"]


def test_ep_test_lane_can_enable_reviews_independently(tmp_path):
    options = handoff(tmp_path)
    options["reviews_enabled"] = True
    config = validate_config(options)
    assert config["reviews_enabled"] is True
    assert config["source_mode"] == "private"






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






class Backend:
    def __init__(self, db):
        self.db = db
        self.active, self.versions, self.visibility, self.events = {}, {}, {}, []
        self.fail_publish = None
        self.public_source = None

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
        if name != self.public_source:
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
def deployed(tmp_path, monkeypatch, request):
    options = handoff(tmp_path)
    atomic_json(Path(options["post_purchase_config"]),
                {"schema": 2, "sender": CREDS["test_sender"], "recipient": CREDS["approved_test_email"]})
    if getattr(request, 'param', None)=='public':
        options['source_mode']='public'
        for field in ('source_api_path','webhook_url'):
            options.pop(field,None)
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
    if options.get('source_mode')=='public':
        api.public_source = options['namespace']+'-source'
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
    assert "CARTRAY_POST_PURCHASE_CONFIG" not in source["vars"]
    assert CREDS["smtp2go_api_key"] not in install.state_path.read_text()
    assert api.events == [("upload", install.name("post-purchase")), ("publish", install.name("post-purchase")),
                          ("upload", install.name("source")), ("publish", install.name("source"))]

    handler = ROOT / "modules/post-purchase/transactional-email/worker.mjs"
    subprocess.run(["node", "--input-type=module", "-e", """
        import assert from 'node:assert/strict';
        import {readFileSync} from 'node:fs';
        const {handle} = await import(process.argv[1]);
        const {env, source} = JSON.parse(readFileSync(0, 'utf8'));
        const action = {type:'order_confirmation', merchant: source.merchant, environment: source.environment,
                        order_id: 'order-fixture', session_id: 'cs_test_fixture', access_url:null,
                        items: [{product_key:'EP-UAT-171',title:'Product',quantity:1}]};
        const calls = [];
        const fake = async (url, options) => {
            calls.push(url);
            if (url.endsWith('/v1/account')) return Response.json({id: env.STRIPE_ACCOUNT_ID});
            if (url.endsWith('/cs_test_fixture')) return Response.json({
              id: action.session_id, livemode: false, mode: 'payment', status: 'complete',
              payment_status: 'paid', amount_total:100,
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
        "source": {"merchant": install.config["store_key"], "environment": "test"},
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


def test_doctor_detects_remote_identity_and_modes_drift(deployed):
    install, api, _ = deployed
    for binding in api.version(install.name("source"))["resources"]["bindings"]:
        if binding["name"] == "CARTRAY_MERCHANT_ID":
            binding["text"] = "other-merchant"
        if binding["name"] == "CARTRAY_POST_PURCHASE_MODE":
            binding["text"] = "test"
    health = install.doctor()
    assert not health["healthy"]
    assert "source.vars.CARTRAY_MERCHANT_ID: differs from inventory" in health["errors"]
    assert "source.vars.CARTRAY_POST_PURCHASE_MODE: differs from inventory" in health["errors"]


def test_consolidated_reviews_use_only_the_selected_email_provider(tmp_path):
    options = handoff(tmp_path)
    options.update(reviews_enabled=True, post_purchase_email_provider='cloudflare')
    atomic_json(Path(options['post_purchase_config']),
                {'schema':2, 'sender':CREDS['test_sender'], 'recipient':CREDS['approved_test_email']})
    planned = Installation.plan(tmp_path / 'shared-email-install', options)
    credentials = {**CREDS, 'cloudflare_email_api_token':'v1.0-provider-fixture-'*3}
    credentials.pop('smtp2go_api_key')
    install = Installation(planned.directory, credentials)
    install.state['workers_subdomain'] = 'example'
    install.state['resources']['database:commerce'] = {'id':'db'}
    assert set(install.roles) == {'source','reviews','post-purchase'}
    assert not install.queues
    nodes, _ = graph(install, post_purchase_modes=OFF)
    assert nodes['post-purchase']['vars']['EMAIL_PROVIDER'] == 'cloudflare'
    assert 'REVIEWS_SOURCE_TRIGGER' not in nodes['source'].get('services',{})
    assert 'CARTRAY_REVIEWS_PRODUCER' not in nodes['source']['vars']
    assert nodes['reviews']['vars']['REVIEWS_RECOVERY_MODE'] == 'order_notifications'


def test_pages_merchant_can_use_public_checkout_with_private_mail_handler(tmp_path):
    options = handoff(tmp_path)
    options.update(source_mode='public', reviews_enabled=True)
    for field in ('source_api_path','webhook_url','storefront_origin'):
        options.pop(field, None)
    atomic_json(Path(options['post_purchase_config']),
                {'schema':2,'sender':CREDS['test_sender'],'recipient':CREDS['approved_test_email']})
    install = Installation.plan(tmp_path/'pages-mail-install', options)
    install.state.update(workers_subdomain='example',pages_subdomain='merchant.pages.dev')
    install.state['resources']['database:commerce']={'id':'db'}
    nodes, _ = graph(install, post_purchase_modes=OFF)
    assert nodes['source']['public'] is True
    assert not nodes['post-purchase'].get('public', False)
    assert nodes['source']['services']=={'POST_PURCHASE_HANDLER':'post-purchase'}
    assert not install.queues
