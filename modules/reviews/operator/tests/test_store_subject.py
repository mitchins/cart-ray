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
