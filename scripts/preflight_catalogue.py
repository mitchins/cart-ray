from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from cartray.errors import CartRayError
from cartray.preflight import UrllibStripeTransport, preflight_catalogue, preflight_lock_output, preflight_output
from cartray.stripe import StripeApiError


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a CartRay CSV catalogue against Stripe test-mode Prices.")
    parser.add_argument("--catalogue", type=Path, required=True, help="local UTF-8 CartRay catalogue CSV")
    parser.add_argument(
        "--fulfilment-expansions", type=Path, required=True, help="local private fulfilment-expansions JSON"
    )
    parser.add_argument("--output-lock", type=Path, help="write the private reviewed Stripe test preflight lock")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        catalogue = asyncio.run(
            preflight_catalogue(
                catalogue_path=arguments.catalogue,
                fulfilment_expansions_path=arguments.fulfilment_expansions,
                environ=os.environ,
                transport=UrllibStripeTransport(),
            )
        )
    except (CartRayError, StripeApiError, OSError, ValueError):
        print("catalogue preflight failed", file=sys.stderr)
        return 1
    if arguments.output_lock is not None:
        arguments.output_lock.parent.mkdir(parents=True, exist_ok=True)
        arguments.output_lock.write_text(
            json.dumps(preflight_lock_output(catalogue), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(preflight_output(catalogue), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
