"""Verify hosted receipt assertions reject unsafe or incomplete observations."""

import copy
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/qualify_hosted_t1.py"
spec = importlib.util.spec_from_file_location("qualify_hosted_t1", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def evidence():
    return dict(
        old_id=3,
        old_source=[{"delivered_at": None}],
        source=[
            {
                "outbox_id": 8,
                "delivered_at": None,
                "order_id": "cr_example",
                "manifest_version": module.CATALOGUE,
                "currency": "aud",
                "subtotal_minor": 4200,
                "settlement_state": "confirmed",
                "stripe_payment_status": "paid",
                "stripe_amount_total_minor": 4200,
            }
        ],
        items=[
            {"product_key": "ACORN-KIT", "quantity": 1, "unit_amount_minor": 2400, "stripe_price_id": "price_a"},
            {"product_key": "ACORN-PLANTER", "quantity": 1, "unit_amount_minor": 1800, "stripe_price_id": "price_b"},
        ],
        activation=[{"outbox_watermark": 3, "fence_mode": "outbox_snapshot"}],
        checkpoint=[
            {
                "activation_watermark": 3,
                "last_outbox_id": 8,
                "subscriber_id": "issuer",
                "installation_id": "install",
                "source_database_id": "db",
                "stripe_account_id": module.STRIPE_ACCOUNT,
                "catalogue_version": module.CATALOGUE,
                "mapping_sha256": "mapping",
            }
        ],
        expected_checkpoint={
            "subscriber_id": "issuer",
            "installation_id": "install",
            "source_database_id": "db",
            "stripe_account_id": module.STRIPE_ACCOUNT,
            "catalogue_version": module.CATALOGUE,
            "mapping_sha256": "mapping",
        },
        old_intake=[],
        old_intent=[],
        intake=[{"outbox_id": 8, "acquisition_id": "cr_example", "disposition": "accepted"}],
        intent=[{"outbox_id": 8, "acquisition_id": "cr_example", "state": "held_eligibility_recipient_consent"}],
        session={
            "id": "cs_test_example",
            "livemode": False,
            "status": "complete",
            "payment_status": "paid",
            "currency": "aud",
            "amount_total": 4200,
        },
        lines={
            "has_more": False,
            "data": [
                {"price": {"id": "price_a"}, "quantity": 1, "amount_total": 2400},
                {"price": {"id": "price_b"}, "quantity": 1, "amount_total": 1800},
            ],
        },
    )


def test_success_receipt_excludes_private_stripe_fields():
    data = evidence()
    data["session"]["customer_details"] = {"email": "do-not-expose@example.invalid"}
    result = module.validate(**data)
    assert result["reviews_receipt_count"] == 1
    assert "do-not-expose" not in str(result)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x["activation"][0].update(outbox_watermark=2),
        lambda x: x["old_intent"].append({"outbox_id": 3}),
        lambda x: x["source"][0].update(delivered_at=123),
        lambda x: x["checkpoint"][0].update(last_outbox_id=7),
        lambda x: x["intake"].append(copy.deepcopy(x["intake"][0])),
        lambda x: x["intent"][0].update(state="sent"),
        lambda x: x["items"][0].update(quantity=2),
        lambda x: x["session"].update(livemode=True),
        lambda x: x["lines"].update(has_more=True),
    ],
)
def test_unsafe_observation_fails_closed(mutation):
    data = evidence()
    mutation(data)
    with pytest.raises(ValueError):
        module.validate(**data)


@pytest.mark.parametrize(
    "field",
    [
        "subscriber_id",
        "installation_id",
        "source_database_id",
        "stripe_account_id",
        "catalogue_version",
        "mapping_sha256",
    ],
)
def test_checkpoint_identity_mismatch_rejected(field):
    data = evidence()
    data["checkpoint"][0][field] = "wrong"
    with pytest.raises(ValueError):
        module.validate(**data)


def test_wrong_fence_rejected():
    data = evidence()
    data["activation"][0]["fence_mode"] = "other"
    with pytest.raises(ValueError):
        module.validate(**data)


def test_deployed_database_mismatch_rejected():
    with pytest.raises(ValueError):
        module.check_bindings(
            [{"name": "REVIEWS_DB", "type": "d1", "database_id": "wrong"}],
            [("REVIEWS_DB", "d1", "database_id", "correct")],
        )


@pytest.mark.parametrize("field", ["environment", "entrypoint"])
def test_nondefault_service_target_rejected(field):
    binding = {"name": "SOURCE_API", "type": "service", "service": "source", field: "other"}
    with pytest.raises(ValueError):
        module.check_bindings([binding], [("SOURCE_API", "service", "service", "source")])


def test_default_production_service_environment_accepted():
    binding = {"name": "SOURCE_API", "type": "service", "service": "source", "environment": "production"}
    module.check_bindings([binding], [("SOURCE_API", "service", "service", "source")])
