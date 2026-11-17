"""Compose CartRay's connected GEL flow pages for a merchant catalogue adapter."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .src import templates

ROOT = Path(__file__).resolve().parent


def install(
    output: Path,
    repo: Path,
    gel: dict,
    theme: str,
    offers: list[dict],
    *,
    context_path: str | None = None,
    return_extension: str | None = None,
    api_origin: str | None = None,
    reviews_enabled: bool = False,
):
    """Install shared cart/return UI; offers are presentation only, never checkout authority."""
    if not offers or len({offer["product_key"] for offer in offers}) != len(offers):
        raise ValueError("connected flows require distinct configured offers")
    if context_path is not None and (
        context_path.startswith("//") or not re.fullmatch(r"/[A-Za-z0-9/_-]{1,100}", context_path)
    ):
        raise ValueError("context path must be same-origin")
    if return_extension is not None and not re.fullmatch(r"/theme/[A-Za-z0-9_-]+\.mjs", return_extension):
        raise ValueError("return extension must be a local theme module")
    if api_origin is not None:
        try:
            url = urlsplit(api_origin)
            _ = url.port  # Reject malformed or out-of-range ports.
        except (ValueError, TypeError) as error:
            raise ValueError("checkout API must be an HTTPS origin") from error
        if (url.scheme != "https" or not url.hostname or not re.fullmatch(r"[A-Za-z0-9.-]+", url.hostname)
                or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")
                or any(char.isspace() for char in api_origin)):
            raise ValueError("checkout API must be an HTTPS origin")
        api_origin = api_origin.rstrip("/")
    source_ids, paths = set(), set()
    for offer in offers:
        if (
            not re.fullmatch(r"[A-Z0-9-]{1,64}", offer["product_key"])
            or not re.fullmatch(r"/products/[a-z0-9]+(?:-[a-z0-9]+)*/", offer["path"])
            or offer["path"] in paths
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(offer["source_id"]))
            or str(offer["source_id"]) in source_ids
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", offer["image"])
            or offer["image"].startswith("//")
            or any(part in (".", "..") for part in offer["image"].split("/"))
            or not isinstance(offer["image_alt"], str)
            or not isinstance(offer["category"], str)
        ):
            raise ValueError("invalid connected offer presentation")
        source_ids.add(str(offer["source_id"]))
        paths.add(offer["path"])
    theme_dir = output / "theme"
    for name in (
        "app.mjs",
        "cartray-adapter.mjs",
        "native-presenter.mjs",
        "connected-browse.mjs",
        "connected-start.mjs",
    ):
        shutil.copyfile(ROOT / "src" / name, theme_dir / name)
    shutil.copyfile(repo / "storefront/app.js", theme_dir / "core-storefront.js")
    paths = {offer["product_key"]: offer["path"] for offer in offers}
    browse = [{"sourceId": offer["source_id"], "productKey": offer["product_key"]} for offer in offers]
    presentations = [
        {
            "product_key": offer["product_key"],
            "slug": offer["path"].split("/")[-2],
            "image": offer["image"],
            "image_alt": offer["image_alt"],
            "category": offer["category"],
        }
        for offer in offers
    ]
    if return_extension and not (output / return_extension.lstrip("/")).is_file():
        raise ValueError("return extension is missing from the storefront")
    config = {
        "schemaVersion": 1,
        "preview": False,
        "base": "/",
        "adapter": "/theme/cartray-adapter.mjs",
        "gel": gel,
        "catalogue": {"products": presentations},
        "contextPath": context_path,
        "returnExtension": return_extension,
        "browseOffers": browse,
    }
    (output / "site-config.js").write_text("window.CARTRAY_THEME = " + json.dumps(config, ensure_ascii=True) + ";\n")
    storefront = {
        "storeName": gel["identity"]["name"],
        "checkoutEnabled": True,
        "previewCatalogue": None,
        "productPaths": paths,
        "reviewSubjects": {},
        "reviewsEnabled": reviews_enabled,
        "reviewInvitationsEnabled": False,
    }
    api = json.dumps(api_origin, ensure_ascii=True) if api_origin else "location.origin"
    (output / "storefront-config.js").write_text(
        "window.CARTRAY_STOREFRONT = "
        + json.dumps(storefront, ensure_ascii=True)
        + "; window.CARTRAY_STOREFRONT.apiBaseUrl = " + api + "; window.CARTRAY_CONNECTED_CATALOGUE = true;\n"
    )
    for route, page in (("cart/index.html", "cart"), ("purchase-return/index.html", "purchase-return")):
        content = templates.simple_page(page, gel, "/")
        html = templates.page_shell(
            gel,
            theme,
            "/",
            page,
            page.replace("-", " ").title(),
            content,
            False,
            nav_links=(("/shop/", "Catalogue"),),
            minimal_footer=True,
        )
        for old, new in (
            ("<body class=", '<body data-cartray-presenter="gel" class='),
            ('<script src="/site-config.js" defer>',
             '<script src="/storefront-config.js" defer></script><script src="/site-config.js" defer>'),
            ('src="/theme/app.mjs"', 'src="/theme/connected-start.mjs"'),
        ):
            if html.count(old) != 1:
                raise ValueError("connected page template is missing an expected unique element")
            html = html.replace(old, new, 1)
        path = output / route
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html)
