# ruff: noqa: E501
# Static HTML/CSS/JavaScript literals preserve their supplied byte layout.
#!/usr/bin/env python3
"""Compile GEL and a fixed theme into static storefront previews. No network or third-party packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

if __package__:
    from .src import templates as t
    from .src.gel import THEMES, canonical, tokens, validate, validate_asset
else:
    from src.gel import THEMES, canonical, tokens, validate, validate_asset

    from src import templates as t

ROOT = Path(__file__).resolve().parent
VERSION = "1.0.0"


def load_inputs(gel_path, catalogue_path, assets_root=ROOT):
    gel = json.loads(Path(gel_path).read_text(encoding="utf8"))
    report = validate(gel, Path(assets_root))
    catalogue = json.loads(Path(catalogue_path).read_text(encoding="utf8"))
    if set(catalogue) != {"version", "review_invitation_default_checked", "products"}:
        raise ValueError("invalid catalogue fields")
    if not isinstance(catalogue["version"], str) or not catalogue["version"]:
        raise ValueError("catalogue version required")
    if type(catalogue["review_invitation_default_checked"]) is not bool:
        raise ValueError("invitation default must be boolean")
    if not isinstance(catalogue["products"], list) or not catalogue["products"]:
        raise ValueError("catalogue must contain products")
    import re

    keys = set()
    slugs = set()
    for p in catalogue["products"]:
        required = {
            "product_key",
            "slug",
            "title",
            "category",
            "amount_minor",
            "currency",
            "max_quantity",
            "review_subject",
            "image",
            "image_alt",
            "eyebrow",
            "short_description",
            "details",
            "features",
            "specs",
        }
        if not isinstance(p, dict) or set(p) != required:
            raise ValueError("invalid product fields")
        if not re.fullmatch(r"[A-Z0-9-]{1,64}", p["product_key"]) or p["product_key"] in keys:
            raise ValueError("invalid product key")
        if not re.fullmatch(r"[a-z0-9-]{1,80}", p["slug"]) or p["slug"] in slugs:
            raise ValueError("invalid product slug")
        keys.add(p["product_key"])
        slugs.add(p["slug"])
        if type(p["amount_minor"]) is not int or not 0 <= p["amount_minor"] <= 100_000_000:
            raise ValueError("invalid preview amount")
        if type(p["max_quantity"]) is not int or not 1 <= p["max_quantity"] <= 100:
            raise ValueError("invalid maximum quantity")
        if p["currency"] != gel["identity"]["currency"]:
            raise ValueError("mixed currency is unsupported")
        if p["review_subject"] is not None and not re.fullmatch(r"[a-z0-9-]{1,128}", p["review_subject"]):
            raise ValueError("invalid review subject")
        for name in ("title", "category", "image_alt", "eyebrow", "short_description", "details"):
            if not isinstance(p[name], str) or not p[name].strip() or len(p[name]) > 2000:
                raise ValueError(f"invalid product {name}")
        if not isinstance(p["features"], list) or not all(
            isinstance(x, str) and 0 < len(x) < 250 for x in p["features"]
        ):
            raise ValueError("invalid features")
        if not isinstance(p["specs"], dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in p["specs"].items()
        ):
            raise ValueError("invalid specs")
        report["asset_sha256"][p["image"]] = validate_asset(Path(assets_root), p["image"])
    return gel, catalogue, report


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf8", newline="\n")


def branding_outputs(output, gel):
    # A handoff, not an API call or a fabricated account branding configuration.
    write(
        output / "stripe-branding.json",
        json.dumps(
            {
                "purpose": "Operator handoff for Stripe hosted Checkout; no API changes or credentials.",
                "display_name": gel["identity"]["name"],
                "background_color": gel["colours"]["background"],
                "button_color": gel["colours"]["brand"],
                "icon_source": gel["assets"].get("stripe_icon", gel["assets"]["logo"]),
                "logo_source": gel["assets"].get("stripe_logo", gel["assets"]["logo"]),
                "font_preference": gel["typography"]["body"],
                "notes": [
                    "Use Stripe-supported settings for the pinned integration/API version.",
                    "Do not put GEL in payment-authority or settlement logic.",
                    "SVG artwork is supplied as source. Convert to a Stripe-supported upload format, or use the provided PNG assets.",
                    "Do not assume a review capability can be injected into Stripe receipts.",
                ],
            },
            indent=2,
        )
        + "\n",
    )


def build_site(output, gel_path, catalogue_path, theme="minimal", base="/", assets_root=ROOT, preview=True):
    output = Path(output)
    if theme not in THEMES:
        raise ValueError("unknown theme")
    import re

    if not re.fullmatch(r"/(?:[a-z0-9-]+/)*", base):
        raise ValueError("base must be an absolute local directory URL")
    if output.exists():
        raise ValueError(f"output exists: {output}; use a new directory")
    gel, catalogue, report = load_inputs(gel_path, catalogue_path, assets_root)
    output.mkdir(parents=True)
    css, resolved = tokens(gel, theme)
    css += f':root {{ --inverse-logo: url("{base + gel["assets"]["logo_inverse"]}"); }}\n'
    write(output / "theme/tokens.css", css)
    for src, dest in [
        ("base.css", "base.css"),
        (theme + ".css", "theme.css"),
        ("app.mjs", "app.mjs"),
        ("native-presenter.mjs", "native-presenter.mjs"),
    ]:
        shutil.copyfile(ROOT / "src" / src, output / "theme" / dest)
    adapter = "demo-adapter.mjs" if preview else "cartray-adapter.mjs"
    shutil.copyfile(ROOT / "src" / adapter, output / "theme" / adapter)
    paths = set(v for k, v in gel["assets"].items() if k != "hero_alt") | {p["image"] for p in catalogue["products"]}
    for path in sorted(paths):
        dest = output / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(assets_root) / path, dest)
    config = {
        "schemaVersion": 1,
        "preview": preview,
        "base": base,
        "adapter": base + "theme/" + adapter,
        "gel": gel,
        "catalogue": catalogue,
    }
    write(
        output / "site-config.js",
        "window.CARTRAY_THEME = " + json.dumps(config, ensure_ascii=True, sort_keys=True) + ";\n",
    )
    products = catalogue["products"]
    pages = {
        "index.html": (
            "home",
            "Home",
            t.hero(gel, theme, base) + t.collection(gel, products, theme, base) + t.story(gel, base),
        ),
        "shop/index.html": ("shop", "The collection", t.collection(gel, products, theme, base, True)),
        "help/index.html": ("help", "Help & care", t.help_page(gel, base, preview)),
    }
    flow = {
        "cart/index.html": "cart",
        "purchase-return/index.html": "purchase-return",
        "review/index.html": "review",
        "review/manage.html": "manage",
    }
    if preview:
        flow.update(
            {
                "checkout-preview/index.html": "checkout-preview",
                "inbox/index.html": "inbox",
                "unsubscribe/index.html": "unsubscribe",
            }
        )
    for path, page in flow.items():
        pages[path] = (page, page.replace("-", " ").title(), t.simple_page(page, gel, base))
    for path, (page, title, content) in pages.items():
        write(output / path, t.page_shell(gel, theme, base, page, title, content, preview))
    for p in products:
        write(
            output / "products" / p["slug"] / "index.html",
            t.page_shell(
                gel,
                theme,
                base,
                "product",
                p["title"],
                t.product_page(gel, p, base, preview),
                preview,
                p["product_key"],
            ),
        )
    write(
        output / "gel.resolved.json",
        json.dumps(
            {
                "schema_version": "1.0",
                "theme": theme,
                "theme_version": VERSION,
                "manifest": gel,
                "tokens": resolved,
                "validation": report,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )
    branding_outputs(output, gel)
    # Cached capabilities and customer state must never be captured by a service worker.
    write(
        output / "_headers",
        """/*
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  Permissions-Policy: camera=(), microphone=(), geolocation=()
/review/*
  Cache-Control: no-store
/purchase-return/*
  Cache-Control: no-store
/unsubscribe/*
  Cache-Control: no-store
""",
    )
    files = {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob("*"))
        if p.is_file()
    }
    write(
        output / "build-receipt.json",
        json.dumps(
            {
                "pack_version": VERSION,
                "theme": theme,
                "preview": preview,
                "gel_sha256": report["manifest_sha256"],
                "content_sha256": hashlib.sha256(canonical(catalogue)).hexdigest(),
                "files": files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return report


def gallery(output):
    cards = [
        ("minimal", "Minimal", "Quiet, spacious, editorial.", "squirrel"),
        ("studio", "Studio", "Expressive, image-led, bold.", "squirrel"),
        ("classic", "Classic", "Familiar, search-first retail.", "squirrel"),
        ("northline-classic", "Classic / Northline", "Same theme. A different merchant.", "northline"),
    ]
    html = "".join(
        f"""<a class="card {slug}" href="./{slug}/"><div class="mini"><div class="mini-header"><span>{"Squirrel" if merchant == "squirrel" else "NORTHLINE"}</span><span>SHOP &nbsp; CART</span></div><div class="mini-hero"><span>{"Small beginnings.<br>Something wonderful." if merchant == "squirrel" else "Good tools.<br>Less fuss."}</span><img src="./{slug}/assets/{"woodland-scene.svg" if merchant == "squirrel" else "workshop-scene.svg"}" alt=""></div></div><div class="card-copy"><span class="number">0{n + 1}</span><div><h2>{name}</h2><p>{description}</p></div><span aria-hidden="true">↗</span></div></a>"""
        for n, (slug, name, description, merchant) in enumerate(cards)
    )
    write(
        output / "index.html",
        f"""<!doctype html><html lang="en-AU"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>CartRay — Three themes. One identity.</title><link rel="stylesheet" href="gallery.css"></head><body><main><header><a class="wordmark" href="./">CartRay <span>STOREFRONT COLLECTION / 01</span></a><p class="edition">FIRST-PARTY THEMES · GEL 1.0</p><h1>Three ways to feel<br>like <em>your</em> store.</h1><p class="intro">One brand file. Three distinct presentations. Browse, fill a cart and try the review journey — no payments, emails or accounts.</p></header><section class="grid" aria-label="Choose a theme">{html}</section><section class="notes"><div><h2>A brand, not a page builder.</h2><p>Colour roles, typography, logos, assets, corners and density. The same Squirrel GEL powers every Squirrel preview.</p></div><div><h2>From browsing to belonging.</h2><p>Catalogue, product, cart, payment handoff, return states, a purchase-scoped review and customer review management.</p></div></section><footer><span>Static HTML + CSS + small JavaScript modules</span><span>Illustrative content · preview data stays in your browser</span></footer></main></body></html>""",
    )
    write(
        output / "gallery.css",
        """*{box-sizing:border-box}body{margin:0;background:#F5F4EF;color:#24382F;font-family:Arial,Helvetica,sans-serif;line-height:1.6}main{max-width:1240px;padding:42px 40px;margin:auto}a{color:inherit;text-underline-offset:4px}a:focus-visible{outline:3px solid #795021;outline-offset:6px}.wordmark{font-weight:700;font-size:1.2rem;text-decoration:none;display:flex;justify-content:space-between;gap:20px}.wordmark span,.edition{font-size:.65rem;letter-spacing:.16em;font-weight:500}.edition{margin-top:65px}h1{font-family:Georgia,serif;font-size:clamp(3rem,6vw,5.2rem);line-height:1.05;font-weight:400;letter-spacing:-.045em;margin:18px 0 24px}h1 em{color:#597558}.intro{font-size:1rem;max-width:610px;color:#53635A}.grid{display:grid;grid-template-columns:1fr 1fr;gap:28px;margin-top:50px}.card{border:1px solid #A7AEA6;border-radius:12px;overflow:hidden;text-decoration:none;background:#fff}.mini{padding:22px 24px 0;background:#FAF8F2;height:235px;overflow:hidden}.mini-header{display:flex;justify-content:space-between;align-items:center;font-family:Georgia,serif;font-size:1rem}.mini-header span+span{font-family:Arial,sans-serif;font-size:.45rem;letter-spacing:.09em}.mini-hero{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:center;margin-top:22px}.mini-hero>span{font-family:Georgia,serif;font-size:1.65rem;line-height:1.1;letter-spacing:-.04em}.mini-hero img{width:100%;border-radius:8px}.studio .mini{background:#244D3E;color:#fff}.studio .mini-hero>span{font-size:2rem}.studio .mini-hero img{transform:rotate(4deg);border:4px solid #FAF8F2}.classic .mini-header{border-bottom:16px solid #244D3E;padding-bottom:10px}.classic .mini-hero{margin-top:12px}.classic .mini-hero>span{font-size:1.45rem}.northline-classic .mini{background:#F3F4F5;color:#243D55}.northline-classic .mini-header{font-family:Arial,sans-serif;font-weight:700;border-bottom:16px solid #243D55;padding-bottom:10px}.northline-classic .mini-hero>span{font-family:Arial,sans-serif;font-weight:700}.card-copy{display:flex;align-items:center;gap:18px;padding:22px}.card-copy>span:last-child{margin-left:auto;font-size:1.4rem}.number{font-size:.7rem;color:#59675F}.card h2{font-size:1.2rem;font-weight:500;line-height:1.3;margin:0 0 5px}.card p{font-size:.78rem;color:#59675F;margin:0}.notes{display:grid;grid-template-columns:1fr 1fr;gap:60px;border-top:1px solid #ABB4AA;padding-top:32px;margin-top:55px}.notes h2{font-size:1.2rem;font-weight:500}.notes p{font-size:.87rem;color:#59675F;max-width:450px}footer{display:flex;justify-content:space-between;gap:20px;font-size:.67rem;color:#59675F;margin-top:45px;padding-bottom:20px}@media(max-width:700px){main{padding:28px 20px}.wordmark{flex-direction:column;gap:4px}.edition{margin-top:45px}.grid{grid-template-columns:1fr;gap:22px;margin-top:35px}.mini{height:220px}.notes{grid-template-columns:1fr;gap:20px}footer{flex-direction:column;gap:8px}.mini-hero>span{font-size:1.5rem}.studio .mini-hero>span{font-size:1.8rem}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto}}""",
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--all", action="store_true", help="build Squirrel in all themes and Northline Classic plus gallery")
    p.add_argument("--theme", choices=THEMES, default="minimal")
    p.add_argument("--gel", type=Path, default=ROOT / "gel/squirrel.json")
    p.add_argument("--catalogue", type=Path, default=ROOT / "content/squirrel.json")
    p.add_argument("--assets-root", type=Path, default=ROOT)
    p.add_argument("--base", default="/")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error("output already exists; choose a new path or remove a previous build explicitly")
    if args.all:
        args.output.mkdir(parents=True)
        for theme in THEMES:
            build_site(
                args.output / theme,
                ROOT / "gel/squirrel.json",
                ROOT / "content/squirrel.json",
                theme,
                "/" + theme + "/",
            )
        build_site(
            args.output / "northline-classic",
            ROOT / "gel/northline.json",
            ROOT / "content/northline.json",
            "classic",
            "/northline-classic/",
        )
        gallery(args.output)
    else:
        build_site(args.output, args.gel, args.catalogue, args.theme, args.base, args.assets_root)
    print(f"Built static preview: {args.output}")


if __name__ == "__main__":
    main()
