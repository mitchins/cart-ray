#!/usr/bin/env python3
"""Chromium presentation regression and screenshot lane.

Default uses a local HTTP server. --offline uses the documented in-memory loader
for environments whose browser policy prohibits URL navigation. Neither lane
qualifies Stripe, Workers, email delivery, or the connected repository adapter.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from offline_browser import OfflineSite
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class HttpSite:
    def __init__(self, page, origin):
        self.page = page
        self.origin = origin
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def open(self, path):
        self.page.goto(self.origin + path)
        self.page.wait_for_timeout(100)
        return self.page

    def follow(self):
        self.page.wait_for_timeout(100)
        return self.page

    def reset(self):
        self.page.evaluate("()=>{localStorage.clear();sessionStorage.clear()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--origin", default="http://127.0.0.1:4173")
    ap.add_argument("--chromium", default=os.getenv("CHROMIUM_EXECUTABLE", "/usr/bin/chromium"))
    args = ap.parse_args()
    checks = []

    def check(name, value):
        checks.append({"name": name, "passed": bool(value)})
        if not value:
            raise AssertionError(name)

    shots = ROOT / "screenshots"
    shots.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page = context.new_page()
        site = OfflineSite(page, ROOT / "dist") if args.offline else HttpSite(page, args.origin)
        for slug in ["minimal", "studio", "classic", "northline-classic"]:
            is_north = slug.startswith("northline")
            key = "NORTH-ROLL" if is_north else "ACORN-KIT"
            second = "NORTH-APRON" if is_north else "ACORN-PLANTER"
            prod = "field-tool-roll" if is_north else "acorn-growing-kit"
            site.open("/" + slug + "/")
            site.reset()
            site.open("/" + slug + "/")
            for device, width, height in [("desktop", 1440, 1000), ("tablet", 768, 1024), ("mobile", 390, 844)]:
                page.set_viewport_size({"width": width, "height": height})
                site.open("/" + slug + "/")
                check(f"{slug} {device}: 3 products", page.locator(".product-card").count() == 3)
                check(
                    f"{slug} {device}: no horizontal overflow",
                    page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"),
                )
                check(f"{slug} {device}: one h1", page.locator("h1").count() == 1)
                check(f"{slug} {device}: all images have alt", page.locator("img:not([alt])").count() == 0)
                page.screenshot(path=str(shots / f"{slug}-home-{device}.png"), full_page=True)
            page.set_viewport_size({"width": 320, "height": 800})
            site.open("/" + slug + "/shop/")
            check(f"{slug}: 320px reflow", page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
            page.set_viewport_size({"width": 1440, "height": 1000})
            site.open("/" + slug + "/shop/")
            page.locator("[data-category-filter]").nth(1).click()
            check(f"{slug}: category filter", page.locator(".product-card").count() == 1)
            page.locator('[data-category-filter="all"]').click()
            page.locator("#sort").select_option("low")
            check(
                f"{slug}: price sorting",
                page.locator(".product-card").first.get_attribute("data-key")
                == ("NORTH-NOTES" if is_north else "ACORN-GUIDE"),
            )
            page.locator("#site-search").fill("no-such-product")
            page.locator(".search-form button").click()
            check(f"{slug}: search empty state", page.locator("#no-results").is_visible())
            page.locator("#reset-filter").click()
            check(f"{slug}: clear filter", page.locator(".product-card").count() == 3)
            page.locator(f'[data-add="{key}"]').click()
            page.locator(f'[data-add="{second}"]').click()
            check(f"{slug}: cart count", page.locator("[data-cart-count]").inner_text() == "2")
            site.open("/" + slug + "/cart/")
            check(f"{slug}: persisted two-product cart", page.locator(".cart-line").count() == 2)
            check(f"{slug}: preselected permission visible", page.locator("#review-invitation-opt-in").is_checked())
            page.locator(f'[data-quantity="{key}"][data-step="1"]').click()
            check(f"{slug}: cart quantity edit", page.locator("[data-cart-count]").inner_text() == "3")
            page.locator(f'[data-quantity="{key}"][data-step="-1"]').click()
            page.screenshot(path=str(shots / f"{slug}-cart-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            check(f"{slug}: mobile cart reflow", page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
            page.screenshot(path=str(shots / f"{slug}-cart-mobile.png"), full_page=True)
            page.locator("#review-invitation-opt-in").uncheck()
            page.locator("#checkout").click()
            site.follow()
            check(
                f"{slug}: no card fields",
                page.locator("input[autocomplete^=cc-], input[name=cardnumber]").count() == 0
                and page.locator(".checkout-handoff").count() == 1,
            )
            page.locator("#simulate-payment").click()
            site.follow()
            check(
                f"{slug}: simulated confirmation",
                page.locator("#content").inner_text().find("Your order is confirmed") >= 0,
            )
            site.open("/" + slug + "/inbox/")
            check(
                f"{slug}: opt-out suppresses invitation",
                page.locator("#content").inner_text().find("No invitation this time") >= 0,
            )
            site.reset()
            site.open("/" + slug + "/")
            page.locator(f'[data-add="{key}"]').click()
            page.locator(f'[data-add="{second}"]').click()
            site.open("/" + slug + "/cart/")
            page.locator("#checkout").click()
            site.follow()
            page.locator("#simulate-payment").click()
            site.follow()
            site.open("/" + slug + "/inbox/")
            check(f"{slug}: selected permission shows invitation", page.locator(".email-preview").count() == 1)
            site.open("/" + slug + "/review/")
            check(f"{slug}: two reviewable products", page.locator("[data-review-product]").count() == 2)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.screenshot(path=str(shots / f"{slug}-review-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            check(f"{slug}: mobile review reflow", page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
            page.screenshot(path=str(shots / f"{slug}-review-mobile.png"), full_page=True)
            page.locator('#review-form button[type="submit"]').click()
            check(
                f"{slug}: validation error takes focus",
                page.locator("textarea").evaluate("(e)=>e===document.activeElement"),
            )
            page.locator('input[name="rating"][value="5"]').check()
            page.locator('#review-form button[type="submit"]').click()
            check(
                f"{slug}: rating-only submission pending",
                page.locator(".review-summary").inner_text().find("awaiting moderation") >= 0,
            )
            page.locator(f'[data-review-product="{second}"]').click()
            check(f"{slug}: second product still available", page.locator("#review-form").count() == 1)
            page.locator("textarea").fill("A nice example project.")
            page.locator('#review-form button[type="submit"]').click()
            check(f"{slug}: text-only review submitted", page.locator(".review-summary").count() == 1)
            site.open("/" + slug + "/review/manage.html?product=" + key)
            page.locator('input[name="title"]').fill("Updated experience")
            page.locator('#manage-form button[type="submit"]').click()
            check(f"{slug}: edit returns to moderation", "awaiting moderation" in page.locator("#message").inner_text())
            page.locator("#withdraw-review").click()
            check(f"{slug}: withdrawal requires dialog", page.locator("dialog").is_visible())
            check(
                f"{slug}: initial dialog focus on cancel",
                page.locator("[data-dialog-cancel]").evaluate("(e)=>e===document.activeElement"),
            )
            page.keyboard.press("Tab")
            page.keyboard.press("Tab")
            check(f"{slug}: modal focus remains contained", page.evaluate('!!document.activeElement.closest("dialog")'))
            page.keyboard.press("Escape")
            check(
                f"{slug}: Escape closes and restores focus",
                not page.locator("dialog").is_visible()
                and page.locator("#withdraw-review").evaluate("(e)=>e===document.activeElement"),
            )
            page.locator("#withdraw-review").click()
            page.locator("[data-dialog-confirm]").click()
            check(f"{slug}: withdrawal completes", "Review withdrawn" in page.locator("#content").inner_text())
            site.open("/" + slug + "/unsubscribe/")
            check(f"{slug}: GET does not unsubscribe", page.locator("#unsubscribe-confirm").count() == 1)
            page.locator("#unsubscribe-confirm").click()
            check(f"{slug}: explicit unsubscribe", "You are unsubscribed" in page.locator("#content").inner_text())
            site.open("/" + slug + "/")
            page.locator(f'[data-add="{key}"]').click()
            site.open("/" + slug + "/cart/")
            page.locator("#checkout").click()
            site.follow()
            page.locator("#simulate-payment").click()
            site.follow()
            site.open("/" + slug + "/inbox/")
            check(
                f"{slug}: later purchase remains suppressed",
                "No invitation this time" in page.locator("#content").inner_text(),
            )
            site.open("/" + slug + "/products/" + prod + "/")
            page.screenshot(path=str(shots / f"{slug}-product-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.screenshot(path=str(shots / f"{slug}-product-desktop.png"), full_page=True)
            for state in ["confirmed", "pending", "unavailable", "cancelled", "expired"]:
                site.open("/" + slug + "/purchase-return/?state=" + state)
                check(f"{slug}: {state} return page", page.locator(".state-panel").count() == 1)
            # Keyboard baseline on a fresh page, without clicking controls first.
            site.open("/" + slug + "/")
            page.keyboard.press("Tab")
            check(
                f"{slug}: keyboard skip link first",
                page.locator(".skip-link").evaluate("(e)=>e===document.activeElement")
                or page.locator(".preview-bar a").evaluate("(e)=>e===document.activeElement"),
            )
            page.emulate_media(reduced_motion="reduce")
            check(
                f"{slug}: reduced motion",
                page.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto",
            )
            page.emulate_media(reduced_motion="no-preference")
            site.reset()
        check("no application exceptions", site.errors == [])
        browser.close()
    result = {
        "lane": "offline Chromium; real generated DOM/CSS/assets; module loading and navigation harnessed"
        if args.offline
        else "HTTP Chromium",
        "browser": "Chromium",
        "checks": len(checks),
        "passed": sum(c["passed"] for c in checks),
        "results": checks,
        "limitations": [
            "No full CartRay repository or hosted Stripe/Workers qualification in this environment.",
            "No axe-core engine was available; accessibility checks are targeted, not certification.",
            "Safari, Firefox, VoiceOver and NVDA remain integration checks.",
        ],
    }
    (ROOT / "evidence/browser-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"{result['passed']}/{result['checks']} browser checks passed ({result['lane']})")


if __name__ == "__main__":
    main()
