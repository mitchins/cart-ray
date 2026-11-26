"""Optional store reviews use ordinary installation identity and provisioning."""

import json

import pytest
from test_merchant_inputs import merchant, plan_merchant
from test_operations import migrated, operations, planned
from topology import graph

SUBJECT = {"subject_id": "merchant-store", "path": "/reviews/", "name": "Merchant shop"}


def test_optional_store_subject_defaults_unchecked_and_preserves_existing_merchants(tmp_path):
    original = planned(tmp_path).config
    assert operations.validate_config(original)["review_invitation_default_checked"] is True
    # The original checkbox preference is absent, not an explicit previous decision.
    fresh = {k: v for k, v in original.items() if k != "review_invitation_default_checked"}
    assert operations.validate_config(fresh | {"store_review_subject": SUBJECT})[
        "review_invitation_default_checked"] is False
    assert operations.validate_config(fresh | {"store_review_subject": SUBJECT,
                                               "review_invitation_default_checked": False})[
        "review_invitation_default_checked"] is False
    assert operations.validate_config(fresh | {"store_review_subject": SUBJECT,
                                               "review_invitation_default_checked": True})[
        "review_invitation_default_checked"] is True


@pytest.mark.parametrize("subject", [
    None, {}, SUBJECT | {"unknown": "field"}, SUBJECT | {"path": "https://other.test/reviews/"},
    SUBJECT | {"path": "/reviews/../other/"}, SUBJECT | {"path": "/reviews/%2e%2e/"},
    SUBJECT | {"subject_id": "acorn-growing-kit"}, SUBJECT | {"path": "/products/woodland-planter/"},
    SUBJECT | {"name": ""}, SUBJECT | {"name": "bad\nname"},
])
def test_store_subject_rejects_invalid_or_colliding_configuration(tmp_path, subject):
    with pytest.raises(ValueError):
        operations.validate_config(planned(tmp_path).config | {"store_review_subject": subject})


def test_store_subject_seed_is_idempotent_and_uses_existing_physical_database(tmp_path):
    install, cloud = migrated(tmp_path)
    install.config["store_review_subject"] = dict(SUBJECT)
    install.migrate()
    original_resources = json.loads(json.dumps(install.state["resources"]))
    install.seed_subjects()
    first = cloud.query(install.db("reviews"), "SELECT * FROM review_subjects ORDER BY subject_id")
    install.seed_subjects()
    assert cloud.query(install.db("reviews"), "SELECT * FROM review_subjects ORDER BY subject_id") == first
    assert len(first) == 3
    assert next(item for item in first if item["subject_id"] == SUBJECT["subject_id"])["kind"] == "store"
    assert cloud.query(install.db("reviews"), "SELECT public_name FROM review_subject_display_names "
                       "WHERE subject_id=?", [SUBJECT["subject_id"]]) == [{"public_name": SUBJECT["name"]}]
    assert install.state["resources"] == original_resources
    install.state.update(workers_subdomain="sandbox", pages_subdomain="shop.example.test")
    nodes, _ = graph(install)
    assert set(nodes) == {"source", "reviews"}
    assert json.loads(nodes["source"]["vars"]["CARTRAY_STORE_REVIEW_SUBJECT"]) == SUBJECT


@pytest.mark.parametrize("change", [
    "UPDATE review_subjects SET kind='product' WHERE subject_id='merchant-store'",
    "UPDATE review_subjects SET canonical_path='/other/' WHERE subject_id='merchant-store'",
    "UPDATE review_subject_display_names SET public_name='Other merchant' WHERE subject_id='merchant-store'",
    "INSERT INTO review_subjects VALUES ('acorn-store','test','unexpected','product','/REVIEWS/')",
])
def test_store_subject_seed_rejects_installed_identity_conflicts(tmp_path, change):
    install, cloud = migrated(tmp_path)
    install.config["store_review_subject"] = dict(SUBJECT)
    install.migrate()
    install.seed_subjects()
    cloud.query(install.db("reviews"), change)
    writes = cloud.writes
    with pytest.raises(ValueError, match="installed store review"):
        install.seed_subjects()
    assert cloud.writes == writes


def test_store_subject_is_release_bound_and_cannot_retarget_retained_identity(tmp_path):
    source = merchant(tmp_path / "merchant")
    install = plan_merchant(tmp_path, source, mapped=True, store_review_subject=SUBJECT)
    first = install.merchant_config_hash()
    install.config["store_review_subject"] = SUBJECT | {"name": "Other"}
    assert install.merchant_config_hash() != first
    for change in ({"name": "Other"}, {"subject_id": "other"}, {"path": "/feedback/"}):
        candidate = {**install.config, "store_review_subject": SUBJECT | change}
        with pytest.raises(ValueError, match="cannot change"):
            install.check_catalogue_subjects(candidate)
    assert install.catalogue_config()["store_review_subject"] == SUBJECT
    previous = install.catalogue_config()
    install.use_catalogue_config({k: v for k, v in previous.items() if k != "store_review_subject"})
    assert "store_review_subject" not in install.config
    install.use_catalogue_config(previous)
    assert install.config["store_review_subject"] == SUBJECT


def test_nonconfigured_merchant_has_no_store_binding(tmp_path):
    install, _ = migrated(tmp_path)
    install.state["workers_subdomain"] = "sandbox"
    install.state.update(workers_subdomain="sandbox", pages_subdomain="shop.example.test")
    nodes, _ = graph(install)
    assert "CARTRAY_STORE_REVIEW_SUBJECT" not in nodes["source"]["vars"]


def test_store_subject_cannot_reuse_a_removed_product_subject(tmp_path):
    source = merchant(tmp_path / "merchant")
    install = plan_merchant(tmp_path, source, mapped=True)
    removed = {**install.config, "subject_map": {}, "subject_details": [],
               "store_review_subject": SUBJECT | {"subject_id": "product-one"}}
    with pytest.raises(ValueError, match="retained product"):
        install.check_catalogue_subjects(removed)
    removed["store_review_subject"] = SUBJECT | {"path": "/products/one/"}
    with pytest.raises(ValueError, match="retained product"):
        install.check_catalogue_subjects(removed)


def test_store_subject_route_cannot_hide_a_non_reviewable_product(tmp_path):
    source = merchant(tmp_path / "merchant")
    install = plan_merchant(tmp_path, source, mapped=False,
                            store_review_subject=SUBJECT | {"path": "/products/one/"})
    with pytest.raises(ValueError, match="collides with a merchant product"):
        install.verify_merchant_inputs()


def record_checkout_choice(connection, *, version, suffix):
    """Use the actual order store to retain an immutable checkout choice/grant."""
    import asyncio

    from cartray.models import CheckoutOrder, OrderItem
    from cartray.store import SqliteOrderStore

    order = CheckoutOrder(
        order_id='rollback-order-' + suffix, checkout_request_id='rollback-request-' + suffix,
        request_fingerprint='rollback-fingerprint-' + suffix, manifest_version='sha256:' + 'a' * 64,
        items=(OrderItem(product_key='PAID-ITEM', quantity=1, stripe_price_id='price_test',
                         unit_amount_minor=100, fulfilment_resources=()),),
        items_digest='sha256:' + 'b' * 64, currency='aud', subtotal_minor=100,
        review_invitation_opt_in=True, review_invitation_policy_enabled=True,
        review_invitation_default_selected=False, review_invitation_wording_version=version,
    )
    asyncio.run(SqliteOrderStore(connection).start_or_load(order, nonce='checkout', now=100))
    assert connection.execute(
        'SELECT wording_version FROM review_invitation_checkout_grants WHERE order_id=?',
        (order.order_id,)).fetchone()[0] == version


def test_v4_checkout_blocks_pre_store_rollback_before_any_mutation(tmp_path):
    install, cloud = migrated(tmp_path)
    install.migrate()
    connection = cloud.databases[install.db('commerce')][1]
    old_release = {'commit': 'a' * 40}
    install.check_store_review_rollback(old_release)
    record_checkout_choice(connection, version='review-invitation-v3', suffix='legacy')
    install.check_store_review_rollback(old_release)
    record_checkout_choice(connection, version='review-invitation-v4', suffix='shop')
    install.state['releases'] = [old_release]
    before_state = json.loads(json.dumps(install.state))
    before_data = list(connection.iterdump())
    before_writes = cloud.writes
    with pytest.raises(ValueError, match='configured, deployed or recorded'):
        install.rollback(0)
    assert install.state == before_state
    assert list(connection.iterdump()) == before_data
    assert cloud.writes == before_writes


@pytest.mark.parametrize('release', [
    {'store_review_contract': 1},
    {'store_review_contract': True, 'store_review_subject': SUBJECT},
    {'store_review_contract': 1, 'store_review_subject': SUBJECT | {'subject_id': 'other-store'}},
    {'store_review_contract': 1, 'catalogue_config': {'store_review_subject': SUBJECT | {'path': '/other/'}}},
    {'store_review_contract': 1, 'store_review_subject': SUBJECT, 'catalogue_config': {}},
])
def test_v4_rollback_requires_compatible_code_and_same_retained_store(tmp_path, release):
    install, cloud = migrated(tmp_path)
    install.config['store_review_subject'] = dict(SUBJECT)
    install.migrate()
    connection = cloud.databases[install.db('commerce')][1]
    record_checkout_choice(connection, version='review-invitation-v4', suffix='shop')
    with pytest.raises(ValueError, match='same store subject'):
        install.check_store_review_rollback(release)


def test_compatible_v4_application_rollback_preserves_checkout_and_store_records(tmp_path):
    from test_lifecycle import build_at, lifecycle

    install, provider, database, _, _ = lifecycle(tmp_path)
    install.config['store_review_subject'] = dict(SUBJECT)
    install.state['config']['store_review_subject'] = dict(SUBJECT)
    first = build_at(install, 'a' * 40)
    assert first['store_review_contract'] == 1 and first['store_review_subject'] == SUBJECT
    install.deploy()
    install.verify()
    build_at(install, 'b' * 40)
    install.deploy()
    install.verify()
    connection = database.databases[install.db('commerce')][1]
    record_checkout_choice(connection, version='review-invitation-v4', suffix='shop')
    before = list(connection.iterdump())
    original_keys = install.vault()
    result = install.rollback(-2)
    assert result['rollback_commit'] == 'a' * 40 and result['database_rewind'] is False
    assert install.config['store_review_subject'] == SUBJECT
    assert install.vault() == original_keys
    assert list(connection.iterdump()) == before
    assert set(provider.active) == {'acorn-operator-test-source', 'acorn-operator-test-reviews'}


@pytest.mark.parametrize('configured', ['current', 'deployment', 'deployed-release'])
def test_configured_store_prevents_checkout_race_with_incompatible_rollback(tmp_path, configured):
    install, cloud = migrated(tmp_path)
    install.migrate()
    connection = cloud.databases[install.db('commerce')][1]
    old_release = {'commit': 'a' * 40}
    install.state['releases'] = [old_release]
    if configured == 'current':
        install.config['store_review_subject'] = dict(SUBJECT)
    elif configured == 'deployment':
        install.state['deployment'] = {'catalogue_config': {'store_review_subject': SUBJECT}}
    else:
        install.state['deployment'] = {'versions': {'source': 'store-capable-version'}}
        install.state['releases'].append({'versions': {'source': 'store-capable-version'},
                                          'store_review_subject': SUBJECT})
    query = cloud.query
    raced = []

    def checkout_after_query(database, sql, params=()):
        result = query(database, sql, params)
        if 'SELECT EXISTS(SELECT 1 FROM review_invitation_checkout_choices' in sql:
            assert result == [{'present': 0}]
            # A real checkout commits just after the read. Paused email does not stop checkout.
            record_checkout_choice(connection, version='review-invitation-v4', suffix='racing')
            raced.append(True)
        return result

    cloud.query = checkout_after_query
    def prohibit_publish(*_):
        pytest.fail('incompatible backend must be rejected before deployment')
    cloud.publish = prohibit_publish
    original_state = json.loads(json.dumps(install.state))
    with pytest.raises(ValueError, match='configured, deployed or recorded'):
        install.rollback(0)
    assert raced == [True]
    assert install.state == original_state
    assert connection.execute('SELECT COUNT(*) FROM review_invitation_checkout_choices').fetchone()[0] == 1


def test_configured_store_rejects_old_release_even_before_first_checkout(tmp_path):
    install, cloud = migrated(tmp_path)
    install.config['store_review_subject'] = dict(SUBJECT)
    install.migrate()
    with pytest.raises(ValueError, match='configured, deployed or recorded'):
        install.check_store_review_rollback({'commit': 'a' * 40})
    install.check_store_review_rollback({'store_review_contract': 1, 'store_review_subject': SUBJECT})
    assert cloud.query(install.db('commerce'),
                       'SELECT COUNT(*) n FROM review_invitation_checkout_choices') == [{'n': 0}]


def test_first_store_reconfiguration_rejects_inherited_legacy_default_without_mutation(tmp_path):
    from test_merchant_inputs import catalogue_revision

    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'), mapped=True)
    original_hash = install.merchant_config_hash()
    original_state = json.loads(json.dumps(install.state))
    values = catalogue_revision(tmp_path / 'replacement') | {'store_review_subject': SUBJECT}
    assert install.config['review_invitation_default_checked'] is True
    with pytest.raises(ValueError, match='requires an explicit'):
        install.reconfigure_catalogue(values)
    assert install.state == original_state
    assert install.merchant_config_hash() == original_hash
    assert 'store_review_subject' not in install.config


@pytest.mark.parametrize('checked', [False, True])
def test_explicit_store_reconfiguration_retains_choice_on_retry_resume_and_snapshot_restore(tmp_path, checked):
    from test_merchant_inputs import Installation, catalogue_revision

    install = plan_merchant(tmp_path, merchant(tmp_path / 'original'), mapped=True)
    previous = install.catalogue_config()
    values = catalogue_revision(tmp_path / 'replacement') | {
        'store_review_subject': SUBJECT, 'review_invitation_default_checked': checked}
    install.reconfigure_catalogue(values)
    assert install.config['review_invitation_default_checked'] is checked
    current = install.catalogue_config()
    assert current['review_invitation_default_checked'] is checked
    install.reconfigure_catalogue(values)
    resumed = Installation(install.directory)
    assert resumed.config['review_invitation_default_checked'] is checked
    assert resumed.catalogue_config() == current
    # The existing snapshot mechanism restores the matching display preference.
    resumed.use_catalogue_config(previous)
    assert resumed.config['review_invitation_default_checked'] is True
    resumed.use_catalogue_config(current)
    assert resumed.config['review_invitation_default_checked'] is checked
