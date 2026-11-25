"""Snapshot and validate operator-owned connected merchant build inputs."""

import csv
import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

from cartray.fingerprints import fingerprint_entries
from cartray.merchant_contract import merchant_path, product_route_groups, register_path
from cartray.order_access_presentation import validate_order_presentation

STOREFRONT_CONFIG_PREFIX = 'window.CARTRAY_STOREFRONT = '

CORE_CLIENT_FILES = {
        "theme/core-storefront.js": "storefront/app.js",
        "theme/cartray-adapter.mjs": "examples/gel-theme-pack/src/cartray-adapter.mjs",
        "theme/connected-browse.mjs": "examples/gel-theme-pack/src/connected-browse.mjs",
        "theme/connected-start.mjs": "examples/gel-theme-pack/src/connected-start.mjs",
        "theme/app.mjs": "examples/gel-theme-pack/src/app.mjs",
        "theme/native-presenter.mjs": "examples/gel-theme-pack/src/native-presenter.mjs",
        "theme/gallery.mjs": "examples/gel-theme-pack/src/gallery.mjs",
}

REVIEW_CLIENT_FILES = {
    "theme/native-reviews.js": "modules/reviews/ui/reviews.js",
    "theme/native-manage.js": "modules/reviews/ui/manage.js",
    "theme/review-presenter.mjs": "examples/gel-theme-pack/src/connected-review.mjs",
    "theme/connected-public-reviews.mjs": "examples/gel-theme-pack/src/connected-public-reviews.mjs",
    "theme/reviews.mjs": "modules/reviews/ui/public.mjs",
}



class ConnectedShell(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = set()
        self.module_scripts = set()
        self.body = {}
        self.buttons = []
        self.ids = set()
        self.review_modes = set()
        self.dialog_controls = set()
        self.product_data = ""
        self._product_data_open = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if "data-native-review" in attrs:
            self.review_modes.add(attrs["data-native-review"])
        if tag == "button":
            self.dialog_controls.update(
                {name for name in ("data-dialog-cancel", "data-dialog-confirm") if name in attrs}
            )
        if tag == "script" and "src" in attrs:
            self.scripts.add(attrs["src"])
            if attrs.get("type") == "module":
                self.module_scripts.add(attrs["src"])
        elif tag == "script" and attrs.get("id") == "product-data" and attrs.get("type") == "application/json":
            self._product_data_open = True
        elif tag == "body":
            self.body = attrs
        elif tag == "button":
            self.buttons.append(attrs)

    def handle_data(self, data):
        if self._product_data_open:
            self.product_data += data

    def handle_endtag(self, tag):
        if tag == "script":
            self._product_data_open = False


def tree_hash(root):
    def entries():
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise ValueError("merchant inputs must contain only regular files and directories")
            if path.is_file():
                yield path.relative_to(root), path.read_bytes()
    return fingerprint_entries(entries())


def commerce_build_hash(root, lock):
    entries = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    entries[Path("stripe-test-preflight.lock.json")] = lock
    return fingerprint_entries(sorted(entries.items()))


def javascript_json(path, prefix):
    text = path.read_text()
    if not text.startswith(prefix):
        raise ValueError(f"invalid connected storefront configuration: {path.name}")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[len(prefix):].lstrip())
    except ValueError as error:
        raise ValueError(f"invalid connected storefront configuration: {path.name}") from error
    if not isinstance(value, dict):
        raise ValueError("connected storefront configuration must be an object")
    return value


def replace_javascript_json(path, prefix, value):
    """Change only the JSON object; retain generated runtime assignments verbatim."""
    text = path.read_bytes().decode('utf-8')
    if not text.startswith(prefix):
        raise ValueError('invalid connected storefront configuration')
    tail = text[len(prefix):]
    start = len(prefix) + len(tail) - len(tail.lstrip())
    _, end = json.JSONDecoder().raw_decode(text[start:])
    path.write_bytes((text[:start] + json.dumps(value, ensure_ascii=True) + text[start + end:]).encode())


def validate(root, repo):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("merchant input root must be a regular directory")
    commerce, site = root / "commerce", root / "storefront"
    if not commerce.is_dir() or not site.is_dir():
        raise ValueError("merchant inputs require commerce and storefront directories")
    if (site / "_worker.js").exists() or (site / "functions").exists():
        raise ValueError("merchant static inputs must not supply a Pages Worker or functions")
    allowed_commerce = {"catalogue.csv", "fulfilment-expansions.json", "presentation.csv", "product-assets"}
    if {p.name for p in commerce.iterdir()} != allowed_commerce:
        raise ValueError("merchant commerce inputs must contain only declared catalogue files and assets")
    files = [p for p in root.rglob("*") if p.is_file()]
    if len(files) > 10000 or sum(p.stat().st_size for p in files) > 200_000_000:
        raise ValueError("merchant inputs exceed installation limits")
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("merchant inputs must not contain symlinks or special files")
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            raise ValueError("merchant inputs must not contain hidden files")
    with (commerce / "catalogue.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    keys = [row.get("product_key") for row in rows]
    if (not keys or len(set(keys)) != len(keys)
            or any(not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,63}", x or "") for x in keys)):
        raise ValueError("invalid merchant commerce product identities")
    config = javascript_json(site / "storefront-config.js", STOREFRONT_CONFIG_PREFIX)
    theme = javascript_json(site / "site-config.js", "window.CARTRAY_THEME = ")
    if (config.get("previewCatalogue") is not None or config.get("checkoutEnabled") is not True
            or not isinstance(config.get("productPaths"), dict) or set(config["productPaths"]) != set(keys)
            or not all(isinstance(value, str) for value in config["productPaths"].values())
            or theme.get("schemaVersion") != 1 or theme.get("base") != "/"
            or theme.get("preview") is not False or theme.get("adapter") != "/theme/cartray-adapter.mjs"
            or theme.get("gel", {}).get("identity", {}).get("name") != config.get("storeName")):
        raise ValueError("merchant storefront must match authorised catalogue identities and connected adapter")
    if "orderAccessPresentation" in config:
        validate_order_presentation(config["orderAccessPresentation"])
    product_route_groups(config["productPaths"], config.get("reviewSubjects"))
    catalogue = theme.get("catalogue")
    products = catalogue.get("products") if isinstance(catalogue, dict) else None
    if (not isinstance(products, list) or len(products) != len(keys)
            or any(not isinstance(p, dict) or not all(isinstance(p.get(field), str) and p[field]
                   for field in ("product_key", "slug", "image", "image_alt", "category")) for p in products)
            or {p["product_key"] for p in products} != set(keys)):
        raise ValueError("merchant storefront catalogue presentation is incomplete")
    for product in products:
        if "cart_quantity_limit" in product and (type(product["cart_quantity_limit"]) is not int
                                          or not 1 <= product["cart_quantity_limit"] <= 100):
            raise ValueError("invalid presentation maximum quantity")
        image = product["image"]
        relative = Path(image.lstrip("/"))
        if (not image.startswith("/") or image.startswith("//") or ".." in relative.parts
                or relative.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".svg")
                or not (site / relative).is_file()
                or config["productPaths"][product["product_key"]]
                != product.get("path", f'/products/{product["slug"]}/')):
            raise ValueError("merchant storefront presentation image or product route is invalid")
    browse = theme.get("browseOffers")
    if (not isinstance(browse, list) or any(not isinstance(row, dict) or set(row) != {"sourceId", "productKey"}
                                           or not isinstance(row["productKey"], str)
                                           for row in browse)
            or {row["productKey"] for row in browse} != set(keys) or len(browse) != len(keys)
            or any(not isinstance(row["sourceId"], (str, int)) or isinstance(row["sourceId"], bool) for row in browse)
            or len({str(row["sourceId"]) for row in browse}) != len(browse)):
        raise ValueError("merchant storefront offer mapping differs from authorised catalogue")
    if any(not (site / route / "index.html").is_file() for route in ("cart", "purchase-return")):
        raise ValueError("merchant storefront is missing its connected cart or return page")
    for route in ("cart", "purchase-return"):
        shell = ConnectedShell()
        shell.feed((site / route / "index.html").read_text())
        if (shell.body.get("data-cartray-presenter") != "gel" or shell.body.get("data-page") != route
                or not {"/storefront-config.js", "/site-config.js"} <= shell.scripts
                or "/theme/connected-start.mjs" not in shell.module_scripts or "content" not in shell.ids):
            raise ValueError(f"merchant {route} page does not load the connected storefront")
    for relative, source in CORE_CLIENT_FILES.items():
        if (site / relative).read_bytes() != (repo / source).read_bytes():
            raise ValueError(f"merchant storefront has unrecognised connected code: {relative}")
    sources = {row["productKey"]: str(row["sourceId"]) for row in browse}
    prefixes = {}
    for key, path in config["productPaths"].items():
        merchant_path(path, product=True)
        register_path(path, prefixes)
        if not (site / path.lstrip("/") / "index.html").is_file():
            raise ValueError("merchant product route is missing")
        page = ConnectedShell()
        page.feed((site / path.lstrip("/") / "index.html").read_text())
        if (page.body.get("data-cartray-presenter") != "gel"
                or not {"/storefront-config.js", "/site-config.js"} <= page.scripts
                or "/theme/connected-browse.mjs" not in page.module_scripts
                or not any(button.get("data-cartray-add") == key or button.get("data-uat-add") == sources[key]
                           for button in page.buttons)):
            raise ValueError("merchant product route does not load its connected purchase action")
    return tree_hash(root)


def snapshot(source, destination, repo):
    before = validate(source, repo)
    shutil.copytree(source, destination, symlinks=False)
    after = validate(destination, repo)
    if before != after:
        raise ValueError("merchant inputs changed while being copied")
    return after
