#!/usr/bin/env python3
"""Qualify merchant catalogue navigation using an isolated synthetic storefront."""
import functools
import http.server
import json
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/ep_uat"))
from test_build import make_source, module  # noqa: E402


def main():
    with tempfile.TemporaryDirectory(prefix="cartray-route-") as temporary:
        root = Path(temporary)
        source = root / "source"
        data = make_source(source)
        product = data["products"][0]
        product["source_delivery_kind"] = "uat_dummy_transfer"
        (source / "catalogue.json").write_text(json.dumps(data))
        fixture = root / "fixture.json"
        fixture.write_text(json.dumps({"livemode": False, "source_archive_sha256": data["source_archive_sha256"],
            "offers": [{"source_id": 1, "source_key": product["key"], "currency": "aud", "amount_minor": 1200,
                        "cartray_product_key": "EP-UAT-1"}]}))
        site = root / "site"
        module.build(source, site, "classic", test_fixture=fixture, catalogue_path="/policies-and-templates/")
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
            functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.route("**/__uat/context", lambda route: route.fulfill(json={"ok": True}))
                page.route("**/catalogue", lambda route: route.fulfill(json={"version": "fixture-v1", "products": [
                    {"product_key": "EP-UAT-1", "title": "A product", "currency": "AUD", "amount_minor": 1200,
                     "max_quantity": 1}]}))
                page.route("**/checkout-status?*", lambda route: route.fulfill(json={"state": "confirmed"}))
                page.route("**/__uat/current-grants?*", lambda route: route.fulfill(json={"grants": []}))
                origin = f"http://127.0.0.1:{server.server_port}"
                page.goto(origin + "/products/woo-1/")
                page.wait_for_function("!document.querySelector('[data-uat-add]').disabled")
                page.locator("[data-uat-add]").click()
                page.goto(origin + "/cart/")
                page.wait_for_selector(".cart-line")
                cart_link = page.get_by_role("link", name="Continue shopping")
                assert cart_link.get_attribute("href") == "/policies-and-templates/"
                assert page.locator('form[role="search"]').get_attribute("action") == "/policies-and-templates/"
                page.goto(origin + "/purchase-return/?checkout=complete&session_id=cs_test_fixture")
                link = page.get_by_role("link", name="Continue shopping")
                link.wait_for()
                assert link.get_attribute("href") == "/policies-and-templates/"
                link.click()
                assert page.url == origin + "/policies-and-templates/"
                assert page.locator(".product-card").count() == 1
                page.goto(origin + "/cart/")
                page.locator("#site-search").fill("not found")
                page.locator("#site-search").press("Enter")
                page.wait_for_url("**/policies-and-templates/?q=not+found")
                assert page.locator("#results-status").inner_text() == "0 products"
                browser.close()
                print("Connected product → cart → confirmed return → merchant catalogue: passed (synthetic API)")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
