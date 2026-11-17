from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from test_prepare import fixture
from test_prepare import module as prepare

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ep_uat_build", ROOT / "examples/ep-uat/build.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def make_source(path):
    blobs = fixture()
    data = prepare.prepare(blobs, "synthetic-hash")
    path.mkdir()
    (path / "catalogue.json").write_text(json.dumps(data))
    for entry in data["public_media"]:
        target = path / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[entry["path"]])
    return data


@pytest.mark.parametrize("theme", ["minimal", "studio", "classic"])
def test_local_build_preserves_identity_without_checkout_or_canonical(tmp_path, theme):
    source = tmp_path / "source"
    data = make_source(source)
    hidden = source / "media/private-draft.png"
    hidden.write_bytes(b"not approved")
    output = tmp_path / "output"
    module.build(source, output, theme)
    page = (output / "products/woo-1/index.html").read_text()
    assert data["products"][0]["key"] == "ep:woo:offer:1"
    assert "ep:woo:offer:2" in page and "checkout is not available" in page.lower()
    assert 'rel="canonical"' not in page and "stripe" not in page.lower()
    assert not (output / "media/private-draft.png").exists()


def test_local_build_rejects_tampered_media_and_checkout_flag(tmp_path):
    source = tmp_path / "source"
    data = make_source(source)
    media = source / data["public_media"][0]["path"]
    media.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="fingerprint"):
        module.build(source, tmp_path / "out", "minimal")
    media.write_bytes(fixture()[data["public_media"][0]["path"]])
    data["products"][0]["checkout_authorized"] = True
    (source / "catalogue.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="checkout"):
        module.build(source, tmp_path / "out", "minimal")
