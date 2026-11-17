#!/usr/bin/env python3
"""Run local UAT browsing checks and capture measurements; never follows external actions."""

from __future__ import annotations

import argparse
import json
import platform
import socket
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimal", type=Path, required=True)
    parser.add_argument("--studio", type=Path, required=True)
    parser.add_argument("--classic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engines", default="chromium,firefox,webkit")
    parser.add_argument("--themes", default="minimal,studio,classic")
    parser.add_argument("--expected-products", type=int, default=121)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"platform": platform.platform(), "python": sys.version.split()[0], "runs": [], "failures": []}
    with sync_playwright() as pw:
        for theme in args.themes.split(","):
            root = getattr(args, theme)
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
                            page.wait_for_selector("#products .product-card")
                            assert page.locator("#products .product-card").count() == args.expected_products
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
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
                            assert not errors, errors
                            assert not failed, failed
                            report["runs"].append(
                                {
                                    "theme": theme,
                                    "engine": engine,
                                    "viewport": layout,
                                    "load_ms": round(initial_ms, 2),
                                    "search_ms_p50": round(statistics.median(times), 2),
                                    "search_ms_p95": round(sorted(times)[18], 2),
                                    "resource_transfer_bytes": sum(x["bytes"] for x in resources),
                                    "reported_js_heap_bytes": memory,
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
