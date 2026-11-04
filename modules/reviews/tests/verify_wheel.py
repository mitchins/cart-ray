"""Verify the wheel contains the declared module resources."""

import json
import sys
from pathlib import Path
from zipfile import ZipFile

wheels = list(Path(sys.argv[1]).glob("cartray_reviews-*.whl"))
if len(wheels) != 1:
    raise SystemExit("Expected exactly one Reviews wheel")
with ZipFile(wheels[0]) as wheel:
    manifest = json.loads(wheel.read("cartray_reviews/module.json"))
    for migration in manifest["migrations"]:
        wheel.getinfo(f"cartray_reviews/{migration}")
