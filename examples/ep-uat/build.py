# ruff: noqa: E501

#!/usr/bin/env python3
"""Build a local, browse-only EP UAT using CartRay's existing GEL theme assets.

Prices are source display data, never checkout authority. No emails or payments are sent.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "examples/gel-theme-pack"
sys.path.insert(0, str(PACK))
from src.gel import tokens  # noqa: E402


class SourceMarkupCheck(HTMLParser):
    """Reject active or undeclared markup in the intermediate catalogue."""

    allowed = {
        "p",
        "br",
        "div",
        "span",
        "strong",
        "b",
        "em",
        "i",
        "ul",
        "ol",
        "li",
        "h2",
        "h3",
        "h4",
        "h5",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "blockquote",
        "img",
    }

    def __init__(self, declared):
        super().__init__(convert_charrefs=True)
        self.declared = declared

    def handle_starttag(self, tag, attrs):
        if tag not in self.allowed:
            raise ValueError("unsupported source markup")
        values = dict(attrs)
        if len(values) != len(attrs):
            raise ValueError("duplicate markup attribute")
        if tag == "img":
            if (
                set(values) != {"src", "alt", "loading"}
                or values["loading"] != "lazy"
                or not values["src"].startswith("/")
                or values["src"][1:] not in self.declared
            ):
                raise ValueError("undeclared source markup image")
        elif tag == "span" and values == {"class": "muted"}:
            pass
        elif values:
            raise ValueError("source markup attributes are not allowed")

    def handle_decl(self, decl):
        raise ValueError("source markup declarations are not allowed")


def esc(s):
    return html.escape(str(s or ""), quote=True)


def shell(gel, title, body, test_products=()):
    name = esc(gel["identity"]["name"])
    test_data = json.dumps(list(test_products), ensure_ascii=True).replace("<", "\\u003c")
    footer = "Selected products use Stripe test mode only. Review invitations are disabled." if test_products else "Browsing qualification only. Purchases and review invitations are disabled."
    return f"""<!doctype html><html lang="en-AU"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{esc(title)} · {name}</title><link rel="stylesheet" href="/theme/tokens.css"><link rel="stylesheet" href="/theme/base.css"><link rel="stylesheet" href="/theme/theme.css"><link rel="stylesheet" href="/theme/uat.css"><script type="module" src="/theme/uat.mjs"></script></head><body><a class="skip-link" href="#main">Skip to content</a><header class="site-header"><div class="masthead wrap"><a class="brand" href="/"><img src="/assets/ep-logo.jpg" alt=""><span>{name}</span></a><nav class="desktop-nav" aria-label="Main navigation"><a href="/shop/">Catalogue</a><a href="/cart/">Cart</a></nav><a class="header-cart" href="/cart/">Cart</a></div></header><main id="main">{body}</main><script type="application/json" id="uat-test-products">{test_data}</script><footer class="site-footer"><div class="wrap"><p>{name} · UAT catalogue</p><p>{footer}</p></div></footer></body></html>"""


def path(p):
    # UAT navigation uses the stable source identity; it is not a canonical URL.
    return f"/products/woo-{p['source_id']}/"


def price(raw, test_enabled=False):
    if raw is None or raw == "":
        return "Price unavailable"
    suffix = " (Stripe test mode only)" if test_enabled else " (not checkout-authorised)"
    return "Source price: " + esc(raw) + " AUD" + suffix


def image(p):
    if not p["images"]:
        return '<div class="uat-no-image">No source image available</div>'
    return f'<img src="/{esc(p["images"][0]["path"])}" alt="" loading="lazy" width="800" height="720">'


def safe_external(url):
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not any(ch.isspace() for ch in url)
        )
    except (ValueError, TypeError):
        return False


def product_card(p, test_ids=()):
    categories = " ".join(str(c["id"]) for c in p["categories"])
    search = " ".join(
        [p["title"], p.get("source_sku") or "", p["summary"], p["description"], *(c["name"] for c in p["categories"])]
    )
    visibility = (
        '<p class="muted">UAT audit: restricted visibility in source catalogue.</p>' if p["source_visibility"] else ""
    )
    add = f'<button type="button" data-uat-add="{p["source_id"]}" disabled>Add to test cart</button>' if p["source_id"] in test_ids else ""
    return f'''<article class="product-card" data-category-ids="{esc(categories)}" data-search="{esc(search.casefold())}" data-order="{p["menu_order"]}" data-name="{esc(p["title"].casefold())}" data-price="{esc(p["source_price"])}"><a class="product-art" href="{path(p)}" tabindex="-1" aria-hidden="true">{image(p)}</a><div class="product-copy"><h2><a href="{path(p)}">{esc(p["title"])}</a></h2><p>{esc(p["summary"][:400])}</p>{visibility}<p class="muted">{price(p["source_price"], p["source_id"] in test_ids)}</p><a href="{path(p)}">View product</a>{add}</div></article>'''


def catalogue(products, merchant_name, test_ids=()):
    cats = {c["id"]: c["name"] for p in products for c in p["categories"]}
    options = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in sorted(cats.items(), key=lambda i: i[1]))
    checkout_note = "only selected products support test checkout" if test_ids else "product checkout is not enabled"
    return f"""<section class="collection wrap"><h1>{esc(merchant_name)} catalogue</h1><p>UAT audit snapshot includes products hidden in the source catalogue; {checkout_note}.</p><div class="catalogue-tools"><label>Search <input id="search" type="search" autocomplete="off"></label><label>Category <select id="category"><option value="">All categories</option>{options}</select></label><label>Sort <select id="sort"><option value="featured">Source order</option><option value="name">Name</option><option value="low">Source price: low to high</option></select></label></div><p id="results-status" role="status" aria-live="polite">{len(products)} products</p><p id="uat-cart-message" role="status"></p><div id="products" class="product-grid">{"".join(product_card(p, test_ids) for p in products)}</div></section>"""


def detail(p, test_ids=(), review_subject=None):
    if p["images"]:
        first = p["images"][0]
        gallery = f'''<figure class="detail-art" data-product-gallery aria-label="Product images"><div class="gallery-stage"><img data-gallery-image src="/{esc(first["path"])}" alt="{esc(first["alt"])}" width="800" height="720"><p data-gallery-fallback hidden>Image unavailable</p></div><div data-gallery-controls hidden></div><figcaption class="sr-only" data-gallery-status role="status" aria-live="polite"></figcaption></figure>'''
    else:
        gallery = '<div class="detail-art uat-no-image">No source image available</div>'
    choices = ""
    if p["variations"]:
        items = "".join(
            f'<option value="{esc(v["key"])}">{esc(", ".join(k.removeprefix("attribute_").replace("_", " ").title() + ": " + str(value) for k, value in v["attributes"].items()))} · {price(v["source_price"])}</option>'
            for v in p["variations"]
        )
        choices = f'<label for="variant">Choose an option</label><select id="variant" data-variant><option value="">Select an option</option>{items}</select><p id="variant-status" role="status"></p><p id="variant-description"></p>'
    action = '<p class="muted">Checkout is not available for this UAT product.</p>'
    if p["source_id"] in test_ids:
        action = f'<button type="button" data-uat-add="{p["source_id"]}" disabled>Add to test cart</button><p id="uat-cart-message" role="status"></p><p>Stripe test mode only; dummy fulfilment.</p>'
    if p["action"] == "free_download":
        action = f'<a class="button" href="/{esc(p["free_download"])}" download>Download free catalogue</a>'
    elif p["action"] == "external":
        url = p.get("external_url") or ""
        if safe_external(url):
            action = f'<a class="button" href="{esc(url)}" rel="noopener noreferrer">{esc(p.get("external_label") or "Visit external site")}</a><p class="muted">External live site; destination has not been tested in UAT.</p>'
        else:
            action = '<p class="muted">External destination unavailable for UAT.</p>'
    description = p["description_html"]
    payload = json.dumps(
        {
            "title": p["title"],
            "image_url": "/" + p["images"][0]["path"] if p["images"] else "",
            "images": [{"url": "/" + i["path"], "alt": i["alt"]} for i in p["images"]],
            "variations": p["variations"],
            "default_attributes": p["default_attributes"],
            "review_subject": review_subject,
        },
        ensure_ascii=True,
    ).replace("<", "\\u003c")
    reviews = ('<section class="wrap" id="reviews" hidden><h2>Reviews</h2><p id="reviews-status" role="status"></p>'
               '<div id="review-list"></div><button id="reviews-more" type="button" hidden>Load more reviews</button></section>') if review_subject else ""
    return f"""<div class="wrap breadcrumb"><a href="/shop/">Catalogue</a> / {esc(p["title"])}</div><section class="product-detail wrap">{gallery}<div class="detail-copy"><h1>{esc(p["title"])}</h1><p id="variant-price">{price(p["source_price"], p["source_id"] in test_ids)}</p><p>{esc(p["summary"])}</p>{choices}{action}<div class="uat-description">{description}</div></div></section>{reviews}<script type="application/json" id="product-data">{payload}</script>"""


def build(source, output, theme, test_fixture=None, review_fixture=None):
    source, output = Path(source), Path(output)
    if output.exists():
        raise ValueError("output already exists")
    data = json.loads((source / "catalogue.json").read_text())
    review_subjects = {}
    if review_fixture:
        review_config = json.loads(Path(review_fixture).read_text())
        if (review_config.get("schema_version") != 1
                or review_config.get("source_archive_sha256") != data["source_archive_sha256"]
                or not isinstance(review_config.get("subjects"), dict)):
            raise ValueError("invalid UAT review subject fixture")
        product_ids = {str(p["source_id"]) for p in data["products"]}
        review_subjects = review_config["subjects"]
        if any(key not in product_ids or not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]{1,128}", value)
               for key, value in review_subjects.items()) or len(set(review_subjects.values())) != len(review_subjects):
            raise ValueError("invalid or duplicate UAT review subject")
    test_products = []
    if test_fixture:
        fixture = json.loads(Path(test_fixture).read_text())
        if fixture.get("livemode") is not False or fixture.get("source_archive_sha256") != data["source_archive_sha256"]:
            raise ValueError("test price fixture is not scoped to this catalogue")
        offers = {p["source_id"]: p for p in data["products"]}
        for item in fixture["offers"]:
            product = offers.get(item["source_id"])
            try:
                matches_price = product and Decimal(product["source_price"]) * 100 == item["amount_minor"]
            except (InvalidOperation, TypeError):
                matches_price = False
            if (not matches_price or product["key"] != item["source_key"] or item["currency"] != "aud"
                    or product["source_delivery_kind"] != "uat_dummy_transfer"):
                raise ValueError("test price fixture conflicts with source offer")
            test_products.append({"source_id": item["source_id"], "title": product["title"],
                                  "amount_minor": item["amount_minor"]})
        if len({x["source_id"] for x in test_products}) != len(test_products) or not 1 <= len(test_products) <= 2:
            raise ValueError("invalid test fixture offer list")
    test_ids = {x["source_id"] for x in test_products}
    declared = set()
    for item in data["public_media"]:
        relative = Path(item["path"])
        file = source / relative
        if (
            relative.is_absolute()
            or relative.parts[:1] != ("media",)
            or ".." in relative.parts
            or not file.is_file()
            or not file.resolve().is_relative_to(source.resolve())
        ):
            raise ValueError("unsafe or missing source media")
        if str(relative) in declared:
            raise ValueError("duplicate source media")
        declared.add(str(relative))
        if hashlib.sha256(file.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"source media fingerprint mismatch: {relative}")
    if data["brand_logo"] not in declared:
        raise ValueError("source logo is not declared")
    ids = set()
    for product in data["products"]:
        if type(product["source_id"]) is not int or product["source_id"] < 1 or product["source_id"] in ids:
            raise ValueError("invalid product identity")
        ids.add(product["source_id"])
        if product["checkout_authorized"] or product["canonical_url"] is not None:
            raise ValueError("browse-only build cannot grant checkout or canonical authority")
        if any(variant["checkout_authorized"] for variant in product["variations"]):
            raise ValueError("browse-only build cannot grant variation checkout authority")
        for media_path in [
            *(image["path"] for image in product["images"]),
            *(image["path"] for option in product["variations"] for image in option["images"]),
            product["free_download"],
        ]:
            if media_path is not None and media_path not in declared:
                raise ValueError("product media is not declared")
        validator = SourceMarkupCheck(declared)
        validator.feed(product["description_html"])
    gel = json.loads((PACK / "gel/squirrel.json").read_text())
    gel["identity"]["name"] = gel["identity"]["short_name"] = data["merchant_name"]
    css, _ = tokens(gel, theme)
    output.mkdir(parents=True)
    for name in sorted(declared):
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
    (output / "theme").mkdir()
    (output / "assets").mkdir()
    (output / "theme/tokens.css").write_text(css)
    for name, dest in [("base.css", "base.css"), (theme + ".css", "theme.css"), ("gallery.mjs", "gallery.mjs")]:
        shutil.copyfile(PACK / "src" / name, output / "theme" / dest)
    shutil.copyfile(Path(__file__).parent / "uat.css", output / "theme/uat.css")
    shutil.copyfile(Path(__file__).parent / "uat.mjs", output / "theme/uat.mjs")
    shutil.copyfile(Path(__file__).parent.parent / "acorn-store/reviews.mjs", output / "theme/reviews.mjs")
    logo_source = source / data["brand_logo"]
    if not logo_source.is_file():
        raise ValueError("source logo unavailable")
    shutil.copyfile(logo_source, output / "assets/ep-logo.jpg")
    products = data["products"]
    for route, content in [
        ("index.html", catalogue(products, data["merchant_name"], test_ids)),
        ("shop/index.html", catalogue(products, data["merchant_name"], test_ids)),
        (
            "cart/index.html",
            '<section class="wrap"><h1>Test cart</h1><div id="uat-cart"><p>Checkout is not available.</p></div></section>' if test_ids else '<section class="wrap"><h1>Cart</h1><p>Checkout is not yet enabled for this UAT catalogue.</p></section>',
        ),
        ("uat-return/index.html", '<section class="wrap"><h1>Test order</h1><div id="uat-return"><p>Checking the test order…</p></div></section>'),
    ]:
        file = output / route
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(shell(gel, "Catalogue", content, test_products))
    for p in products:
        file = output / path(p).lstrip("/") / "index.html"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(shell(gel, p["title"], detail(p, test_ids, review_subjects.get(str(p["source_id"]))), test_products))
    (output / "provenance.json").write_text(
        json.dumps(
            {"source_archive_sha256": data["source_archive_sha256"], "theme": theme, "counts": data["reconciliation"]},
            indent=2,
        )
        + "\n"
    )
    print(f"{theme}: {len(products)} products, {data['reconciliation']['variations']} variations")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--theme", choices=["minimal", "studio", "classic"], required=True)
    parser.add_argument("--test-fixture", type=Path)
    parser.add_argument("--review-fixture", type=Path)
    args = parser.parse_args()
    build(args.source, args.output, args.theme, args.test_fixture, args.review_fixture)
