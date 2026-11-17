from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ep_uat_media", ROOT / "examples/ep-uat/media_qualification.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_truncated_jpeg_with_matching_hash_is_not_a_valid_image(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    image = media / "test.jpg"
    Image.new("RGB", (64, 64), "blue").save(image, format="JPEG")
    raw = image.read_bytes()

    def manifest(blob):
        image.write_bytes(blob)
        (tmp_path / "catalogue.json").write_text(json.dumps({
            "source_archive_sha256": "test", "public_media": [
                {"path": "media/test.jpg", "sha256": hashlib.sha256(blob).hexdigest()}
            ]
        }))

    manifest(raw)
    assert module.qualify(tmp_path)["verified_raster_images"] == 1
    manifest(raw[:-20])
    with pytest.raises(OSError):
        module.qualify(tmp_path)
