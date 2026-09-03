"""Select a reviewed, repository-local catalogue profile for a build."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CatalogueProfile:
    """Fixed local inputs used to compile one permitted catalogue deployment artifact."""

    name: str
    catalogue: Path
    fulfilment_expansions: Path
    presentation: Path
    product_assets: Path
    price_resolutions: Path | None = None
    preflight_lock: Path | None = None

    def compiler_arguments(self, output: Path, *, check: bool = False) -> list[str]:
        """Returns compile-script arguments without accepting caller-controlled source paths."""

        arguments = [
            "--catalogue",
            str(self.catalogue),
            "--fulfilment-expansions",
            str(self.fulfilment_expansions),
            "--presentation",
            str(self.presentation),
            "--product-assets",
            str(self.product_assets),
            "--output",
            str(output),
        ]
        if self.price_resolutions is not None:
            arguments.extend(("--price-resolutions", str(self.price_resolutions)))
        else:
            if self.preflight_lock is None:
                raise ValueError("catalogue profile requires a preflight lock or fixture price resolutions")
            arguments.extend(("--preflight-lock", str(self.preflight_lock)))
        if check:
            arguments.append("--check")
        return arguments


_PROFILES = {
    "synthetic": CatalogueProfile(
        name="synthetic",
        catalogue=ROOT / "fixtures" / "catalogue.csv",
        fulfilment_expansions=ROOT / "fixtures" / "fulfilment-expansions.json",
        presentation=ROOT / "fixtures" / "catalogue-presentation.csv",
        product_assets=ROOT / "storefront" / "assets" / "products",
        price_resolutions=ROOT / "fixtures" / "price-resolutions.json",
    ),
    "real-test-subset": CatalogueProfile(
        name="real-test-subset",
        catalogue=ROOT / "catalogue" / "real-test-subset" / "catalogue.csv",
        fulfilment_expansions=ROOT / "catalogue" / "real-test-subset" / "fulfilment-expansions.json",
        presentation=ROOT / "catalogue" / "real-test-subset" / "catalogue-presentation.csv",
        product_assets=ROOT / "storefront" / "assets" / "products",
        preflight_lock=ROOT / "catalogue" / "real-test-subset" / "stripe-test-preflight.lock.json",
    ),
}


def selected_catalogue_profile(environ: dict[str, str] | None = None) -> CatalogueProfile:
    """Returns the explicitly selected profile, defaulting only to the synthetic fixture profile."""

    name = (os.environ if environ is None else environ).get("CARTRAY_CATALOGUE_PROFILE", "synthetic")
    try:
        return _PROFILES[name]
    except KeyError as error:
        permitted = ", ".join(sorted(_PROFILES))
        raise ValueError(f"CARTRAY_CATALOGUE_PROFILE must be one of: {permitted}") from error
