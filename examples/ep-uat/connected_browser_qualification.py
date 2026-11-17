"""Exercise the connected CartRay cart and synthetic return in an isolated local browser."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine", choices=("chromium", "firefox", "webkit"), default="chromium")
    args = parser.parse_args()
    parsed = urlsplit(args.origin)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.port:
        raise ValueError("qualification is restricted to an explicit local test server")
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = []
    with sync_playwright() as playwright:
        browser = getattr(playwright, args.engine).launch(headless=True)
        for name, viewport in (("desktop", {"width": 1440, "height": 900}), ("mobile", {"width": 390, "height": 844})):
            context = browser.new_context(viewport=viewport)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(args.origin + "/products/woo-171/", wait_until="domcontentloaded")
            page.wait_for_function('!document.querySelector("[data-uat-add]").disabled')
            page.locator("[data-uat-add]").click()
            page.goto(args.origin + "/cart/", wait_until="domcontentloaded")
            page.get_by_role("button", name="Continue to secure payment").wait_for()
            assert "Core Module" in page.locator("#content").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            assert page.locator('a[href="/help/"]').count() == 0
            page.screenshot(path=str(args.output / f"{name}-cart.png"), full_page=True)
            # This is an isolated synthetic response: server authority is tested separately.
            page.route("**/checkout-status?*", lambda route: route.fulfill(json={"state": "confirmed"}))
            grants = {
                "grants": [
                    {"source_id": 171, "title": "Core Module", "followups": []},
                    {"source_id": 131, "title": "Incident Policy", "followups": []},
                ]
            }
            page.route("**/__uat/current-grants?*", lambda route: route.fulfill(json=grants))
            page.goto(
                args.origin + "/purchase-return/?checkout=complete&session_id=cs_test_mock",
                wait_until="domcontentloaded",
            )
            page.get_by_text("Your test downloads").wait_for()
            assert page.get_by_role("link", name="Download dummy test file for Core Module").count() == 1
            assert page.get_by_role("link", name="Download dummy test file for Incident Policy").count() == 1
            page.reload(wait_until="domcontentloaded")
            page.get_by_text("Your test downloads").wait_for()
            page.screenshot(path=str(args.output / f"{name}-return.png"), full_page=True)
            page.goto(args.origin + "/purchase-return/?checkout=cancelled", wait_until="domcontentloaded")
            page.get_by_text("Checkout cancelled.", exact=False).wait_for()
            assert page.get_by_text("Your test downloads").count() == 0
            assert not errors, errors
            evidence.append(
                {
                    "viewport": name,
                    "connected_cart": True,
                    "synthetic_return": True,
                    "confirmed_reload": True,
                    "cancelled_not_previous_order": True,
                    "page_errors": errors,
                }
            )
            context.close()
        browser.close()
    (args.output / "report.json").write_text(json.dumps({"engine": args.engine, "results": evidence}, indent=2) + "\n")


if __name__ == "__main__":
    main()
