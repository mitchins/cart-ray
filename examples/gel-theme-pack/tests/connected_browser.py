#!/usr/bin/env python3
"""HTTP browser qualification of the real adapter and native clients using explicit test API fixtures.

This verifies presenter integration, not hosted Stripe, eligibility or email delivery.
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:4174")
    parser.add_argument("--engines", default="chromium,webkit,firefox")
    parser.add_argument("--label", choices=("minimal", "studio", "classic", "northline-classic"))
    args = parser.parse_args()
    checks = []
    completed_engines = []
    prefix = f"{args.label}-" if args.label else ""
    results = ROOT / (
        f"evidence/connected-browser-{args.label}.json" if args.label else "evidence/connected-browser-results.json"
    )
    shots = ROOT / "screenshots"
    shots.mkdir(exist_ok=True)

    def check(name, result):
        checks.append({"name": name, "passed": bool(result)})
        assert result, name

    with sync_playwright() as p:
        for engine in args.engines.split(","):
            print("Checking " + engine, flush=True)
            browser = getattr(p, engine).launch(headless=True, timeout=15000)
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(args.origin + "/")
                page.wait_for_selector(".product-card")
                check(
                    engine + " unique generated page title",
                    page.title() == page.locator("body").get_attribute("data-page-title"),
                )
                for label, width, height in [("desktop", 1440, 1000), ("tablet", 768, 1024), ("mobile", 390, 844)]:
                    page.set_viewport_size({"width": width, "height": height})
                    check(
                        engine + " connected " + label + " reflow",
                        page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"),
                    )
                    page.screenshot(path=str(shots / f"connected-{prefix}{engine}-home-{label}.png"), full_page=True)
                check(
                    engine + " real adapter has no demo module",
                    not page.evaluate(
                        "performance.getEntriesByType('resource').some(x=>x.name.includes('demo-adapter'))"
                    ),
                )
                check(engine + " mobile reflow", page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))
                check(
                    engine + " reduced motion",
                    page.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto",
                )
                keys = page.evaluate("Object.keys(window.CARTRAY_STOREFRONT.productPaths)")[:2]
                names = [page.locator(f'[data-key="{key}"] h3').inner_text() for key in keys]
                for key in keys:
                    page.locator(f'[data-add="{key}"]').click()
                page.goto(args.origin + "/cart/")
                page.wait_for_selector(".cart-line")
                check(
                    engine + " authoritative quantity maximum",
                    page.locator('[data-quantity][data-step="1"]').first.is_disabled(),
                )
                page.evaluate("window.CARTRAY_THEME.gel.identity.currency='USD'")
                page.locator('[data-quantity][data-step="-1"]').first.click()
                check(engine + " quantity removal", page.locator("[data-cart-count]").inner_text() == "1")

                expected_subtotal = page.evaluate(
                    """key=>{const p=window.CARTRAY_STOREFRONT.previewCatalogue.products.find(p=>p.product_key===key);
                    return new Intl.NumberFormat(window.CARTRAY_THEME.gel.identity.locale,
                    {style:'currency',currency:p.currency}).format(p.amount_minor/100)}""",
                    keys[1],
                )
                check(
                    engine + " subtotal uses authoritative currency",
                    page.locator(".order-summary .total").inner_text().strip().endswith(expected_subtotal),
                )
                page.screenshot(path=str(shots / f"connected-{prefix}{engine}-cart-mobile.png"), full_page=True)
                check(engine + " checkout disabled by merchant config", page.locator("#checkout").is_disabled())
                check(
                    engine + " reviews invitation disabled by merchant config",
                    page.locator("#review-invitation-opt-in").count() == 0,
                )
                requests = []
                attempts = []

                def api(route):
                    request = route.request
                    requests.append(request.url)
                    data = request.post_data_json or {}
                    if request.url.endswith("/invitation"):
                        route.fulfill(
                            json={
                                "store_name": "Test store",
                                "draft_scope": "test-scope",
                                "subjects": [
                                    {"subject_id": "kit", "public_name": names[0], "progress": "available"},
                                    {"subject_id": "planter", "public_name": names[1], "progress": "available"},
                                ],
                            }
                        )
                    elif request.url.endswith("/contributions"):
                        attempts.append((request.headers.get("idempotency-key"), data))
                        route.fulfill(status=503, json={"error": "unavailable"})
                    elif request.url.endswith("/read"):
                        route.fulfill(
                            json={
                                "rating": 4,
                                "body": "Good planter",
                                "title": "Nice",
                                "author": "Test",
                                "revision_id": "revision-1",
                            }
                        )
                    else:
                        route.fulfill(json={})

                page.route("**/reviews/v1/**", api)
                token = "a" * 64
                page.goto(args.origin + "/review/#invitation=" + token)
                page.wait_for_selector("[data-product-chooser]")
                page.screenshot(path=str(shots / f"connected-{prefix}{engine}-review-mobile.png"), full_page=True)
                check(engine + " two-product native choice", page.locator("[data-product-chooser] button").count() == 2)
                form = page.locator("#content > form:visible")
                form.locator('input[name="rating-stars"][value="5"]').check()
                check(
                    engine + " stars update native select", form.locator('select[name="rating"]').input_value() == "5"
                )
                with page.expect_response(lambda response: response.url.endswith("/reviews/v1/contributions")):
                    form.locator('button[type="submit"]').click()
                page.wait_for_function("document.querySelector('#message').textContent.includes(\"couldn't submit\")")
                with page.expect_response(lambda response: response.url.endswith("/reviews/v1/contributions")):
                    form.locator('button[type="submit"]').click()
                check(
                    engine + " lost acknowledgement stable retry key",
                    len(attempts) == 2 and attempts[0][0] == attempts[1][0] and attempts[0][0],
                )
                check(
                    engine + " rating-only native payload",
                    attempts[0][1]["rating"] == 5 and attempts[0][1]["body"] == "",
                )
                check(
                    engine + " capability absent from theme storage",
                    page.evaluate("token=>!JSON.stringify({...localStorage,...sessionStorage}).includes(token)", token),
                )
                page.locator("[data-product-chooser] button").nth(1).click()
                check(
                    engine + " second purchased product chosen",
                    names[1] in page.locator("#content > form:visible").inner_text(),
                )
                form = page.locator("#content > form:visible")
                form.locator("textarea").fill("Lovely planter")
                with page.expect_response(lambda response: response.url.endswith("/reviews/v1/contributions")):
                    form.locator('button[type="submit"]').click()
                check(
                    engine + " comment-only native payload",
                    attempts[-1][1]["rating"] is None and attempts[-1][1]["body"] == "Lovely planter",
                )
                check(engine + " review independent of catalogue", not any("/catalogue" in url for url in requests))
                page.goto(args.origin + "/review/manage.html#review=" + token + "&token=" + "b" * 64)
                page.wait_for_selector('input[name="rating-stars"]')
                page.get_by_role("button", name="Withdraw review", exact=True).first.click()
                check(engine + " native dialog visible", page.locator("dialog").is_visible())
                page.keyboard.press("Escape")
                check(
                    engine + " dialog escape restores focus",
                    page.evaluate("document.activeElement.textContent==='Withdraw review'"),
                )
                page.get_by_role("button", name="Withdraw review", exact=True).first.click()
                page.locator("[data-dialog-confirm]").click()
                page.wait_for_function("document.querySelector('#message').textContent.includes('has been withdrawn')")
                check(engine + " native withdrawal", not page.locator("#content > form").count())
                page.goto(args.origin + "/review/#invitation=invalid")
                page.wait_for_function("document.querySelector('#message').textContent.includes('unavailable')")
                check(engine + " invalid invitation rejected", page.locator("#content form").count() == 0)
                check(engine + " no script errors", not errors)
                context.close()
                completed_engines.append(engine)
            except Exception as error:
                checks.append({"name": engine + " browser execution", "passed": False, "error": str(error)})
                raise
            finally:
                browser.close()
                results.write_text(
                    json.dumps(
                        {
                            "lane": "HTTP real adapter/native clients; API fixture responses only; no hosted claims",
                            "composition": args.label or "classic",
                            "completed_engines": completed_engines,
                            "complete": len(completed_engines) == len(args.engines.split(",")),
                            "checks": checks,
                        },
                        indent=2,
                    )
                    + "\n"
                )
            print(engine + " passed", flush=True)
    print(f"{len(checks)} connected checks passed: {args.engines}")


if __name__ == "__main__":
    main()
