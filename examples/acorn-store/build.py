#!/usr/bin/env python3
"""Build the isolated, disabled-by-default Acorn reference storefront."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from cartray.catalogue import CsvCatalogueSourceAdapter, FixturePriceResolver
from cartray.catalogue_bundle import build_runtime_presented_catalogue, compile_catalogue_bundle
from cartray.preflight import PreflightLock
from cartray.presentation import CsvPresentationSourceAdapter, validate_presentation_assets

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PREFLIGHT_LOCK = HERE / ".private/stripe-test-preflight.lock.json"


def configuration(path: Path) -> dict:
    config = json.loads(path.read_text())
    if config.get("environment") != "test" or not re.fullmatch(r"[a-z0-9-]{1,64}", config.get("store_key", "")):
        raise ValueError("reference store must use a scoped test environment")
    if config.get("review_invitations", "disabled") not in ("disabled", "automatic"):
        raise ValueError("unsupported review invitation mode")
    products = config.get("products")
    if not isinstance(products, list) or len(products) != 3:
        raise ValueError("reference store requires exactly three product mappings")
    keys, paths, subjects = set(), set(), set()
    for item in products:
        key, path, subject = item.get("product_key"), item.get("path"), item.get("review_subject")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9-]{1,64}", key) or key in keys:
            raise ValueError("invalid or duplicate product key")
        if not isinstance(path, str) or not re.fullmatch(r"/products/[a-z0-9-]+/", path) or path in paths:
            raise ValueError("invalid or duplicate product path")
        if subject is not None and (
            not isinstance(subject, str) or not re.fullmatch(r"[a-z0-9-]{1,128}", subject) or subject in subjects
        ):
            raise ValueError("invalid or duplicate review subject")
        keys.add(key)
        paths.add(path)
        if subject is not None:
            subjects.add(subject)
    if len(subjects) != 2:
        raise ValueError("exactly two products must be reviewable")
    if not isinstance(config.get("store_name"), str) or not 1 <= len(config["store_name"]) <= 80:
        raise ValueError("invalid store name")
    return config


async def preview_manifest(config: dict, lock_path: Path | None = None) -> dict:
    source = CsvCatalogueSourceAdapter(HERE / "catalogue.csv")
    presentation = CsvPresentationSourceAdapter(HERE / "catalogue-presentation.csv")
    fixture_prices = FixturePriceResolver.from_json(HERE / "price-resolutions.json")
    prices = PreflightLock.from_json(lock_path) if lock_path is not None else fixture_prices
    if lock_path is not None:
        lookup_keys = {item.stripe_lookup_key for item in await source.load()}
        if lookup_keys != set(prices.prices):
            raise ValueError("test preflight lock does not match catalogue lookup keys")
        for key in lookup_keys:
            expected, actual = await fixture_prices.resolve(key), await prices.resolve(key)
            if (actual.amount_minor, actual.currency) != (expected.amount_minor, expected.currency):
                raise ValueError("test Stripe price differs from the approved reference amount")
    expansions = {
        key: tuple(value) for key, value in json.loads((HERE / "fulfilment-expansions.json").read_text()).items()
    }
    validate_presentation_assets(await presentation.load(), HERE / "assets/products")
    bundle = await compile_catalogue_bundle(
        source,
        prices,
        expansions,
        presentation,
        expected_catalogue_version=prices.catalogue_version if lock_path is not None else None,
    )
    result = (await build_runtime_presented_catalogue(bundle, prices)).public_manifest()
    mapped = {item["product_key"]: item for item in config["products"]}
    if {item["product_key"] for item in result["products"]} != set(mapped):
        raise ValueError("product mapping does not match catalogue")
    for item in result["products"]:
        if bool(mapped[item["product_key"]]["review_subject"]) != (item["amount_minor"] > 0):
            raise ValueError("reviewable products must be paid and free product non-reviewable")
    return result


def test_api(value: str | None) -> str | None:
    if value is None:
        return None
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.netloc
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("test checkout API must be an HTTPS origin")
    return value.rstrip("/")


def build(
    output: Path,
    config: dict,
    *,
    checkout_api: str | None = None,
    reviews: bool = False,
    preflight_lock: Path = PREFLIGHT_LOCK,
) -> None:
    if output.exists():
        raise ValueError("output directory already exists; choose a new directory")
    api = test_api(checkout_api)
    manifest = asyncio.run(preview_manifest(config, preflight_lock if api else None))
    output.mkdir(parents=True)
    for filename in ("app.js", "styles.css"):
        shutil.copy2(ROOT / "storefront" / filename, output / filename)
    shutil.copy2(HERE / "acorn.css", output / "acorn.css")
    shutil.copy2(HERE / "reviews.mjs", output / "reviews.mjs")
    shutil.copytree(HERE / "assets", output / "assets")
    shutil.copytree(ROOT / "modules/reviews/ui", output / "review")
    paths = {item["product_key"]: item["path"] for item in config["products"]}
    subjects = {item["product_key"]: item["review_subject"] for item in config["products"] if item["review_subject"]}
    base = {
        "storeName": config["store_name"],
        "checkoutEnabled": bool(api),
        "apiBaseUrl": api,
        "previewCatalogue": None if api else manifest,
        "productPaths": paths,
        "reviewSubjects": subjects,
        "reviewsEnabled": reviews,
        "reviewInvitationsEnabled": bool(api and config.get("review_invitations") == "automatic"),
    }
    # JSON is external JS, not HTML: no user-provided text is interpolated into markup.
    (output / "storefront-config.js").write_text(
        "window.CARTRAY_STOREFRONT = "
        + json.dumps(base, ensure_ascii=True)
        + ";\n"
        + "window.CARTRAY_STOREFRONT.productKey = document.body.dataset.productKey || null;\n"
        "document.title = window.CARTRAY_STOREFRONT.storeName;\n"
        "document.querySelectorAll('[data-store-name]').forEach(e => { "
        "e.textContent = window.CARTRAY_STOREFRONT.storeName; });\n"
    )
    template = (HERE / "index.html").read_text()
    (output / "index.html").write_text(template.replace("{{PRODUCT_KEY}}", ""))
    for item in config["products"]:
        directory = output / item["path"].strip("/")
        directory.mkdir(parents=True)
        (directory / "index.html").write_text(template.replace("{{PRODUCT_KEY}}", item["product_key"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for static output")
    parser.add_argument("--config", type=Path, default=HERE / "store.json")
    parser.add_argument("--test-checkout-api", help="explicit HTTPS CartRay TEST Worker origin")
    parser.add_argument("--reviews", action="store_true", help="enable same-origin test Reviews reads")
    args = parser.parse_args()
    build(
        args.output,
        configuration(args.config),
        checkout_api=args.test_checkout_api,
        reviews=args.reviews,
    )


if __name__ == "__main__":
    main()
