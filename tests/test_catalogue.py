from cartray.errors import CheckoutValidationError


def test_public_and_private_manifests_have_separate_concerns(fixture_catalogue):
    public = fixture_catalogue.public_manifest()
    private = fixture_catalogue.private_manifest()
    assert public["products"]["TEST-TEMPLATE"]["amount_minor"] == 2500
    assert "stripe_price_id" not in public["products"]["TEST-TEMPLATE"]
    assert private["products"]["TEST-TEMPLATE"]["stripe_price_id"] == "price_fixture_template"
    assert "fulfilment_resources" not in public["products"]["TEST-BUNDLE"]


def test_catalogue_rejects_unknown_product(fixture_catalogue):
    try:
        fixture_catalogue.product("UNKNOWN")
    except CheckoutValidationError as error:
        assert "unknown" in str(error)
    else:
        raise AssertionError("expected product validation failure")
