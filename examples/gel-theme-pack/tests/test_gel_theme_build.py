import copy
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import ROOT, build_site
from connect import BOOT, EXPORTS, connect
from src.gel import contrast, tokens, validate, validate_asset
from src.templates import money


class GelTests(unittest.TestCase):
    def setUp(self):
        self.gel = json.loads((ROOT / "gel/squirrel.json").read_text())

    def test_defaults_validate(self):
        self.assertGreater(len(validate(self.gel, ROOT)["contrast"]), 10)

    def test_reject_unknown(self):
        self.gel["arbitrary_css"] = "body{}"
        with self.assertRaises(ValueError):
            validate(self.gel, ROOT)

    def test_reject_low_contrast(self):
        self.gel["colours"]["text"] = "#EEEEEE"
        with self.assertRaises(ValueError):
            validate(self.gel, ROOT)

    def test_reject_colour_injection(self):
        self.gel["colours"]["brand"] = "red;display:none"
        with self.assertRaises(ValueError):
            validate(self.gel, ROOT)

    def test_reject_font_injection(self):
        self.gel["typography"]["heading"] = "url(https://example.com/font)"
        with self.assertRaises(ValueError):
            validate(self.gel, ROOT)

    def test_theme_does_not_mutate_brand(self):
        before = copy.deepcopy(self.gel)
        for theme in ("minimal", "studio", "classic"):
            tokens(self.gel, theme)
        self.assertEqual(before, self.gel)

    def test_optional_hero(self):
        del self.gel["assets"]["hero"]
        del self.gel["assets"]["hero_alt"]
        validate(self.gel, ROOT)

    def test_contrast_known_values(self):
        self.assertEqual(contrast("#000000", "#FFFFFF"), 21)

    def test_traversal(self):
        with self.assertRaises(ValueError):
            validate_asset(ROOT, "assets/../../secret.svg")

    def test_active_svg_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            for raw in [
                '<svg xmlns="http://www.w3.org/2000/svg"><script>bad()</script></svg>',
                '<svg xmlns="http://www.w3.org/2000/svg" onload="bad()"/>',
                '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
                '<!DOCTYPE svg [<!ENTITY foo SYSTEM "file:///etc/passwd">]><svg/>',
            ]:
                (root / "assets/x.svg").write_text(raw)
                with self.assertRaises(ValueError):
                    validate_asset(root, "assets/x.svg")

    def test_deterministic_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            for path in (a, b):
                build_site(path, ROOT / "gel/squirrel.json", ROOT / "content/squirrel.json", "minimal")

            def hashes(root):
                return {
                    str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in root.rglob("*")
                    if p.is_file()
                }

            self.assertEqual(hashes(a), hashes(b))

    def test_all_pages_have_one_h1_labels_and_local_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "site"
            build_site(path, ROOT / "gel/squirrel.json", ROOT / "content/squirrel.json")
            pages = list(path.rglob("*.html"))
            self.assertEqual(len(pages), 13)
            for page in pages:
                html = page.read_text()
                self.assertEqual(len(re.findall(r"<h1\b", html)), 1, str(page))
                self.assertIn("Skip to content", html)
                self.assertIn("no-referrer", html)
                self.assertNotIn("fonts.googleapis", html)

    def test_existing_output_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                build_site(Path(tmp), ROOT / "gel/squirrel.json", ROOT / "content/squirrel.json")

    def test_connected_uses_original_exports_and_clients(self):
        # Contract fixture, not a claim that the full upstream repo was run here.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / "source"
            repo = root / "repo"
            output = root / "connected"
            site.mkdir()
            (repo / "modules/reviews/ui").mkdir(parents=True)
            products = json.loads((ROOT / "content/squirrel.json").read_text())["products"]
            cfg = {
                "productPaths": {p["product_key"]: "/products/" + p["slug"] + "/" for p in products},
                "reviewSubjects": {p["product_key"]: p["review_subject"] for p in products if p["review_subject"]},
                "checkoutEnabled": False,
            }
            (site / "storefront-config.js").write_text("window.CARTRAY_STOREFRONT = " + json.dumps(cfg) + ";")
            core = "\n".join(f"export function {name}() {{}}" for name in EXPORTS) + "\nfunction boot() {}\n" + BOOT
            (site / "app.js").write_text(core)
            (site / "reviews.mjs").write_text("// public reader fixture\n")
            for name in ("reviews.js", "manage.js"):
                (repo / "modules/reviews/ui" / name).write_text("// native fixture " + name + "\n")
            connect(site, repo, output, ROOT / "gel/squirrel.json", ROOT / "content/squirrel.json", "classic")
            self.assertEqual(core, (output / "theme/core-storefront.js").read_text())
            self.assertIn('data-cartray-presenter="gel"', (output / "index.html").read_text())
            self.assertEqual((site / "app.js").read_text(), core)
            self.assertEqual(
                (output / "theme/native-reviews.js").read_bytes(), (repo / "modules/reviews/ui/reviews.js").read_bytes()
            )
            self.assertFalse((output / "theme/demo-adapter.mjs").exists())
            self.assertFalse((output / "checkout-preview").exists())
            self.assertFalse((output / "inbox").exists())
            self.assertIn("/storefront-config.js", (output / "index.html").read_text())
            self.assertIn("Price loading", (output / "products/acorn-growing-kit/index.html").read_text())



class CurrencyTests(unittest.TestCase):
    def test_static_preview_prices_identify_the_actual_currency(self):
        self.assertEqual(money({"amount_minor": 2400, "currency": "EUR"}), "€24.00")
        self.assertEqual(money({"amount_minor": 2400, "currency": "GBP"}), "£24.00")
        self.assertEqual(money({"amount_minor": 0, "currency": "EUR"}), "Free")
        self.assertEqual(money({"amount_minor": 2400, "currency": "CHF"}), "CHF 24.00")


if __name__ == "__main__":
    unittest.main()
