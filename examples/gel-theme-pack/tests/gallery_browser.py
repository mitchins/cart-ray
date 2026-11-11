#!/usr/bin/env python3
"""Gallery interaction and layout regression; no backend or checkout calls."""

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def connected_gallery(playwright, args):
    shots = ROOT / "screenshots"
    shots.mkdir(exist_ok=True)
    results = ROOT / f"evidence/gallery-browser-{args.label}.json"
    checks = []
    completed = []
    try:
        for engine in args.engines.split(","):
            executable = os.getenv("CHROMIUM_EXECUTABLE") if engine == "chromium" else None
            browser = getattr(playwright, engine).launch(executable_path=executable)
            try:
                page = browser.new_page(reduced_motion="reduce")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(args.origin + "/shop/")
                page.wait_for_selector(".product-card")
                products = page.evaluate("window.CARTRAY_STOREFRONT.previewCatalogue.products")
                paths = page.evaluate("window.CARTRAY_STOREFRONT.productPaths")
                if args.label != "northline-classic":
                    assert any(len(product.get("images", [])) > 1 for product in products)
                for viewport, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(args.origin + "/shop/")
                    page.wait_for_selector(".product-card")
                    for product in products:
                        card = page.locator(f'[data-key="{product["product_key"]}"] .product-art img')
                        assert card.get_attribute("src") == product["image_url"]
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
                    page.screenshot(
                        path=str(shots / f"gallery-{args.label}-{engine}-catalogue-{viewport}.png"), full_page=True
                    )
                    for product in products:
                        key = product["product_key"]
                        page.goto(args.origin + paths[key])
                        page.wait_for_function("document.querySelector('[data-gallery-image]').complete")
                        expected = product.get("images") or [{"url": product["image_url"]}]
                        page.wait_for_function(
                            "count=>document.querySelectorAll('.gallery-thumbnail').length===count", arg=len(expected)
                        )
                        buttons = page.locator(".gallery-thumbnail")
                        image = page.locator("[data-gallery-image]")
                        assert image.get_attribute("src") == product["image_url"]
                        assert image.evaluate("image=>image.naturalWidth > 0")
                        stage = page.locator(".gallery-stage").bounding_box()
                        for index, item in enumerate(expected):
                            if len(expected) > 1:
                                buttons.nth(index).click()
                            assert image.get_attribute("src") == item["url"]
                            page.wait_for_function(
                                "()=>{const image=document.querySelector('[data-gallery-image]');"
                                "return image.complete && image.naturalWidth > 0}"
                            )
                            if item.get("alt"):
                                assert image.get_attribute("alt") == item["alt"]
                            assert page.locator(".gallery-stage").bounding_box()["height"] == stage["height"]
                        if len(expected) > 1:
                            buttons.last.press("Home")
                            assert buttons.first.get_attribute("aria-pressed") == "true"
                            buttons.first.press("ArrowLeft")
                            assert buttons.last.get_attribute("aria-pressed") == "true"
                            buttons.last.press("ArrowRight")
                        else:
                            assert page.locator("[data-gallery-controls]").is_hidden()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
                        page.screenshot(
                            path=str(shots / f"gallery-{args.label}-{engine}-{key}-{viewport}.png"), full_page=True
                        )
                        checks.append(
                            {
                                "engine": engine,
                                "viewport": viewport,
                                "product": key,
                                "images": len(expected),
                                "passed": True,
                            }
                        )
                assert not errors, errors
                completed.append(engine)
            finally:
                browser.close()
    finally:
        results.write_text(
            json.dumps(
                {
                    "composition": args.label,
                    "completed_engines": completed,
                    "complete": len(completed) == len(args.engines.split(",")),
                    "checks": checks,
                },
                indent=2,
            )
            + "\n"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin")
    parser.add_argument("--label", default="minimal", choices=("minimal", "studio", "classic", "northline-classic"))
    parser.add_argument("--engines", default="chromium")
    args = parser.parse_args()
    if args.origin:
        with sync_playwright() as playwright:
            connected_gallery(playwright, args)
        print(f"Connected gallery qualification passed: {args.label}, {args.engines}")
        return

    script = (ROOT / "src/gallery.mjs").read_text().replace("export function", "function")
    with sync_playwright() as playwright:
        for engine in args.engines.split(","):
            executable = os.getenv("CHROMIUM_EXECUTABLE") if engine == "chromium" else None
            browser = getattr(playwright, engine).launch(executable_path=executable)
            page = browser.new_page()
            pixel = (
                'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="800" height="720">'
                '<rect width="800" height="720" fill="green"/></svg>'
            )
            page.route(
                "https://gallery.test/**",
                lambda route: (
                    route.fulfill(body=pixel.split(",", 1)[1], content_type="image/svg+xml")
                    if "missing" not in route.request.url
                    else route.abort()
                ),
            )
            for theme in ("minimal", "studio", "classic"):
                for width in (375, 1280):
                    page.set_viewport_size({"width": width, "height": 900})
                    css = (ROOT / "src/base.css").read_text() + (ROOT / f"src/{theme}.css").read_text()
                    page.set_content(
                        f'<style>{css}</style><body class="theme-{theme}"><section class="product-detail wrap">'
                        '<figure class="detail-art" data-product-gallery><div class="gallery-stage">'
                        '<img data-gallery-image width="800" height="720">'
                        "<p data-gallery-fallback hidden>Image unavailable</p></div>"
                        "<div data-gallery-controls hidden></div>"
                        '<figcaption data-gallery-status role="status"></figcaption></figure></section></body>'
                    )
                    page.add_script_tag(content=script)
                    page.evaluate(
                        """()=>mountGallery(document.querySelector('figure'),{title:'Kit',image_url:'https://gallery.test/primary.svg',images:[{url:'https://gallery.test/primary.svg',alt:'Front'},{url:'https://gallery.test/side.svg',alt:'Side'},{url:'https://gallery.test/missing.svg',alt:'Missing'}]})"""
                    )
                    buttons = page.locator(".gallery-thumbnail")
                    assert buttons.count() == 3
                    stage = page.locator(".gallery-stage").bounding_box()
                    buttons.nth(1).click()
                    assert page.locator("[data-gallery-image]").get_attribute("alt") == "Side"
                    buttons.nth(1).press("Home")
                    assert buttons.nth(0).get_attribute("aria-pressed") == "true"
                    buttons.nth(0).press("ArrowLeft")
                    page.locator("[data-gallery-fallback]").wait_for(state="visible")
                    buttons.nth(2).press("ArrowRight")
                    page.locator("[data-gallery-image]").wait_for(state="visible")
                    assert page.locator(".gallery-stage").bounding_box()["height"] == stage["height"]
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert page.locator(".gallery-thumbnail img").first.get_attribute("loading") == "lazy"
            browser.close()
    print("Gallery pointer, keyboard, fallback and stable mobile/desktop layouts passed in all three themes.")


if __name__ == "__main__":
    main()
