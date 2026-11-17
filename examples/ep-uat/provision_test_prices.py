#!/usr/bin/env python3
"""Create narrowly scoped Stripe test Prices for selected source-backed UAT offers.

The result is a private test-lane fixture, not a production catalogue or entitlement.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import json
import os
import re
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit


def call_stripe(key, method, path, data=None, idempotency=None):
    header = {"Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode()}
    if idempotency:
        header["Idempotency-Key"] = idempotency
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request("https://api.stripe.com/v1/" + path, data=payload, headers=header, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def amount_minor(raw):
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("source price is not a valid decimal") from error
    if not value.is_finite() or value <= 0 or value.as_tuple().exponent < -2:
        raise ValueError("source price must be positive with at most two decimal places")
    minor = int(value * 100)
    if minor > 100_000_000:
        raise ValueError("source price exceeds UAT limit")
    return minor


def dummy_followups(blobs, catalogue, offer_ids):
    """Retain approved dummy follow-up references without sending or treating them as entitlements."""
    workflows = {w["key"]: w for w in json.loads(blobs["workflows.json"])}
    offers = {p["source_id"]: p for p in catalogue["products"]}
    results = []
    for offer_id in offer_ids:
        offer = offers.get(offer_id)
        if not offer:
            raise ValueError(f"missing follow-up source offer {offer_id}")
        for key in offer["source_workflow_keys"]:
            workflow = workflows.get(key)
            if not workflow or offer["key"] not in workflow["matchingOfferKeys"]:
                raise ValueError("follow-up workflow identity does not match source offer")
            for action in workflow["actions"]:
                url = action.get("url", "")
                parsed = urlsplit(url)
                if (
                    action.get("kind") != "uat_dummy_transfer"
                    or action.get("sendEnabled") is not False
                    or parsed.scheme != "https"
                    or parsed.hostname != "www.dropbox.com"
                    or not re.fullmatch(r"/t/[A-Za-z0-9]+", parsed.path)
                    or parsed.query
                    or parsed.fragment
                    or parsed.username
                    or parsed.password
                ):
                    raise ValueError("follow-up is not the approved disabled dummy transfer")
                results.append(
                    {"source_key": offer["key"], "workflow_key": key, "dummy_url": url, "send_enabled": False}
                )
    return results


def provision(catalogue, offer_ids, key, expected_account, transport=call_stripe):
    if not key.startswith(("sk_test_", "rk_test_")) or not re.fullmatch(r"acct_[A-Za-z0-9]+", expected_account):
        raise ValueError("test-only Stripe key and explicit account identity are required")
    if catalogue.get("source_currency") != "AUD":
        raise ValueError("this EP test lane requires source-verified AUD pricing")
    products = {p["source_id"]: p for p in catalogue["products"]}
    if len(set(offer_ids)) != len(offer_ids) or not 1 <= len(offer_ids) <= 3:
        raise ValueError("select one to three distinct offers")
    source_hash = catalogue["source_archive_sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise ValueError("invalid source archive hash")
    candidates_to_process = []
    for offer_id in offer_ids:
        offer = products.get(offer_id)
        if not offer or offer["source_type"] != "simple" or offer["source_delivery_kind"] != "uat_dummy_transfer":
            raise ValueError(f"offer {offer_id} is not eligible for dummy fulfilment test")
        if not offer.get("images"):
            raise ValueError(f"offer {offer_id} has no approved presentation image")
        total = amount_minor(offer["source_price"])
        candidates_to_process.append((offer_id, offer, total))
    account = transport(key, "GET", "account")
    if account.get("id") != expected_account:
        raise ValueError("Stripe test account mismatch")
    selected = []
    for offer_id, offer, total in candidates_to_process:
        lookup = f"ep_uat_woo_{offer_id}_{source_hash[:12]}"
        current = transport(
            key, "GET", "prices?" + urllib.parse.urlencode({"lookup_keys[]": lookup, "active": "true", "limit": 10})
        )
        candidates = current.get("data", [])
        if len(candidates) > 1:
            raise ValueError(f"ambiguous Stripe test price for {offer_id}")
        if candidates:
            price = candidates[0]
        else:
            fields = {
                "currency": "aud",
                "unit_amount": str(total),
                "lookup_key": lookup,
                "product_data[name]": f"EP UAT — {offer['title']}",
                "metadata[uat_source_key]": offer["key"],
                "metadata[uat_source_sha256]": source_hash,
            }
            price = transport(
                key, "POST", "prices", fields, idempotency="cartray-ep-uat-" + str(offer_id) + "-" + source_hash[:32]
            )
        if (
            price.get("livemode") is not False
            or price.get("active") is not True
            or price.get("type") != "one_time"
            or price.get("recurring") is not None
            or price.get("unit_amount") != total
            or price.get("currency") != "aud"
            or price.get("lookup_key") != lookup
            or price.get("metadata", {}).get("uat_source_key") != offer["key"]
            or price.get("metadata", {}).get("uat_source_sha256") != source_hash
        ):
            raise ValueError(f"Stripe price does not match source-backed test mapping for {offer_id}")
        selected.append(
            {
                "source_key": offer["key"],
                "source_id": offer_id,
                "source_sku": offer["source_sku"],
                "cartray_product_key": f"EP-UAT-{offer_id}",
                "lookup_key": lookup,
                "stripe_price_id": price["id"],
                "amount_minor": total,
                "currency": "aud",
                "dummy_entitlement": f"EP-UAT-DUMMY-{offer_id}",
            }
        )
    return {
        "schema": "ep-uat-test-prices-v1",
        "stripe_account": expected_account,
        "livemode": False,
        "source_archive_sha256": catalogue["source_archive_sha256"],
        "offers": selected,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--stripe-account", required=True)
    parser.add_argument("--offer-id", type=int, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("test mapping output exists")
    if args.key_file.stat().st_mode & 0o077:
        raise ValueError("test key file must be private (0600)")
    spec = importlib.util.spec_from_file_location("ep_uat_prepare", Path(__file__).with_name("prepare.py"))
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    archive_hash = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    if archive_hash != source.APPROVED_SHA256:
        raise ValueError("test prices require the approved source archive")
    blobs, _manifest = source.read_archive(args.archive)
    catalogue = source.prepare(blobs, archive_hash)
    followups = dummy_followups(blobs, catalogue, args.offer_id)
    key = args.key_file.read_text().strip()
    result = provision(catalogue, args.offer_id, key, args.stripe_account)
    args.output.mkdir(parents=True, mode=0o700)
    lock = args.output / "test-price-map.json"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    with (args.output / "catalogue.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "product_key",
                "title",
                "stripe_lookup_key",
                "fulfilment_type",
                "fulfilment_version",
                "max_quantity",
                "active",
            ]
        )
        byid = {p["source_id"]: p for p in catalogue["products"]}
        for row in result["offers"]:
            writer.writerow(
                [
                    row["cartray_product_key"],
                    byid[row["source_id"]]["title"],
                    row["lookup_key"],
                    "download",
                    row["dummy_entitlement"],
                    10,
                    "true",
                ]
            )
    (args.output / "fulfilment-expansions.json").write_text(
        json.dumps({r["dummy_entitlement"]: [r["dummy_entitlement"]] for r in result["offers"]}, indent=2) + "\n"
    )
    (args.output / "dummy-download.txt").write_text(
        "CartRay Effective Policy UAT dummy file. This contains no paid materials.\n"
    )
    (args.output / "dummy-followups.json").write_text(
        json.dumps({"send_enabled": False, "items": followups}, indent=2) + "\n"
    )
    (args.output / "price-resolutions.json").write_text(
        json.dumps(
            {
                r["lookup_key"]: {
                    "stripe_price_id": r["stripe_price_id"],
                    "amount_minor": r["amount_minor"],
                    "currency": r["currency"],
                }
                for r in result["offers"]
            },
            indent=2,
        )
        + "\n"
    )
    product_assets = args.output / "product-assets"
    product_assets.mkdir()
    with (args.output / "presentation.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["product_key", "short_description", "image_key", "image_alt"])
        byid = {p["source_id"]: p for p in catalogue["products"]}
        for row in result["offers"]:
            offer = byid[row["source_id"]]
            image = offer["images"][0]
            name = Path(image["path"]).name
            (product_assets / name).write_bytes(blobs[image["path"]])
            description = offer["summary"] or offer["title"]
            writer.writerow([row["cartray_product_key"], description[:2000].strip(), name, image["alt"]])
    print(f"Created verified test-only mappings for {len(result['offers'])} offers; no checkout or delivery performed.")


if __name__ == "__main__":
    main()
