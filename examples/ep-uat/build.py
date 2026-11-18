# ruff: noqa: E501

#!/usr/bin/env python3
"""Build a local, browse-only EP UAT using CartRay's existing GEL theme assets.

Prices are source display data, never checkout authority. No emails or payments are sent.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import importlib.util
import json
import re
import shutil
import sys
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "examples/gel-theme-pack"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(PACK))
from src.gel import tokens, validate  # noqa: E402
from src.templates import header as theme_header  # noqa: E402

from cartray.merchant_contract import merchant_path, page_seo, register_path, seo_head  # noqa: E402


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
        super().__init__(convert_charrefs=False)
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

    def handle_comment(self, data):
        raise ValueError("source markup comments are not allowed")

    def close(self):
        # Python versions differ in how close() flushes an incomplete tag.
        if "<" in self.rawdata:
            raise ValueError("incomplete source markup is not allowed")
        super().close()

    def handle_data(self, data):
        if "<" in data:
            raise ValueError("incomplete source markup is not allowed")


def esc(s):
    return html.escape(str(s or ""), quote=True)


def shell(gel, title, body, test_products=(), theme="minimal", review_enabled=False, *, seo=None, catalogue_path="/shop/"):
    name = esc(gel["identity"]["name"])
    test_data = json.dumps(list(test_products), ensure_ascii=True).replace("<", "\\u003c")
    locale = esc(gel["identity"]["locale"])
    header = theme_header(gel, theme, "/", False, nav_links=((catalogue_path, "Catalogue"),), header_search=False, cart_count=False)
    footer = "Selected products use Stripe test mode only. Review invitations are disabled." if test_products else "Browsing qualification only. Purchases and review invitations are disabled."
    connected = ('<script src="/storefront-config.js" defer></script><script src="/site-config.js" defer></script>'
                 '<script type="module" src="/theme/connected-browse.mjs"></script>') if test_products else (
                     '<script src="/storefront-config.js" defer></script>' if review_enabled else "")
    presenter = ' data-cartray-presenter="gel"' if test_products else ""
    metadata = seo_head(seo, force_noindex=True) if seo is not None else f'<meta name="robots" content="noindex,nofollow"><title>{esc(title)} · {name}</title>'
    test_script = f'<script type="application/json" id="uat-test-products">{test_data}</script>' if seo is None else ""
    footer_html = f'<footer class="site-footer"><div class="wrap"><p>{name} · UAT catalogue</p><p>{footer}</p></div></footer>' if seo is None else ""
    parity = ' data-offline-parity="true"' if seo is not None else ""
    script = "catalogue.mjs" if seo is not None else "uat.mjs"
    return f"""<!doctype html><html lang="{locale}"><head>{connected}<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{metadata}<meta name="referrer" content="no-referrer"><link rel="stylesheet" href="/theme/tokens.css"><link rel="stylesheet" href="/theme/base.css"><link rel="stylesheet" href="/theme/theme.css"><link rel="stylesheet" href="/theme/uat.css"><script type="module" src="/theme/{script}"></script></head><body{presenter}{parity}>{header}<main id="main">{body}</main>{test_script}{footer_html}</body></html>"""


def path(p):
    # Audit navigation is explicitly supplied and never treated as canonical.
    result = p.get("navigation_path")
    try:
        merchant_path(result, product=True)
    except ValueError as error:
        raise ValueError("invalid product navigation path") from error
    return result


def price(raw, test_enabled=False, currency="AUD", *, offline_parity=False):
    if raw is None or raw == "":
        return "Price unavailable"
    if offline_parity:
        return esc(raw) + " " + esc(currency)
    suffix = " (Stripe test mode only)" if test_enabled else " (not checkout-authorised)"
    return "Source price: " + esc(raw) + " " + esc(currency) + suffix


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


def product_card(p, test_ids=(), currency="AUD", *, offline_parity=False):
    categories = " ".join(str(c["id"]) for c in p["categories"])
    search = " ".join(
        [p["title"], p.get("source_sku") or "", p["summary"], p["description"], *(c["name"] for c in p["categories"])]
    )
    search_lower = search.lower()
    search_folded = search.casefold()
    if search_folded != search_lower:
        search_lower += " " + search_folded
    visibility = (
        '<p class="muted">UAT audit: restricted visibility in source catalogue.</p>'
        if not offline_parity and set(p["source_visibility"]) & {"exclude-from-catalog", "exclude-from-search"} else ""
    )
    add = f'<button type="button" data-uat-add="{p["source_id"]}" disabled>Add to test cart</button>' if p["source_id"] in test_ids else ""
    return f'''<article class="product-card" data-category-ids="{esc(categories)}" data-search="{esc(search_lower)}" data-order="{p["menu_order"]}" data-name="{esc(p["title"].casefold())}" data-price="{esc(p["source_price"])}"><a class="product-art" href="{path(p)}" tabindex="-1" aria-hidden="true">{image(p)}</a><div class="product-copy"><h2><a href="{path(p)}">{esc(p["title"])}</a></h2><p>{esc(p["summary"][:400])}</p>{visibility}<p class="muted">{price(p["source_price"], p["source_id"] in test_ids, currency, offline_parity=offline_parity)}</p><a href="{path(p)}">View product</a>{add}</div></article>'''


def catalogue(products, merchant_name, test_ids=(), currency="AUD", *, offline_parity=False):
    cats = {c["id"]: c["name"] for p in products for c in p["categories"]}
    options = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in sorted(cats.items(), key=lambda i: i[1]))
    checkout_note = "only selected products support test checkout" if test_ids else "product checkout is not enabled"
    notice = "" if offline_parity else f'<p>UAT audit snapshot includes products hidden in the source catalogue; {checkout_note}.</p>'
    return f"""<section class="collection wrap"><h1>{esc(merchant_name)} catalogue</h1>{notice}<div class="catalogue-tools"><label>Search <input id="search" type="search" autocomplete="off"></label><label>Category <select id="category"><option value="">All categories</option>{options}</select></label><label>Sort <select id="sort"><option value="featured">Source order</option><option value="name">Name</option><option value="low">Source price: low to high</option></select></label></div><p id="results-status" role="status" aria-live="polite">{len(products)} products</p><p id="uat-cart-message" role="status"></p><div id="products" class="product-grid">{"".join(product_card(p, test_ids, currency, offline_parity=offline_parity) for p in products)}</div></section>"""


def detail(p, test_ids=(), review_subject=None, currency="AUD", *, offline_parity=False, catalogue_path="/shop/"):
    if p["images"]:
        first = p["images"][0]
        gallery = f'''<figure class="detail-art" data-product-gallery aria-label="Product images"><div class="gallery-stage"><img data-gallery-image src="/{esc(first["path"])}" alt="{esc(first["alt"])}" width="800" height="720"><p data-gallery-fallback hidden>Image unavailable</p></div><div data-gallery-controls hidden></div><figcaption class="sr-only" data-gallery-status role="status" aria-live="polite"></figcaption></figure>'''
    else:
        gallery = '<div class="detail-art uat-no-image">No source image available</div>'
    choices = ""
    if p["variations"]:
        items = "".join(
            f'<option value="{esc(v["key"])}">{esc(", ".join(k.removeprefix("attribute_").replace("_", " ").title() + ": " + str(value) for k, value in v["attributes"].items()))} · {price(v["source_price"], currency=currency, offline_parity=offline_parity)}</option>'
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
    if offline_parity:
        action = '<div data-cartray-purchase-action></div>'
    description = p["description_html"]
    payload = json.dumps(
        {
            "title": p["title"],
            "image_url": "/" + p["images"][0]["path"] if p["images"] else "",
            "images": [{"url": "/" + i["path"], "alt": i["alt"]} for i in p["images"]],
            "variations": p["variations"],
            "default_attributes": p["default_attributes"],
            "review_subject": review_subject,
            "currency": currency,
        },
        ensure_ascii=True,
    ).replace("<", "\\u003c")
    reviews = ('<section class="wrap" id="reviews" hidden><h2>Reviews</h2><p id="reviews-status" role="status"></p>'
               '<div id="review-list"></div><button id="reviews-more" type="button" hidden>Load more reviews</button></section>') if review_subject else ""
    reader = '<script type="module" src="/theme/connected-public-reviews.mjs"></script>' if review_subject else ""
    return f"""<div class="wrap breadcrumb"><a href="{esc(catalogue_path)}">Catalogue</a> / {esc(p["title"])}</div><section class="product-detail wrap">{gallery}<div class="detail-copy"><h1>{esc(p["title"])}</h1><p id="variant-price">{price(p["source_price"], p["source_id"] in test_ids, currency, offline_parity=offline_parity)}</p><p>{esc(p["summary"])}</p>{choices}{action}<div class="uat-description">{description}</div></div></section>{reviews}<script type="application/json" id="product-data">{payload}</script>{reader}"""


def build(source, output, theme, test_fixture=None, review_fixture=None, gel_file=None, *, offline_seo=None):
    source, output = Path(source), Path(output)
    if output.exists():
        raise ValueError("output already exists")
    if offline_seo is not None and (test_fixture is not None or review_fixture is not None):
        raise ValueError("offline parity cannot use test checkout or review fixtures")
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
        if data.get("source_currency") != "AUD":
            raise ValueError("this Stripe test checkout fixture requires an AUD catalogue")
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
            product_key = item.get("cartray_product_key")
            if product_key != f"EP-UAT-{item['source_id']}" or not product.get("images"):
                raise ValueError("invalid authorised offer identity or presentation")
            test_products.append({"source_id": item["source_id"], "product_key": product_key,
                                  "title": product["title"], "amount_minor": item["amount_minor"],
                                  "path": path(product), "image": "/" + product["images"][0]["path"],
                                  "image_alt": product["images"][0]["alt"],
                                  "category": product["categories"][0]["name"] if product["categories"] else "Products"})
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
    ids, navigation_paths, prefixes = set(), set(), {}
    for product in data["products"]:
        source_id = product.get("source_id")
        if not ((type(source_id) is int and source_id > 0) or
                (isinstance(source_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", source_id))) or str(source_id) in ids:
            raise ValueError("invalid product identity")
        ids.add(str(source_id))
        navigation = path(product)
        if navigation.casefold() in navigation_paths:
            raise ValueError("duplicate product navigation path")
        navigation_paths.add(navigation.casefold())
        register_path(navigation, prefixes)
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
        validator.close()
    seo_pages = None
    catalogue_path = "/shop/"
    if offline_seo is not None:
        config = json.loads(Path(offline_seo).read_text())
        if not isinstance(config, dict) or set(config) != {"schema_version", "catalogue_path", "pages"} or type(config["schema_version"]) is not int or config["schema_version"] != 1:
            raise ValueError("invalid offline SEO contract")
        catalogue_path = merchant_path(config["catalogue_path"], product=config["catalogue_path"] != "/shop/")
        seo_pages = config["pages"]
        expected = {path(p) for p in data["products"]} | {catalogue_path}
        if catalogue_path.casefold() in navigation_paths or not isinstance(seo_pages, dict) or set(seo_pages) != expected:
            raise ValueError("offline SEO must cover each explicit product and catalogue route")
        register_path(catalogue_path, prefixes)
        origins = set()
        for route, seo in seo_pages.items():
            structured = page_seo(seo)
            url = urlsplit(seo["canonical_url"])
            if url.path != route or (route == catalogue_path and structured):
                raise ValueError("offline SEO canonical or structured data differs from route")
            origins.add((url.scheme, url.hostname, url.port or 443))
        if len(origins) != 1:
            raise ValueError("offline SEO canonical origins must match")
    gel = json.loads(Path(gel_file or Path(__file__).with_name("gel.json")).read_text())
    if gel.get("identity", {}).get("name") != data["merchant_name"] or gel["identity"].get("currency") != data["source_currency"]:
        raise ValueError("GEL merchant identity or currency does not match the UAT catalogue")
    if gel.get("assets") != {"logo": "assets/merchant-logo.jpg", "logo_inverse": "assets/merchant-logo.jpg", "favicon": "assets/merchant-logo.jpg"}:
        raise ValueError("UAT GEL assets must use the approved source logo")
    with TemporaryDirectory(prefix="ep-uat-gel-") as tmp:
        root = Path(tmp)
        (root / "assets").mkdir()
        shutil.copyfile(source / data["brand_logo"], root / "assets/merchant-logo.jpg")
        gel_validation = validate(gel, root)
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
    shutil.copyfile(Path(__file__).parent / "catalogue.mjs", output / "theme/catalogue.mjs")
    if seo_pages is None:
        shutil.copyfile(Path(__file__).parent / "uat.mjs", output / "theme/uat.mjs")
    shutil.copyfile(ROOT / "modules/reviews/ui/public.mjs", output / "theme/reviews.mjs")
    if review_subjects:
        shutil.copyfile(PACK / "src/connected-public-reviews.mjs", output / "theme/connected-public-reviews.mjs")
        if not test_products:
            (output / "storefront-config.js").write_text('window.CARTRAY_STOREFRONT = {"reviewsEnabled":true};\n')
        shutil.copyfile(PACK / "src/connected-review.mjs", output / "theme/review-presenter.mjs")
        shutil.copyfile(PACK / "src/native-presenter.mjs", output / "theme/native-presenter.mjs")
        for filename, dest in (("reviews.js", "native-reviews.js"), ("manage.js", "native-manage.js")):
            shutil.copyfile(ROOT / "modules/reviews/ui" / filename, output / "theme" / dest)
    logo_source = source / data["brand_logo"]
    if not logo_source.is_file():
        raise ValueError("source logo unavailable")
    shutil.copyfile(logo_source, output / "assets/merchant-logo.jpg")
    products = data["products"]
    standard_pages = [
        ("index.html", catalogue(products, data["merchant_name"], test_ids, data["source_currency"])),
        ("shop/index.html", catalogue(products, data["merchant_name"], test_ids, data["source_currency"])),
        (
            "cart/index.html",
            '<section class="wrap"><h1>Test cart</h1><div id="uat-cart"><p>Checkout is not available.</p></div></section>' if test_ids else '<section class="wrap"><h1>Cart</h1><p>Checkout is not yet enabled for this UAT catalogue.</p></section>',
        ),
        ("uat-return/index.html", '<section class="wrap"><h1>Test order</h1><div id="uat-return"><p>Checking the test order…</p></div></section>'),
    ]
    if seo_pages is not None:
        standard_pages = [(catalogue_path.lstrip("/") + "index.html",
                           catalogue(products, data["merchant_name"], currency=data["source_currency"], offline_parity=True))]
    for route, content in standard_pages:
        file = output / route
        file.parent.mkdir(parents=True, exist_ok=True)
        seo = seo_pages[catalogue_path] if seo_pages is not None else None
        file.write_text(shell(gel, "Catalogue", content, test_products, theme, bool(review_subjects),
                              seo=seo, catalogue_path=catalogue_path))
    if review_subjects:
        for route, kind, heading in (("review/index.html", "review", "Share your experience"),
                                     ("review/manage.html", "manage", "Manage your review")):
            body = (f'<section class="wrap" data-native-review="{kind}"><h1>{heading}</h1>'
                    '<p id="message" role="status" aria-live="polite"></p><div id="content"></div></section>'
                    '<dialog id="confirm-dialog" aria-labelledby="dialog-title"><h2 id="dialog-title">Withdraw this review?</h2>'
                    '<p>Your review will no longer be displayed.</p><button type="button" data-dialog-cancel>Keep my review</button>'
                    '<button type="button" data-dialog-confirm>Withdraw review</button></dialog>'
                    '<script type="module" src="/theme/review-presenter.mjs"></script>')
            file = output / route
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(shell(gel, heading, body, test_products, theme, bool(review_subjects)))
    for p in products:
        file = output / path(p).lstrip("/") / "index.html"
        file.parent.mkdir(parents=True, exist_ok=True)
        seo = seo_pages[path(p)] if seo_pages is not None else None
        content = detail(p, test_ids, review_subjects.get(str(p["source_id"])), data["source_currency"],
                         offline_parity=seo_pages is not None, catalogue_path=catalogue_path)
        file.write_text(shell(gel, p["title"], content, test_products, theme, bool(review_subjects),
                              seo=seo, catalogue_path=catalogue_path))
    if test_products:
        if "cartray_gel" not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                "cartray_gel", PACK / "__init__.py", submodule_search_locations=[str(PACK)]
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        shutil.copyfile(Path(__file__).parent / "return-grants.mjs", output / "theme/return-grants.mjs")
        importlib.import_module("cartray_gel.connected_flows").install(
            output, ROOT, gel, theme, test_products, context_path="/__uat/context",
            return_extension="/theme/return-grants.mjs", reviews_enabled=bool(review_subjects),
        )
    (output / "provenance.json").write_text(
        json.dumps(
            {"source_archive_sha256": data["source_archive_sha256"], "theme": theme,
             "gel_sha256": gel_validation["manifest_sha256"], "counts": data["reconciliation"],
             "output_mode": "offline-parity-noindex" if seo_pages is not None else "uat-noindex"},
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
    parser.add_argument("--gel", type=Path, help="merchant-specific GEL manifest (defaults to EP UAT)")
    parser.add_argument("--offline-seo", type=Path, help="explicit offline-only SEO metadata contract")
    args = parser.parse_args()
    build(args.source, args.output, args.theme, args.test_fixture, args.review_fixture, args.gel,
          offline_seo=args.offline_seo)
