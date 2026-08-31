from __future__ import annotations

import json
import os
from pathlib import Path
from shutil import copy2
from urllib.parse import urlparse

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "storefront"
DIST = ROOT / "storefront-dist"
STATIC_FILES = ("index.html", "app.js", "styles.css")


def main() -> None:
    mode = os.environ.get("CARTRAY_STOREFRONT_MODE", "preview")
    if mode not in {"preview", "production"}:
        raise ValueError("CARTRAY_STOREFRONT_MODE must be preview or production")
    config = _configuration(mode)
    DIST.mkdir(exist_ok=True)
    for name in STATIC_FILES:
        copy2(SOURCE / name, DIST / name)
    (DIST / "storefront-config.js").write_text(f"window.CARTRAY_STOREFRONT = {json.dumps(config)};\n")


def _configuration(mode: str) -> dict[str, object]:
    if mode == "preview":
        return {"checkoutEnabled": False, "apiBaseUrl": None}
    api_base_url = os.environ.get("CARTRAY_STOREFRONT_API_BASE_URL")
    if not api_base_url:
        raise ValueError("CARTRAY_STOREFRONT_API_BASE_URL is required for production builds")
    parsed = urlparse(api_base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("CARTRAY_STOREFRONT_API_BASE_URL must be an absolute https URL")
    return {"checkoutEnabled": True, "apiBaseUrl": api_base_url.rstrip("/")}


if __name__ == "__main__":
    main()
