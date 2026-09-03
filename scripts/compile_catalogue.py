from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from cartray.catalogue import CsvCatalogueSourceAdapter, FixturePriceResolver
from cartray.catalogue_bundle import compile_catalogue_bundle, render_compiled_catalogue_module
from cartray.presentation import CsvPresentationSourceAdapter, validate_presentation_assets


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile reviewed CartRay catalogue inputs into a Worker bundle.")
    parser.add_argument("--catalogue", required=True, type=Path)
    parser.add_argument("--price-resolutions", required=True, type=Path)
    parser.add_argument("--fulfilment-expansions", required=True, type=Path)
    parser.add_argument("--presentation", required=True, type=Path)
    parser.add_argument("--product-assets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="fail if output is absent or differs from the compiler")
    return parser.parse_args()


def main() -> None:
    options = arguments()
    expansions = {key: tuple(value) for key, value in json.loads(options.fulfilment_expansions.read_text()).items()}
    presentation_adapter = CsvPresentationSourceAdapter(options.presentation)
    presentation_sources = asyncio.run(presentation_adapter.load())
    validate_presentation_assets(presentation_sources, options.product_assets)
    bundle = asyncio.run(
        compile_catalogue_bundle(
            CsvCatalogueSourceAdapter(options.catalogue),
            FixturePriceResolver.from_json(options.price_resolutions),
            expansions,
            presentation_adapter,
        )
    )
    rendered = render_compiled_catalogue_module(bundle)
    if options.check:
        if not options.output.is_file() or options.output.read_text() != rendered:
            raise SystemExit("compiled catalogue bundle is stale")
        return
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(rendered)


if __name__ == "__main__":
    main()
