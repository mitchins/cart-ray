"""Small, strict GEL v1 compiler. Standard library only; no merchant CSS."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

THEMES = ("minimal", "studio", "classic")
FONTS = {
    "editorial": 'Georgia, "Times New Roman", serif',
    "modern": "Arial, Helvetica, sans-serif",
    "humanist": '-apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif',
    "system": "system-ui, sans-serif",
}
COLOURS = (
    "brand",
    "on_brand",
    "accent",
    "on_accent",
    "background",
    "surface",
    "text",
    "muted",
    "border",
    "focus",
    "danger",
)


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def contrast(a, b):
    def lum(c):
        values = [int(c[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        values = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in values]
        return sum(x * w for x, w in zip(values, (0.2126, 0.7152, 0.0722)))

    x, y = sorted((lum(a), lum(b)))
    return (y + 0.05) / (x + 0.05)


def keys(value, expected, where, optional=()):
    if not isinstance(value, dict) or set(value) - set(expected) - set(optional) or set(expected) - set(value):
        raise ValueError(f"{where}: missing or unknown fields")


def text(value, where, maximum=500):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 and c != "\n" for c in value)
    ):
        raise ValueError(f"{where}: expected non-empty plain text up to {maximum} characters")


def validate_asset(root, name):
    if not isinstance(name, str) or not re.fullmatch(r"assets/[A-Za-z0-9_./-]+\.(svg|png|jpg|jpeg|webp)", name):
        raise ValueError("asset must be a local assets/ image path")
    if ".." in Path(name).parts:
        raise ValueError("asset traversal is not allowed")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or (root / name).is_symlink():
        raise ValueError(f"asset missing or unsafe: {name}")
    if path.stat().st_size > 2_000_000:
        raise ValueError("asset exceeds 2 MB")
    raw = path.read_bytes()
    if path.suffix == ".svg":
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise ValueError("SVG DTDs and entities are not permitted")
        tree = ET.fromstring(raw)
        allowed = {
            "svg",
            "g",
            "path",
            "rect",
            "circle",
            "ellipse",
            "line",
            "polyline",
            "polygon",
            "defs",
            "linearGradient",
            "radialGradient",
            "stop",
            "filter",
            "feGaussianBlur",
            "clipPath",
            "pattern",
            "title",
            "desc",
        }
        for el in tree.iter():
            if el.tag.split("}")[-1] not in allowed:
                raise ValueError("SVG must be passive vector artwork")
            for key, val in el.attrib.items():
                k = key.split("}")[-1].lower()
                if (
                    k.startswith("on")
                    or k in ("href", "src", "style")
                    or re.search(r"(javascript:|https?:|data:|//)", val, re.I)
                ):
                    raise ValueError("SVG must not contain active content or remote resources")
                if "url(" in val and not re.fullmatch(r"url\(#[A-Za-z0-9_-]+\)", val):
                    raise ValueError("only local SVG fragment references are permitted")
    elif path.suffix == ".png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")
    elif path.suffix in (".jpg", ".jpeg") and not raw.startswith(b"\xff\xd8"):
        raise ValueError("invalid JPEG signature")
    elif path.suffix == ".webp" and not (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
        raise ValueError("invalid WebP signature")
    return hashlib.sha256(raw).hexdigest()


def validate(gel, root):
    keys(gel, ("schema_version", "identity", "assets", "colours", "typography", "shape", "layout", "hero"), "GEL")
    if gel["schema_version"] != "1.0":
        raise ValueError("unsupported GEL schema version")
    identity = gel["identity"]
    keys(
        identity,
        (
            "key",
            "name",
            "short_name",
            "wordmark_descriptor",
            "tagline",
            "description",
            "contact_email",
            "locale",
            "currency",
        ),
        "identity",
    )
    for k, v in identity.items():
        text(v, "identity." + k, 500)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", identity["key"]):
        raise ValueError("invalid merchant key")
    if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", identity["locale"]):
        raise ValueError("invalid locale")
    if not re.fullmatch(r"[A-Z]{3}", identity["currency"]):
        raise ValueError("invalid currency")
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", identity["contact_email"]):
        raise ValueError("invalid contact email")
    keys(
        gel["assets"], ("logo", "logo_inverse", "favicon"), "assets", ("hero", "hero_alt", "stripe_icon", "stripe_logo")
    )
    if bool(gel["assets"].get("hero")) != bool(gel["assets"].get("hero_alt")):
        raise ValueError("hero requires a text alternative")
    hashes = {k: validate_asset(root, v) for k, v in gel["assets"].items() if k != "hero_alt"}
    if "hero_alt" in gel["assets"]:
        text(gel["assets"]["hero_alt"], "hero alt")
    c = gel["colours"]
    keys(c, COLOURS, "colours")
    for k, v in c.items():
        if not isinstance(v, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError(f"invalid colour: {k}")
    checks = []
    pairs = [("brand", "on_brand", 4.5), ("accent", "on_accent", 4.5)]
    for surface in ("background", "surface"):
        pairs += [(name, surface, 4.5) for name in ("text", "muted", "brand", "danger")]
        pairs += [(name, surface, 3) for name in ("border", "focus")]
    for a, b, target in pairs:
        score = contrast(c[a], c[b])
        checks.append({"foreground": a, "background": b, "ratio": round(score, 3), "minimum": target})
        if score < target:
            raise ValueError(f"insufficient contrast: {a}/{b} = {score:.2f}, needs {target}:1")
    keys(gel["typography"], ("heading", "body"), "typography")
    if any(v not in FONTS for v in gel["typography"].values()):
        raise ValueError("use an approved font stack")
    keys(gel["shape"], ("corners",), "shape")
    keys(gel["layout"], ("density",), "layout")
    if gel["shape"]["corners"] not in ("square", "soft", "round"):
        raise ValueError("unsupported corners")
    if gel["layout"]["density"] not in ("compact", "comfortable", "spacious"):
        raise ValueError("unsupported density")
    keys(gel["hero"], ("eyebrow", "title", "body", "cta"), "hero")
    for k, v in gel["hero"].items():
        text(v, "hero." + k)
    return {"manifest_sha256": digest(gel), "asset_sha256": hashes, "contrast": checks}


def tokens(gel, theme):
    if theme not in THEMES:
        raise ValueError("unknown theme")
    corner = {"square": "2px", "soft": "12px", "round": "24px"}[gel["shape"]["corners"]]
    space = {"compact": "1rem", "comfortable": "1.5rem", "spacious": "2rem"}[gel["layout"]["density"]]
    global_tokens = {"--gel-" + k.replace("_", "-"): v for k, v in gel["colours"].items()}
    global_tokens.update(
        {
            "--gel-font-heading": FONTS[gel["typography"]["heading"]],
            "--gel-font-body": FONTS[gel["typography"]["body"]],
            "--gel-radius": corner,
            "--gel-gap": space,
        }
    )
    semantic = {
        "--color-page": "var(--gel-background)",
        "--color-panel": "var(--gel-surface)",
        "--color-ink": "var(--gel-text)",
        "--color-subtle": "var(--gel-muted)",
        "--color-action": "var(--gel-brand)",
        "--color-on-action": "var(--gel-on-brand)",
        "--color-highlight": "var(--gel-accent)",
        "--color-on-highlight": "var(--gel-on-accent)",
        "--color-rule": "var(--gel-border)",
        "--color-focus": "var(--gel-focus)",
        "--color-error": "var(--gel-danger)",
    }
    components = {
        "--button-bg": "var(--color-action)",
        "--button-fg": "var(--color-on-action)",
        "--card-radius": "var(--gel-radius)",
        "--card-gap": "var(--gel-gap)",
        "--field-border": "var(--color-rule)",
        "--page-max": {"minimal": "1240px", "studio": "1360px", "classic": "1280px"}[theme],
    }
    resolved = {**global_tokens, **semantic, **components}
    css = ":root {\n" + "".join(f"  {k}: {v};\n" for k, v in resolved.items()) + "}\n"
    return css, {"global": global_tokens, "semantic": semantic, "component": components}
