"""Real browser CSP/render/form qualification; dummy local resources, not entitlement authority."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from cartray.order_access_presentation import render_order_access_page


def main():
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--engines", default="chromium,firefox,webkit")
    parser.add_argument("--output", type=Path, default=Path("/tmp/cartray-order-page-browser"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    env = SimpleNamespace(
        CARTRAY_ORDER_ACCESS_PRESENTATION=json.dumps(
            {
                "name": "Reference merchant",
                "home_path": "/catalogue/",
                "contact_path": "/contact-us/",
                "accent": "#001b35",
                "logo_path": "/assets/merchant-logo.svg",
                "heading_font": "editorial",
            }
        )
    )
    html, csp = render_order_access_page(env)
    calls = []
    unavailable = [False]
    empty = [False]
    long_name = 'Approved-' + 'x' * 155 + '.pdf'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            logo = self.path == "/assets/merchant-logo.svg"
            body = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="50" viewBox="0 0 200 50">'
                    '<rect width="200" height="50" fill="#001b35"/></svg>') if logo else (
                        html if self.path == "/order-access" else "Approved external dummy resource")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml" if logo else "text/html; charset=utf-8")
            if self.path == "/order-access":
                self.send_header("Content-Security-Policy", csp)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(body)
            if self.headers.get("Content-Type") == "application/x-www-form-urlencoded":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/external")
                self.end_headers()
                return
            self.send_response(503 if unavailable[0] else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if empty[0]:
                self.wfile.write(b'{"items":[]}')
                return
            self.wfile.write(
                json.dumps(
                    {
                        "items": [
                            {
                                "title": "Purchased product",
                                "quantity": 2,
                                "resources": [
                                    {"name": "External dummy resource", "index": "0.0", "available": True},
                                    {"name": "Pending file", "index": "0.1", "available": False},
                                    {"name": long_name, "index": "0.2", "available": True},
                                ],
                            }
                        ]
                    }
                ).encode()
            )

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            for engine in args.engines.split(","):
                browser = getattr(playwright, engine).launch(headless=True, timeout=15000)
                try:
                    page = browser.new_page()
                    violations = []
                    page.add_init_script(
                        "document.addEventListener('securitypolicyviolation',e=>{"
                        "if(e.violatedDirective==='img-src' && e.blockedURI===location.origin+'/favicon.ico')return;"
                        "console.error('native-csp:'+e.violatedDirective)})"
                    )
                    page.on(
                        "console",
                        lambda item: violations.append(item.text) if item.text.startswith("native-csp:") else None,
                    )
                    page.on("pageerror", lambda error: violations.append(str(error)))
                    page.goto(origin + "/order-access#token=" + "a" * 43)
                    page.wait_for_selector("[data-cr-state=ready]")
                    assert page.get_by_role("button", name="Pending file").is_disabled()
                    page.wait_for_function("document.querySelector('.cr-order-logo').naturalWidth>0")
                    assert (page.get_by_role('img', name='Reference merchant').get_attribute('src')
                            == '/assets/merchant-logo.svg')
                    assert 'Georgia' in page.locator('h1').evaluate('e=>getComputedStyle(e).fontFamily')
                    assert page.get_by_role('list', name='Purchased resources').locator('li').count() == 3
                    assert page.locator('.cr-resource-name').all_inner_texts() == [
                        'External dummy resource', 'Pending file', long_name]
                    assert page.get_by_role("link", name="Contact the store").get_attribute("href") == "/contact-us/"
                    for name, width in [("desktop", 1440), ("mobile", 390), ("narrow", 320)]:
                        page.set_viewport_size({"width": width, "height": 900})
                        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
                        assert (
                            page.get_by_role("button", name="External dummy resource").evaluate(
                                "e=>getComputedStyle(e).minHeight"
                            )
                            == "48px"
                        )
                        # WebKit screenshot instrumentation injects a style that strict CSP rejects.
                        # Keep CSP intact and qualify its real layout/controls without capture injection.
                        if engine != "webkit":
                            page.screenshot(
                                path=str(args.output / f"{engine}-order-{name}.png"), full_page=True, caret="initial"
                            )
                    assert not violations, violations
                    page.get_by_role("button", name="External dummy resource").click()
                    try:
                        page.wait_for_url(f"http://localhost:{server.server_port}/external", timeout=5000)
                    except Exception:
                        print(
                            "redirect diagnostic",
                            engine,
                            page.url.split("#")[0],
                            ["resource" if b"resource=" in x else "read" for x in calls],
                            violations,
                            flush=True,
                        )
                        raise
                    assert b"resource=0.0" in calls[-1] and b"token=" in calls[-1]
                    before = len(calls)
                    page.goto(origin + "/order-access")
                    page.wait_for_selector("[data-cr-state=invalid]")
                    assert len(calls) == before
                    unavailable[0] = True
                    page.goto(origin + "/order-access#token=" + "a" * 43)
                    page.reload()
                    page.wait_for_selector("[data-cr-state=unavailable]")
                    assert page.locator("#content").get_attribute("aria-busy") == "false"
                    unavailable[0] = False
                    page.get_by_role("button", name="Try again").click()
                    page.wait_for_selector("[data-cr-state=ready]")
                    empty[0] = True
                    page.reload()
                    page.wait_for_selector("[data-cr-state=ready]")
                    assert "No purchased resources" in page.locator("#content").inner_text()
                    assert page.locator(".cr-resource-form").count() == 0
                    empty[0] = False
                    print(
                        engine + ": branded CSP, responsive controls, external POST redirect, "
                        "invalid/unavailable passed",
                        flush=True,
                    )
                finally:
                    browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
