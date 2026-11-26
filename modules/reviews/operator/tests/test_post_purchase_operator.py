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






@pytest.mark.parametrize('field',['sender','recipient'])
def test_schema2_rejects_malformed_mailboxes(field):
    value = {'schema':2,'sender':'sender@example.test','recipient':'recipient@example.test'}
    value[field] = 'a@example.test\r\nBcc:x@example.test'
    with pytest.raises(ValueError,match='approved sender and recipient'):
        validate_post_purchase(value,{},set())


def test_schema1_handoff_is_rejected_before_inventory_creation(tmp_path):
    options = handoff(tmp_path)
    atomic_json(Path(options['post_purchase_config']),{'schema':1,'actions':[ACTION]})
    target = tmp_path / 'installation'
    with pytest.raises(ValueError,match='per-workflow email configuration is retired'):
        Installation.plan(target,options)
    assert not (target / 'inventory.json').exists()



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
             "catalogue_config": install.catalogue_config(),
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
    credentials = deepcopy(CREDS)
    if getattr(request, 'param', None) == 'resources':
        options['fulfilment'] = {'buckets': {}}
        credentials['fulfilment_resources'] = '{}'
    path = Installation.plan(tmp_path / "installation", options).directory
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE orders(order_id TEXT PRIMARY KEY); "
                     "CREATE TABLE outbox(id INTEGER PRIMARY KEY, order_id TEXT, event_type TEXT);"
                     "CREATE TABLE cartray_operator_migrations(namespace TEXT, position INT, path TEXT, checksum TEXT);"
                     "INSERT INTO cartray_operator_migrations VALUES ('commerce', 1, 'fixture', 'hash');")
    for migration in ("0005_review_invitation_checkout_grants.sql", "0006_post_purchase_actions.sql",
                      "0007_post_purchase_checkpoint.sql"):
        db.executescript((ROOT / "migrations" / migration).read_text())
    api = Backend(db)
    if options.get('source_mode')=='public':
        api.public_source = options['namespace']+'-source'
    install = Installation(path, credentials, api)
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
        install.activate(True)
    reload = Installation(install.directory, CREDS, api)
    assert reload.doctor()["post_purchase"]["pending"] == "activate"
    assert reload.activate(True)["modes"] == ON
    assert reload.doctor()["healthy"]
    assert reload.state["activation_authorized"] is False
    assert reload.activate(True)["post_purchase"] == "already-active"
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


def test_legacy_activation_reports_handover_without_deleted_helpers(tmp_path,monkeypatch):
    install = Installation.plan(tmp_path / 'installation',handoff(tmp_path))
    install.config['reviews_enabled'] = True
    monkeypatch.setattr(install,'post_purchase_actions',lambda: ())
    with pytest.raises(ValueError,match='schema-2 order-confirmation handover'):
        install.post_purchase_activate(True)


def test_schema2_snapshot_keeps_existing_file_protection(tmp_path):
    options = handoff(tmp_path)
    target,input_path = tmp_path / 'installation',Path(options['post_purchase_config'])
    original = json.loads(input_path.read_text())
    input_path.chmod(0o644)
    with pytest.raises(ValueError,match='private regular file'):
        Installation.plan(target,options)
    input_path.chmod(0o600)
    symlink = tmp_path / 'linked.json'
    symlink.symlink_to(input_path)
    with pytest.raises(OSError):
        Installation.plan(target,{**options,'post_purchase_config':str(symlink)})
    install = Installation.plan(target,options)
    snapshot = Path(install.config['post_purchase_config'])
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert original['recipient'] not in install.state_path.read_text()
    input_path.unlink()
    assert Installation(target).post_purchase_actions() == original
    snapshot.write_bytes(snapshot.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='input changed'):
        install.post_purchase_actions()


def test_catalogue_rollback_resumes_after_publish_ack_loss(deployed, monkeypatch):
    install, api, db = deployed
    first = install.state['releases'][0]
    first['catalogue_config'] = install.catalogue_config()
    original_lock = Path(install.config['preflight_lock']).read_bytes()
    retained = install.directory / 'inputs/retained-first-lock.json'
    retained.write_bytes(original_lock)
    first['catalogue_config']['preflight_lock'] = 'inputs/retained-first-lock.json'
    first_versions = dict(first['versions'])
    replacement = {**install.catalogue_config(), 'subject_map': {'EP-UAT-171': 'subject-one'},
                   'subject_details': [['subject-one', '/products/one/', 'Product']]}
    revised_lock = json.loads(original_lock)
    revised_lock['catalogue_version'] = 'sha256:' + 'c' * 64
    atomic_json(install.directory / 'inputs/second-lock.json', revised_lock)
    replacement['preflight_lock'] = 'inputs/second-lock.json'
    install.use_catalogue_config(replacement)
    capture_build(install, 'second')
    install.deploy()
    install.state['deployment']['catalogue_config'] = replacement
    second_versions = dict(install.state['deployment']['versions'])
    before = list(db.iterdump())
    check = install.check_post_purchase_version

    def expected_catalogue(role, mode, actual, version, **kwargs):
        expected = {} if version == first_versions[role] else {'EP-UAT-171': 'subject-one'}
        assert version in (first_versions[role], second_versions[role])
        assert install.config['subject_map'] == expected
        return check(role, mode, actual, version, **kwargs)

    monkeypatch.setattr(install, 'check_post_purchase_version', expected_catalogue)
    api.fail_publish = install.name('source')
    with pytest.raises(CloudError, match='publish reply lost'):
        install.rollback(0)
    assert install.state['phase'] == 'rolling-back'
    assert install.rollback(0)['database_rewind'] is False
    assert install.state['deployment']['versions'] == first_versions
    assert install.config['subject_map'] == {}
    install.deploy()
    assert install.pause()['post_purchase'] == 'already-disabled'
    assert list(db.iterdump()) == before


@pytest.mark.parametrize('deployed', ['resources'], indirect=True)
def test_off_resource_binding_upgrade_uses_recorded_shape_then_incoming_shape(deployed, tmp_path):
    from test_merchant_inputs import catalogue_revision

    install, api, db = deployed
    key = install.vault()['order_access']
    before = list(db.iterdump())
    initial = api.version(install.name('source'))
    assert 'CARTRAY_RESOURCE_CONFIG' in {binding['name'] for binding in initial['resources']['bindings']}
    values = catalogue_revision(tmp_path / 'expanded')
    for index, size in enumerate((35, 125)):
        install.credentials['fulfilment_resources'] = json.dumps({
            f'resource-{i}': {'provider': 'external', 'url': f'https://example.test/resource/{i}',
                             'filename': 'Dummy ' + 'x' * 100} for i in range(size)})
        page = Path(values['merchant_inputs']) / 'storefront/cart/index.html'
        page.write_text(page.read_text() + f'<!-- revision {index} -->')
        install.reconfigure_catalogue(values)
        assert install.pause()['post_purchase'] == 'already-disabled'
        build = capture_build(install, f'resource-upgrade-{index}')
        build['resource_config_encoding'] = 'bounded-secrets-v1'
        build['resource_config_sha256'] = digest(install.credentials['fulfilment_resources'].encode())
        atomic_json(install.directory / 'releases' / build['commit'] / 'build.json', build)
        install.deploy()
        actual = api.version(install.name('source'))
        names = {binding['name'] for binding in actual['resources']['bindings']}
        assert 'CARTRAY_RESOURCE_CONFIG_META' in names
        assert 'CARTRAY_RESOURCE_CONFIG' not in names
        assert not install.worker_binding_errors('source', graph(install)[0]['source'], actual, actual['id'])
    assert install.vault()['order_access'] == key
    assert list(db.iterdump()) == before


@pytest.mark.parametrize('deployed', ['resources'], indirect=True)
def test_pause_reaches_recorded_off_versions_after_resource_handoff_changes(deployed):
    install, api, db = deployed
    initialise(install, db)
    install.post_purchase_activate(True)
    disabled = dict(install.state['deployment']['post_purchase_disabled_versions'])
    before = list(db.iterdump())
    install.credentials['fulfilment_resources'] = json.dumps({
        f'resource-{i}': {'provider': 'external', 'url': f'https://example.test/dummy/{i}',
                         'filename': 'Dummy ' + 'x' * 100} for i in range(100)})
    api.events.clear()
    install.pause()
    assert install.state['post_purchase_modes'] == OFF
    assert install.state['deployment']['versions'] == disabled
    assert not any(event[0] == 'upload' for event in api.events)
    assert list(db.iterdump()) == before
    with pytest.raises(ValueError, match='cannot decode multipart'):
        install.post_purchase_activate(True)


@pytest.mark.parametrize('deployed', ['resources'], indirect=True)
def test_pause_stops_unacknowledged_activation_after_resource_handoff_changes(deployed):
    install, api, db = deployed
    initialise(install, db)
    before = list(db.iterdump())
    disabled = dict(install.state['deployment']['post_purchase_disabled_versions'])
    api.fail_publish = install.name('source')
    with pytest.raises(CloudError, match='reply lost'):
        install.post_purchase_activate(True)
    assert api.version(install.name('source'))['id'] != disabled['source']
    install.credentials['fulfilment_resources'] = json.dumps({
        f'resource-{i}': {'provider': 'external', 'url': f'https://example.test/dummy/{i}',
                         'filename': 'Dummy ' + 'x' * 100} for i in range(100)})
    api.events.clear()
    install.pause()
    assert install.state['post_purchase_modes'] == OFF
    assert install.state['deployment']['versions'] == disabled
    assert not any(event[0] == 'upload' for event in api.events)
    assert list(db.iterdump()) == before


def test_recipient_policy_reconfiguration_preserves_installation_and_is_repeatable(deployed):
    install, api, db = deployed
    initialise(install, db)
    before_vault = install.vault_path.read_bytes()
    resources = deepcopy(install.state['resources'])
    fence = db.execute('SELECT * FROM post_purchase_checkpoint').fetchall()
    original = Path(install.config['post_purchase_config']).read_bytes()
    policy = {'addresses': ['Specific@another.test'], 'domains': ['EffectivePolicy.COM.AU', 'mitchellcurrie.com']}
    queries = []
    db.set_trace_callback(queries.append)
    result = install.reconfigure_test_recipients(policy)
    assert not any(q.lstrip().upper().startswith(('UPDATE', 'DELETE', 'INSERT', 'REPLACE')) for q in queries)
    assert result['changed'] is True
    assert install.vault_path.read_bytes() == before_vault
    assert install.state['resources'] == resources
    assert db.execute('SELECT * FROM post_purchase_checkpoint').fetchall() == fence
    assert (install.directory / 'inputs/post-purchase.json').read_bytes() == original
    assert 'build' not in install.state
    assert install.state['post_purchase_modes'] == OFF
    assert install.post_purchase_actions()['recipient'] == CREDS['approved_test_email']
    bindings = {b['name']: b['text'] for b in secret_bindings(install, 'post-purchase')}
    assert json.loads(bindings['DELIVERY_CONFIG'])['test_recipient_allowlist'] == {
        'addresses': ['specific@another.test'], 'domains': ['effectivepolicy.com.au', 'mitchellcurrie.com']}
    assert bindings['APPROVED_TEST_RECIPIENT'] == CREDS['approved_test_email']
    reloaded = Installation(install.directory, install.credentials, api)
    inventory = reloaded.state_path.read_bytes()
    assert reloaded.reconfigure_test_recipients(policy)['changed'] is False
    assert reloaded.state_path.read_bytes() == inventory
    with pytest.raises(ValueError, match='input|configuration'):
        install.check_post_purchase_release(install.state['releases'][-1])


def test_recipient_policy_cannot_change_while_sending(deployed):
    install, _, _ = deployed
    install.state['post_purchase_modes'] = ON
    before = install.state_path.read_bytes()
    with pytest.raises(ValueError, match='disable'):
        install.reconfigure_test_recipients({'addresses': [], 'domains': ['example.test']})
    assert install.state_path.read_bytes() == before


@pytest.mark.parametrize('policy', [None, {}, [], {'addresses': [], 'domains': []},
    {'addresses': [], 'domains': ['*.example.test']},
    {'addresses': [], 'domains': ['example.test', 'EXAMPLE.TEST']},
    {'addresses': [], 'domains': ['еxample.test']},
    {'addresses': ['a..b@example.test'], 'domains': []},
    {'addresses': [], 'domains': ['sub..example.test']},
    {'addresses': [], 'domains': ['x.test'] * 33},
    {'addresses': [], 'domains': ['example.test'], 'extra': True}])
def test_bad_recipient_policy_cannot_change_inventory(deployed, policy):
    install, _, _ = deployed
    before = install.state_path.read_bytes()
    with pytest.raises(ValueError, match='allowlist'):
        install.reconfigure_test_recipients(policy)
    assert install.state_path.read_bytes() == before


def test_schema2_policy_is_normalized_and_sender_handoff_unchanged(tmp_path):
    options = handoff(tmp_path)
    snapshot = {'schema': 2, 'sender': CREDS['test_sender'], 'recipient': CREDS['approved_test_email'],
                'test_recipient_allowlist': {'addresses': ['Specific@another.test'], 'domains': ['Example.TEST']}}
    atomic_json(Path(options['post_purchase_config']), snapshot)
    install = Installation.plan(tmp_path / 'installation', options)
    install.credentials = deepcopy(CREDS)
    secrets = install.initial_post_purchase_secrets()
    assert secrets['post_purchase_approved_test_email'] == CREDS['approved_test_email']
    assert install.post_purchase_actions()['test_recipient_allowlist']['addresses'] == ['specific@another.test']


def test_policy_update_deploys_off_with_existing_resources(deployed):
    install, api, db = deployed
    initialise(install, db)
    install.reconfigure_test_recipients({'addresses': [], 'domains': ['example.test']})
    capture_build(install, 'updated')
    install.deploy()
    assert install.state['post_purchase_modes'] == OFF
    assert install.post_purchase_fence()['activation_watermark'] == 7
    assert install.state['deployment']['complete'] is True


def test_policy_binding_size_is_checked_before_inventory_changes(deployed):
    from topology import test_recipient_delivery_config as encode_policy
    from topology import validate_test_recipient_allowlist
    def address(length, index):
        local = ('u' + str(index)).ljust(64, 'a')
        return local + '@' + 'd' * (length - 70) + '.test'
    policy = {'addresses': [address(128, i) for i in range(30)], 'domains': []}
    last_length = 4096 - len(encode_policy(policy).encode()) - 3
    policy['addresses'].append(address(last_length, 30))
    assert len(encode_policy(validate_test_recipient_allowlist(policy)).encode()) == 4096
    install, _, _ = deployed
    before = install.state_path.read_bytes()
    policy['addresses'][-1] = address(last_length + 1, 30)
    with pytest.raises(ValueError, match='4096-byte'):
        install.reconfigure_test_recipients(policy)
    assert install.state_path.read_bytes() == before


def test_refresh_native_clients_keeps_commerce_policy_keys_and_checkpoint(deployed, tmp_path):
    from merchant_inputs import javascript_json, tree_hash
    install, api, db = deployed
    initialise(install, db)
    import shutil
    source = tmp_path / 'runtime-suffix'
    shutil.copytree(install.config['merchant_inputs'], source)
    script = source / 'storefront/storefront-config.js'
    suffix = b" window.CARTRAY_STOREFRONT.apiBaseUrl='/commerce-api'; window.CARTRAY_CONNECTED_CATALOGUE=true;\r\n"
    script.write_bytes(script.read_bytes() + suffix)
    install.update_merchant_inputs(source)
    original = Path(install.config['merchant_inputs'])
    commerce_hash = tree_hash(original / 'commerce')
    old_inputs_hash = tree_hash(original)
    old_vault = install.vault_path.read_bytes()
    old_policy = install.config['post_purchase_config_sha256']
    branding = {'name': 'Example merchant', 'home_path': '/shop/', 'contact_path': '/contact-us/', 'accent': '#001b35'}
    install.refresh_merchant_clients(branding)
    updated = Path(install.config['merchant_inputs'])
    assert (updated / 'storefront/storefront-config.js').read_bytes().endswith(suffix)
    assert tree_hash(updated / 'commerce') == commerce_hash
    assert tree_hash(original) == old_inputs_hash
    assert install.vault_path.read_bytes() == old_vault
    assert install.config['post_purchase_config_sha256'] == old_policy
    assert javascript_json(updated / 'storefront/storefront-config.js', 'window.CARTRAY_STOREFRONT = ')[
        'orderAccessPresentation'] == branding
    assert install.post_purchase_fence()['activation_watermark'] == 7
    assert install.state['post_purchase_modes'] == OFF
    nodes, _ = graph(install)
    # The fixture has no fulfilment; branding cannot install or authorize resource access.
    assert 'CARTRAY_ORDER_ACCESS_PRESENTATION' not in nodes['source']['vars']


@pytest.mark.parametrize('deployed', ['resources'], indirect=True)
def test_branding_refresh_uses_recorded_old_bindings_then_deploys_new_presentation(deployed):
    from types import SimpleNamespace

    from cartray.order_access_presentation import render_order_access_page
    install, api, db = deployed
    initialise(install, db)
    branding = {'name': 'Example merchant', 'home_path': '/catalogue/', 'contact_path': '/contact-us/',
                'accent': '#001b35'}
    before_resources = deepcopy(install.state['resources'])
    before_vault = install.vault_path.read_bytes()
    install.refresh_merchant_clients(branding)
    nodes, _ = graph(install)
    encoded = nodes['source']['vars']['CARTRAY_ORDER_ACCESS_PRESENTATION']
    assert json.loads(encoded) == branding
    assert 'Example merchant' in render_order_access_page(
        SimpleNamespace(CARTRAY_ORDER_ACCESS_PRESENTATION=encoded))[0]
    # Existing OFF source has no branding binding; verification must use its retained package.
    install.check_current_post_purchase_off()
    capture_build(install, 'branded')
    install.deploy()
    assert install.state['post_purchase_modes'] == OFF
    assert install.state['resources'] == before_resources
    assert install.vault_path.read_bytes() == before_vault
    assert install.post_purchase_fence()['activation_watermark'] == 7
    old_release = len(install.state['releases']) - 1
    old_binding = nodes['source']['vars']['CARTRAY_ORDER_ACCESS_PRESENTATION']
    with_logo = {**branding, 'logo_path': '/assets/approved-logo.svg', 'heading_font': 'editorial'}
    install.refresh_merchant_clients(with_logo)
    install.check_current_post_purchase_off()
    capture_build(install, 'logo')
    install.deploy()
    logo_nodes, _ = graph(install)
    assert json.loads(logo_nodes['source']['vars']['CARTRAY_ORDER_ACCESS_PRESENTATION']) == with_logo
    assert install.rollback(old_release)['database_rewind'] is False
    restored_nodes, _ = graph(install)
    assert restored_nodes['source']['vars']['CARTRAY_ORDER_ACCESS_PRESENTATION'] == old_binding
    assert install.vault_path.read_bytes() == before_vault
    assert install.state['resources'] == before_resources
    assert install.post_purchase_fence()['activation_watermark'] == 7


@pytest.mark.parametrize(('phase', 'complete'), [
    ('deploying', True), ('rolling-back', True), ('migrated', False),
])
def test_client_refresh_rejects_deployment_transition_without_mutation(deployed, phase, complete):
    install, _, db = deployed
    initialise(install, db)
    install.state['phase'] = phase
    install.state['deployment']['complete'] = complete
    install.save()
    before = install.state_path.read_bytes()
    inputs = install.config['merchant_inputs_sha256']
    with pytest.raises(ValueError, match='finish deployment'):
        install.refresh_merchant_clients()
    assert install.state_path.read_bytes() == before
    assert install.config['merchant_inputs_sha256'] == inputs
