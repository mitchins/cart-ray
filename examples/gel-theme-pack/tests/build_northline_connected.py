#!/usr/bin/env python3
"""Build an explicit Northline test catalogue with the real connected adapter.

This fixture has no checkout backend. It is used only for browser qualification,
not by the merchant installer or a hosted storefront deployment.
"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

PACK = Path(__file__).resolve().parents[1]
REPO = PACK.parents[1]
sys.path.insert(0, str(PACK))
from connect import connect  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    products = json.loads((PACK / "content/northline.json").read_text())["products"]
    catalogue = {
        "version": "northline-connected-browser-fixture-v1",
        "products": [
            {
                **{
                    name: product[name]
                    for name in ("product_key", "title", "amount_minor", "currency", "short_description")
                },
                "max_quantity": 1,
                "image_url": "/" + product["image"],
            }
            for product in products
        ],
    }
    config = {
        "storeName": "Northline Supply",
        "checkoutEnabled": False,
        "apiBaseUrl": None,
        "previewCatalogue": catalogue,
        "productPaths": {product["product_key"]: f"/products/{product['slug']}/" for product in products},
        "reviewSubjects": {
            product["product_key"]: product["review_subject"] for product in products if product["review_subject"]
        },
        "reviewsEnabled": False,
        "reviewInvitationsEnabled": False,
        "reviewInvitationDefaultChecked": False,
    }
    with tempfile.TemporaryDirectory(prefix="northline-browser-source-") as temporary:
        source = Path(temporary)
        shutil.copyfile(REPO / "storefront/app.js", source / "app.js")
        shutil.copyfile(REPO / "examples/acorn-store/reviews.mjs", source / "reviews.mjs")
        (source / "storefront-config.js").write_text("window.CARTRAY_STOREFRONT = " + json.dumps(config) + ";\n")
        connect(source, REPO, args.output, PACK / "gel/northline.json", PACK / "content/northline.json", "classic")


if __name__ == "__main__":
    main()
