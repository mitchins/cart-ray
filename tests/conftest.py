from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cartray.catalogue import FixturePriceResolver, build_catalogue, load_catalogue_sources
from cartray.checkout import CheckoutService
from cartray.gateway import FakePaymentGateway
from cartray.store import SqliteOrderStore


@pytest.fixture
def fixture_catalogue():
    root = Path(__file__).parents[1] / "fixtures"
    sources = load_catalogue_sources(root / "catalogue.csv")
    resolver = FixturePriceResolver.from_json(root / "price-resolutions.json")
    expansions = {
        key: tuple(value) for key, value in json.loads((root / "fulfilment-expansions.json").read_text()).items()
    }
    return asyncio.run(build_catalogue(sources, resolver, expansions))


@pytest.fixture
def checkout_service(fixture_catalogue):
    gateway = FakePaymentGateway()
    service = CheckoutService(fixture_catalogue, SqliteOrderStore.in_memory(), gateway)
    return service, gateway
