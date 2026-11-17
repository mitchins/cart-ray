#!/usr/bin/env python3
"""Exercise the EP UAT review UI against a synthetic acquisition and local Reviews Worker."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/reviews/tests"))
import native_acquisition_workerd as native  # noqa: E402
import reviews_qualification as proxy  # noqa: E402


class BrowserQualificationError(Exception):
    """A sanitized stage marker for the synthetic browser qualification."""


def exercise(site, output, worker_url, token, store_name, product_name):
    """The capability is synthetic, stays in memory and is never printed."""
    proxy.worker.PORT = urlsplit(worker_url).port
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(proxy.Handler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    stage = "browser launch"
    try:
        stage = "direct and proxied invitation"
        status, direct = native.call("/reviews/v1/invitation", {"invitation_token": token})
        if status != 200 or len(direct.get("subjects", [])) != 2:
            raise ValueError("unexpected direct invitation result")
        request = urllib.request.Request(
            base + "/reviews/v1/invitation", data=json.dumps({"invitation_token": token}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            forwarded = json.load(response)
        if forwarded != direct:
            raise ValueError("proxied invitation differs from Worker")
        stage = "browser launch"
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            stage = "invitation navigation"
            page.goto(base + "/review/#invitation=" + token, wait_until="load")
            stage = "product selection"
            page.get_by_text("Your invitation from " + store_name).wait_for()
            page.get_by_role("button", name=product_name, exact=True).click()
            page.get_by_role("heading", name=product_name).wait_for()
            stage = "rating entry"
            form = page.locator("#content form:visible")
            form.get_by_role("radio", name="2 stars").check()
            form.locator('textarea[name="body"]').fill("EP UAT synthetic review")
            stage = "review submission"
            form.get_by_role("button", name="Submit review").click()
            page.get_by_text("Thank you. Your review", exact=False).first.wait_for()
            link = page.get_by_role("link", name="Correct or withdraw this review")
            link.wait_for()
            manage_url = urljoin(base + "/review/", link.get_attribute("href"))
            if (urlsplit(manage_url).netloc != urlsplit(base).netloc
                    or urlsplit(manage_url).path != "/review/manage.html"):
                raise ValueError("management link must stay on the local UAT site")
            pending = native.call("/reviews-admin/v1/pending", admin=True)[1]["pending"]
            item = next(x for x in pending if x["body"] == "EP UAT synthetic review")
            stage = "approval and public display"
            action = "/reviews-admin/v1/contributions/" + item["review_id"] + "/approve"
            assert native.call(action, {"revision_id": item["revision_id"]}, admin=True)[0] == 200
            page.goto(base + "/products/woo-171/", wait_until="load")
            page.get_by_text("EP UAT synthetic review").wait_for()
            page.screenshot(path=str(output / "native-review-published.png"))
            page.goto(manage_url, wait_until="load")
            stage = "review correction"
            page.get_by_text("Changes are checked before they appear publicly.", exact=False).wait_for()
            page.locator('textarea[name="body"]').fill("EP UAT corrected review")
            page.get_by_role("button", name="Save changes").click()
            page.get_by_text("Your changes were submitted and are awaiting moderation.").wait_for()
            pending = native.call("/reviews-admin/v1/pending", admin=True)[1]["pending"]
            item = next(x for x in pending if x["body"] == "EP UAT corrected review")
            assert native.call(action, {"revision_id": item["revision_id"]}, admin=True)[0] == 200
            page.reload(wait_until="load")
            stage = "review withdrawal"
            page.get_by_role("button", name="Withdraw review", exact=True).click()
            page.locator("[data-dialog-confirm]").click()
            page.get_by_text("Your review has been withdrawn.").wait_for()
            public = native.call("/reviews/v1/subjects/example-policy/contributions")[1]
            assert all(x.get("body") not in ("EP UAT synthetic review", "EP UAT corrected review")
                       for x in public["contributions"])
            browser.close()
        (output / "review-customer-result.json").write_text(json.dumps({
            "purchase": "synthetic confirmed acquisition",
            "runtime": "local Workerd and D1",
            "checks": ["invitation", "pending", "moderation", "public display", "edit", "withdrawal"],
            "production": False,
        }, indent=2) + "\n")
        print("EP UAT browser: synthetic purchase invitation, pending, approval, edit and withdrawal passed.")
    except Exception as error:
        raise BrowserQualificationError(f"{stage}: {type(error).__name__}") from None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        source = json.loads((args.source / "catalogue.json").read_text())
        provenance = json.loads((args.site / "provenance.json").read_text())
        if source["source_archive_sha256"] != provenance["source_archive_sha256"]:
            raise ValueError("site and source snapshot mismatch")
        products = [p for p in source["products"] if p["source_id"] == 171]
        if len(products) != 1:
            raise ValueError("synthetic review product is unavailable")
        store_name, product_name = source["merchant_name"], products[0]["title"]
        native.main(lambda base, token: exercise(args.site, args.output, base, token, store_name, product_name),
                    public_store_name=store_name,
                    public_names={"example-policy": product_name, "example-bundle": "Synthetic bundle"})
    except Exception as error:
        # Browser errors can contain fragment capabilities; do not print their text.
        detail = str(error) if isinstance(error, BrowserQualificationError) else type(error).__name__
        print(f"Synthetic review browser qualification failed: {detail}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
