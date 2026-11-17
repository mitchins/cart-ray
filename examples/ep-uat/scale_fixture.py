#!/usr/bin/env python3
"""Create an explicitly synthetic, checkout-disabled scaling snapshot for local benchmarks."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path


def create(source, output, factor):
    if factor not in (5, 10) or output.exists():
        raise ValueError("factor must be 5 or 10 and output must be new")
    source, output = Path(source), Path(output)
    data = json.loads((source / "catalogue.json").read_text())
    original = data["products"]
    products = list(original)
    for batch in range(1, factor):
        for index, item in enumerate(original):
            synthetic = copy.deepcopy(item)
            synthetic["source_id"] = 90_000_000 + batch * len(original) + index
            synthetic["navigation_path"] = f"/products/synthetic-{batch}-{index}/"
            synthetic["key"] = f"synthetic:scaling:{batch}:{index}"
            synthetic["title"] = f"Synthetic {batch}-{index}: {item['title']}"
            synthetic["action"] = "browse_only"
            synthetic["external_url"] = None
            synthetic["external_label"] = None
            synthetic["free_download"] = None
            synthetic["variations"] = []
            synthetic["default_attributes"] = {}
            synthetic["source_workflow_keys"] = []
            synthetic["source_delivery_kind"] = "none"
            products.append(synthetic)
    data["products"] = products
    data["synthetic_scaling_factor"] = factor
    data["reconciliation"]["products"] = len(products)
    output.mkdir(parents=True)
    shutil.copytree(source / "media", output / "media")
    (output / "catalogue.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"Synthetic scaling fixture: {len(products)} listings, including {len(original)} real source products.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--factor", type=int, required=True)
    args = parser.parse_args()
    create(args.source, args.output, args.factor)
