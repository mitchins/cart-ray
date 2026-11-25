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
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    checks = []
    completed_engines = []
    prefix = f"{args.label}-" if args.label else ""
    evidence_root = args.evidence_dir or ROOT
    (evidence_root / "evidence").mkdir(parents=True, exist_ok=True)
    results = evidence_root / (
        f"evidence/connected-browser-{args.label}.json" if args.label else "evidence/connected-browser-results.json"
    )
    shots = evidence_root / "screenshots"
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
                context.add_init_script("window.CARTRAY_REVIEWS_UI={contactPath:'/contact-us/'}")
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
                wording = "I'm happy to receive invitations to review products I purchase."

                def enable_review_permission(route):
                    response = route.fetch()
                    extra = {
                        "checkoutEnabled": True,
                        "reviewInvitationsEnabled": True,
                        "reviewInvitationDefaultChecked": True,
                        "reviewInvitationWordingVersion": "review-invitation-v3",
                        "reviewInvitationWording": wording,
                    }
                    route.fulfill(
                        response=response,
                        body=response.text() + "\nObject.assign(window.CARTRAY_STOREFRONT," + json.dumps(extra) + ");",
                    )

                page.route("**/storefront-config.js", enable_review_permission)
                page.reload()
                choice = page.get_by_role("checkbox", name=wording)
                choice.wait_for()
                check(engine + " general review disclosure defaults checked", choice.is_checked())
                choice.focus()
                choice.press("Space")
                check(
                    engine + " keyboard review opt-out leaves checkout available",
                    not choice.is_checked() and page.locator("#checkout").is_enabled(),
                )
                choice.press("Space")
                check(engine + " keyboard review permission can be restored", choice.is_checked())
                page.screenshot(path=str(shots / f"connected-{prefix}{engine}-permission-mobile.png"), full_page=True)
                page.unroute("**/storefront-config.js", enable_review_permission)
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
                check(
                    engine + " native ready semantic state",
                    page.locator("#content").get_attribute("data-cr-state") == "ready",
                )
                check(
                    engine + " native field and action groups",
                    page.locator(".cr-field").count() >= 4 and page.locator(".cr-actions").count() == 2,
                )
                for label, width in [("desktop", 1440), ("mobile", 390), ("narrow", 320)]:
                    page.set_viewport_size({"width": width, "height": 900})
                    check(
                        engine + " native form " + label + " reflow",
                        page.evaluate("document.documentElement.scrollWidth<=innerWidth+1"),
                    )
                    page.screenshot(path=str(shots / f"connected-{prefix}{engine}-review-{label}.png"), full_page=True)
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
                def long_management_page():
                    # Keep a long footer after the form disappears, reproducing a merchant page.
                    page.evaluate("""()=>{
                      const form=document.querySelector('#content > form');
                      form.style.minHeight='1600px';form.style.display='flex';form.style.flexDirection='column';
                      form.querySelector('.cr-actions').style.marginTop='auto';
                      const footer=document.createElement('footer');footer.style.height='1800px';
                      footer.textContent='Merchant footer';document.body.append(footer);
                    }""")

                def result_is_visible():
                    return page.evaluate("""()=>{
                      const message=document.querySelector('#message'), box=message.getBoundingClientRect();
                      return document.activeElement===message && box.top>=0 && box.bottom<=innerHeight;
                    }""")

                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(args.origin + "/review/manage.html#review=" + token + "&token=" + "b" * 64)
                page.wait_for_selector('input[name="rating-stars"]')
                long_management_page()
                page.get_by_role("button", name="Withdraw review", exact=True).first.click()
                check(engine + " native dialog visible", page.locator("dialog").is_visible())
                check(
                    engine + " destructive/secondary dialog hooks",
                    page.locator("[data-dialog-cancel].cr-button--secondary").count() == 1
                    and page.locator("[data-dialog-confirm].cr-button--danger").count() == 1,
                )
                page.screenshot(path=str(shots / f"connected-{prefix}{engine}-withdraw-dialog.png"), full_page=True)
                page.keyboard.press("Escape")
                check(
                    engine + " dialog escape restores focus",
                    page.evaluate("document.activeElement.textContent==='Withdraw review'"),
                )
                page.get_by_role("button", name="Withdraw review", exact=True).first.click()
                page.locator("[data-dialog-confirm]").click()
                page.wait_for_function("document.querySelector('#message').textContent.includes('has been withdrawn')")
                check(engine + " native withdrawal", not page.locator("#content > form").count())
                check(engine + " mobile withdrawal result focused and visible", result_is_visible())
                page.screenshot(path=str(shots / f"connected-{prefix}{engine}-withdraw-result-mobile.png"))
                for action, width in [("edit", 390), ("edit", 320), ("edit", 1440), ("withdraw", 320)]:
                    page.set_viewport_size({"width": width, "height": 844})
                    page.goto(args.origin + "/review/manage.html#review=" + token + "&token=" + "b" * 64)
                    page.reload()  # The same fragment URL otherwise keeps the previous terminal document.
                    page.wait_for_selector('input[name="rating-stars"]')
                    long_management_page()
                    if action == "edit":
                        page.locator('[data-cr-action=edit]').click()
                    else:
                        page.locator('[data-cr-action=withdraw]').click()
                        page.locator('[data-dialog-confirm]').click()
                    state = "submitted" if action == "edit" else "withdrawn"
                    page.wait_for_function(
                        "state=>document.querySelector('#content').dataset.crState===state", arg=state
                    )
                    check(engine + " " + action + " result focused and visible " + str(width), result_is_visible())
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(args.origin + "/review/#invitation=invalid")
                page.wait_for_function("document.querySelector('#message').textContent.includes('unavailable')")
                check(engine + " invalid invitation rejected", page.locator("#content form").count() == 0)
                for suffix in ["/review/", "/review/manage.html"]:
                    page.goto(args.origin + suffix)
                    page.wait_for_function("document.querySelector('#content').dataset.crState==='invalid'")
                    check(
                        engine + " terminal loading cleared " + suffix,
                        "loading" not in page.locator("#content").inner_text().lower(),
                    )
                page.goto(args.origin + "/review/#invitation=" + token)
                page.wait_for_selector("#content form")
                page.route("**/reviews/v1/invitation", lambda route: route.abort())
                page.reload()
                page.wait_for_selector("[data-cr-action=retry]")
                check(
                    engine + " network failure settles loading",
                    page.locator("#content").get_attribute("aria-busy") == "false",
                )
                page.unroute("**/reviews/v1/invitation")
                check(
                    engine + " safe merchant contact action",
                    page.get_by_role("link", name="Contact the store").get_attribute("href") == "/contact-us/",
                )
                for mode, pattern, suffix, value in [
                    (
                        "review",
                        "**/reviews/v1/invitation",
                        "/review/#invitation=" + token,
                        {
                            "store_name": "Test store",
                            "draft_scope": "retry-test",
                            "subjects": [{"subject_id": "kit", "public_name": "Product", "progress": "available"}],
                        },
                    ),
                    (
                        "manage",
                        "**/reviews/v1/manage/*/read",
                        "/review/manage.html#review=" + token + "&token=" + "b" * 64,
                        {"rating": 4, "body": "Existing text", "author": "Test", "revision_id": "revision-1"},
                    ),
                ]:
                    page.goto('about:blank')
                    pending = []
                    page.route(pattern, lambda route: pending.append(route))
                    page.goto(args.origin + suffix)
                    page.wait_for_function("document.querySelector('#content').dataset.crState==='loading'")
                    page.evaluate("document.readyState")
                    assert len(pending) == 1
                    pending[0].fulfill(status=503, json={"error": "unavailable"})
                    page.wait_for_selector("[data-cr-action=retry]")
                    with page.expect_request(
                        lambda request: request.url.endswith("/invitation") or request.url.endswith("/read")
                    ):
                        page.evaluate("let b=document.querySelector('[data-cr-action=retry]');b.click();b.click()")
                    check(
                        engine + " retry synchronously disabled " + mode,
                        page.locator("[data-cr-action=retry]").is_disabled(),
                    )
                    assert len(pending) == 2
                    with page.expect_request(
                        lambda request: request.url.endswith("/invitation") or request.url.endswith("/read")
                    ):
                        page.evaluate(
                            "document.querySelector('[data-cr-action=retry]').dispatchEvent(new Event('click'))"
                        )
                    page.evaluate("document.readyState")
                    assert len(pending) == 3
                    pending[2].fulfill(json=value)
                    page.wait_for_selector("#content textarea")
                    page.locator("#content textarea").fill("Keep this new draft")
                    pending[1].fulfill(status=503, json={"error": "late failure"})
                    page.evaluate("document.readyState")
                    check(
                        engine + " late retry cannot erase edited form " + mode,
                        page.locator("#content textarea").input_value() == "Keep this new draft",
                    )
                    page.unroute(pattern)
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
