#!/usr/bin/env python3
"""Run local UAT browsing checks and capture measurements; never follows external actions."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright


def audit_source(source, root):
    """Reconcile generated pages against a prepared, approved UAT snapshot."""
    data = json.loads((source / "catalogue.json").read_text())
    counts = {"products": 0, "variations": 0, "external": 0, "free_download": 0, "media": 0}
    for item in data["public_media"]:
        relative = Path(item["path"])
        if relative.is_absolute() or relative.parts[:1] != ("media",) or ".." in relative.parts:
            raise ValueError("unsafe UAT media path")
        media = root / relative
        if not media.is_file() or not media.resolve().is_relative_to(root.resolve()):
            raise ValueError("missing UAT media")
        if hashlib.sha256(media.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("UAT media fingerprint mismatch")
        counts["media"] += 1
    for product in data["products"]:
        navigation = product["navigation_path"]
        if not isinstance(navigation, str) or not re.fullmatch(r"/products/[a-z0-9]+(?:-[a-z0-9]+)*/", navigation):
            raise ValueError("unsafe UAT navigation path")
        page = (root / navigation.lstrip("/") / "index.html").read_text()
        if html.escape(product["title"]) not in page:
            raise ValueError("listing title missing from its page")
        description = product["description_html"].replace("\r\n", "\n").replace("\r", "\n")
        if description and description not in page:
            raise ValueError("sanitized source description missing from its page")
        if any(image["path"] not in page for image in product["images"]):
            raise ValueError("product gallery image missing from its page")
        if product["canonical_url"] is not None or 'rel="canonical"' in page:
            raise ValueError("UAT must not invent source canonical URLs")
        if product["action"] == "external":
            if "data-uat-add=" in page or not (
                "External live site;" in page or "External destination unavailable" in page
            ):
                raise ValueError("external product exposes an inappropriate checkout action")
            counts["external"] += 1
        if product["action"] == "free_download":
            if "data-uat-add=" in page or "download>Download free catalogue" not in page:
                raise ValueError("free resource exposes an inappropriate checkout action")
            counts["free_download"] += 1
        for variant in product["variations"]:
            if variant["key"] not in page:
                raise ValueError("variation is missing from its parent page")
            if any(image["path"] not in page for image in variant["images"]):
                raise ValueError("variation gallery image missing from its parent page")
            counts["variations"] += 1
        counts["products"] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimal", type=Path, required=True)
    parser.add_argument("--studio", type=Path, required=True)
    parser.add_argument("--classic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engines", default="chromium,firefox,webkit")
    parser.add_argument("--themes", default="minimal,studio,classic")
    parser.add_argument("--expected-products", type=int, default=121)
    parser.add_argument("--source", type=Path, help="prepared catalogue for source/page reconciliation")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"platform": platform.platform(), "cpu_count": os.cpu_count(), "python": sys.version.split()[0],
              "runs": [], "failures": []}
    with sync_playwright() as pw:
        for theme in args.themes.split(","):
            root = getattr(args, theme)
            if args.source:
                report.setdefault("source_audits", {})[theme] = audit_source(args.source, root)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            process = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(root)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for engine in args.engines.split(","):
                    browser = None
                    try:
                        browser = getattr(pw, engine).launch(timeout=20000)
                        for layout, w, h in [("desktop", 1440, 900), ("mobile", 390, 844)]:
                            page = browser.new_page(viewport={"width": w, "height": h}, reduced_motion="reduce")
                            errors = []
                            failed = []
                            page.on("pageerror", lambda e: errors.append(str(e)))
                            page.on("response", lambda r: failed.append(r.url) if r.status >= 400 else None)
                            start = time.perf_counter()
                            page.goto(f"http://127.0.0.1:{port}/", wait_until="load", timeout=30000)
                            initial_ms = (time.perf_counter() - start) * 1000
                            initial_memory = page.evaluate(
                                "performance.memory ? performance.memory.usedJSHeapSize : null"
                            )
                            page.wait_for_selector("#products .product-card")
                            assert page.locator("#products .product-card").count() == args.expected_products
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
                            if layout == "mobile":
                                menu = page.locator("details.mobile-nav")
                                menu.locator("summary").focus()
                                menu.locator("summary").press("Enter")
                                assert menu.get_attribute("open") is not None
                                assert menu.get_by_role("link", name="Catalogue").is_visible()
                                menu.locator("summary").press("Enter")
                                assert menu.get_attribute("open") is None
                            page.screenshot(path=str(args.output / f"{theme}-{engine}-{layout}-catalogue.png"))
                            times = []
                            for _ in range(20):
                                start = time.perf_counter()
                                page.locator("#search").fill("consulting")
                                page.wait_for_function(
                                    "count => document.querySelector('#results-status').textContent "
                                    "!== `${count} products`",
                                    arg=args.expected_products,
                                )
                                times.append((time.perf_counter() - start) * 1000)
                                page.locator("#search").fill("")
                            page.locator("#search").fill("consulting")
                            page.screenshot(path=str(args.output / f"{theme}-{engine}-{layout}-search.png"))
                            page.locator("#search").fill("")
                            page.locator("#category").select_option(label="Free")
                            assert page.locator(".product-card:visible").count() > 0
                            page.screenshot(path=str(args.output / f"{theme}-{engine}-{layout}-category.png"))
                            resources = page.evaluate(
                                "performance.getEntriesByType('resource').map(x=>({name:x.name,bytes:x.transferSize}))"
                            )
                            navigation = page.evaluate("""() => {
                                const n = performance.getEntriesByType('navigation')[0];
                                return n ? {transfer: n.transferSize || null,
                                    dom_content_loaded_ms: n.domContentLoadedEventEnd || null} : null;
                            }""")
                            memory = page.evaluate("performance.memory ? performance.memory.usedJSHeapSize : null")
                            page.goto(f"http://127.0.0.1:{port}/products/woo-4311/", wait_until="load")
                            variant = page.locator("#variant")
                            assert variant.locator("option").count() == 4
                            variant.select_option("ep:woo:offer:7527")
                            page.reload()
                            assert variant.input_value() == "ep:woo:offer:7527"
                            assert "Checkout unavailable" in page.locator("#variant-status").inner_text()
                            assert "120" in page.locator("#variant-price").inner_text()
                            assert page.locator("#variant-description").inner_text()
                            page.screenshot(path=str(args.output / f"{theme}-{engine}-{layout}-variation.png"))
                            page.goto(f"http://127.0.0.1:{port}/products/woo-30/", wait_until="load")
                            assert page.locator("a[download]").count() == 1
                            page.screenshot(path=str(args.output / f"{theme}-{engine}-{layout}-free.png"))
                            page.goto(f"http://127.0.0.1:{port}/products/woo-171/", wait_until="load")
                            assert page.locator("[data-product-gallery]").count() == 1
                            page.screenshot(path=str(args.output / f"{theme}-{engine}-{layout}-product.png"))
                            page.goto(f"http://127.0.0.1:{port}/products/woo-148/", wait_until="load")
                            assert (
                                page.get_by_text("External live site; destination has not been tested in UAT.").count()
                                == 1
                            )
                            assert page.locator("[data-uat-add]").count() == 0
                            page.screenshot(path=str(args.output / f"{theme}-{engine}-{layout}-external.png"))
                            page.goto(f"http://127.0.0.1:{port}/cart/", wait_until="load")
                            assert page.get_by_text("Checkout is not yet enabled for this UAT catalogue.").count() == 1
                            page.screenshot(path=str(args.output / f"{theme}-{engine}-{layout}-cart.png"))
                            assert not errors, errors
                            assert not failed, failed
                            report["runs"].append(
                                {
                                    "theme": theme,
                                    "engine": engine,
                                    "browser_version": browser.version,
                                    "viewport": layout,
                                    "load_ms": round(initial_ms, 2),
                                    "search_ms_p50": round(statistics.median(times), 2),
                                    "search_ms_p95": round(sorted(times)[18], 2),
                                    "resource_transfer_bytes": sum(x["bytes"] for x in resources),
                                    "document_transfer_bytes": navigation["transfer"] if navigation else None,
                                    "dom_content_loaded_ms": (
                                        navigation["dom_content_loaded_ms"] if navigation else None
                                    ),
                                    "reported_js_heap_bytes": memory,
                                    "reported_js_heap_growth_bytes": (
                                        memory - initial_memory
                                        if memory is not None and initial_memory is not None else None
                                    ),
                                }
                            )
                            page.close()
                    except Exception:
                        report["failures"].append(
                            {"theme": theme, "engine": engine, "error": traceback.format_exc()[-1500:]}
                        )
                    finally:
                        if browser:
                            browser.close()
            finally:
                process.terminate()
                process.wait(timeout=5)
    (args.output / "browser-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"runs": len(report["runs"]), "failures": report["failures"]}, indent=2))
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
