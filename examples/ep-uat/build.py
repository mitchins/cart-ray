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
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "examples/gel-theme-pack"
sys.path.insert(0, str(PACK))
from src.gel import tokens  # noqa: E402


def esc(s):
    return html.escape(str(s or ""), quote=True)


def shell(gel, title, body):
    name = esc(gel["identity"]["name"])
    return f"""<!doctype html><html lang="en-AU"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{esc(title)} · {name}</title><link rel="stylesheet" href="/theme/tokens.css"><link rel="stylesheet" href="/theme/base.css"><link rel="stylesheet" href="/theme/theme.css"><link rel="stylesheet" href="/theme/uat.css"><script type="module" src="/theme/uat.mjs"></script></head><body><a class="skip-link" href="#main">Skip to content</a><header class="site-header"><div class="masthead wrap"><a class="brand" href="/"><img src="/assets/ep-logo.jpg" alt=""><span>{name}</span></a><nav class="desktop-nav" aria-label="Main navigation"><a href="/shop/">Catalogue</a><a href="/cart/">Cart</a></nav><a class="header-cart" href="/cart/">Cart</a></div></header><main id="main">{body}</main><footer class="site-footer"><div class="wrap"><p>{name} · UAT catalogue</p><p>Browsing qualification only. Purchases and review invitations are disabled.</p></div></footer></body></html>"""


def path(p):
    # UAT navigation uses the stable source identity; it is not a canonical URL.
    return f"/products/woo-{p['source_id']}/"


def price(raw):
    if raw is None or raw == "":
        return "Price unavailable"
    return "Source price: " + esc(raw) + " AUD (not checkout-authorised)"


def image(p):
    if not p["images"]:
        return '<div class="uat-no-image">No source image available</div>'
    return f'<img src="/{esc(p["images"][0]["path"])}" alt="" loading="lazy" width="800" height="720">'


def product_card(p):
    categories = " ".join(str(c["id"]) for c in p["categories"])
    search = " ".join(
        [p["title"], p.get("source_sku") or "", p["summary"], p["description"], *(c["name"] for c in p["categories"])]
    )
    return f'''<article class="product-card" data-category-ids="{esc(categories)}" data-search="{esc(search.casefold())}" data-order="{p["menu_order"]}" data-name="{esc(p["title"].casefold())}" data-price="{esc(p["source_price"])}"><a class="product-art" href="{path(p)}" tabindex="-1" aria-hidden="true">{image(p)}</a><div class="product-copy"><h2><a href="{path(p)}">{esc(p["title"])}</a></h2><p>{esc(p["summary"][:400])}</p><p class="muted">{price(p["source_price"])}</p><a href="{path(p)}">View product</a></div></article>'''


def catalogue(products, merchant_name):
    cats = {c["id"]: c["name"] for p in products for c in p["categories"]}
    options = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in sorted(cats.items(), key=lambda i: i[1]))
    return f"""<section class="collection wrap"><h1>{esc(merchant_name)} catalogue</h1><p>UAT browsing snapshot · product checkout is not enabled.</p><div class="catalogue-tools"><label>Search <input id="search" type="search" autocomplete="off"></label><label>Category <select id="category"><option value="">All categories</option>{options}</select></label><label>Sort <select id="sort"><option value="featured">Source order</option><option value="name">Name</option><option value="low">Source price: low to high</option></select></label></div><p id="results-status" role="status" aria-live="polite">{len(products)} products</p><div id="products" class="product-grid">{"".join(product_card(p) for p in products)}</div></section>"""


def detail(p):
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
        choices = f'<label for="variant">Choose an option</label><select id="variant" data-variant><option value="">Select an option</option>{items}</select><p id="variant-status" role="status"></p>'
    action = '<p class="muted">Checkout is not available for this UAT product.</p>'
    if p["action"] == "free_download":
        action = f'<a class="button" href="/{esc(p["free_download"])}" download>Download free catalogue</a>'
    elif p["action"] == "external":
        url = p.get("external_url") or ""
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not any(ch.isspace() for ch in url)
        ):
            action = f'<a class="button" href="{esc(url)}" rel="noopener noreferrer">{esc(p.get("external_label") or "Visit external site")}</a><p class="muted">External live site; destination has not been tested in UAT.</p>'
        else:
            action = '<p class="muted">External destination unavailable for UAT.</p>'
    description = "".join(f"<p>{esc(line)}</p>" for line in p["description"].splitlines())
    payload = json.dumps(
        {
            "title": p["title"],
            "image_url": "/" + p["images"][0]["path"] if p["images"] else "",
            "images": [{"url": "/" + i["path"], "alt": i["alt"]} for i in p["images"]],
            "variations": p["variations"],
            "default_attributes": p["default_attributes"],
        },
        ensure_ascii=True,
    ).replace("<", "\\u003c")
    return f"""<div class="wrap breadcrumb"><a href="/shop/">Catalogue</a> / {esc(p["title"])}</div><section class="product-detail wrap">{gallery}<div class="detail-copy"><h1>{esc(p["title"])}</h1><p>{price(p["source_price"])}</p><p>{esc(p["summary"])}</p>{choices}{action}<div class="uat-description">{description}</div></div></section><script type="application/json" id="product-data">{payload}</script>"""


def build(source, output, theme):
    source, output = Path(source), Path(output)
    if output.exists():
        raise ValueError("output already exists")
    data = json.loads((source / "catalogue.json").read_text())
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
        for media_path in [*(image["path"] for image in product["images"]), product["free_download"]]:
            if media_path is not None and media_path not in declared:
                raise ValueError("product media is not declared")
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
    logo_source = source / data["brand_logo"]
    if not logo_source.is_file():
        raise ValueError("source logo unavailable")
    shutil.copyfile(logo_source, output / "assets/ep-logo.jpg")
    products = data["products"]
    for route, content in [
        ("index.html", catalogue(products, data["merchant_name"])),
        ("shop/index.html", catalogue(products, data["merchant_name"])),
        (
            "cart/index.html",
            '<section class="wrap"><h1>Cart</h1><p>Checkout is not yet enabled for this UAT catalogue.</p></section>',
        ),
    ]:
        file = output / route
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(shell(gel, "Catalogue", content))
    for p in products:
        file = output / path(p).lstrip("/") / "index.html"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(shell(gel, p["title"], detail(p)))
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
    args = parser.parse_args()
    build(args.source, args.output, args.theme)
