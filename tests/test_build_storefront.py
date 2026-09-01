from pathlib import Path
from runpy import run_path

import pytest

_configuration = run_path(Path(__file__).parents[1] / "scripts" / "build_storefront.py")["_configuration"]


@pytest.mark.parametrize(
    "api_base_url",
    ("https://username@example.test", "https://username:password@example.test"),
)
def test_production_storefront_configuration_rejects_userinfo(monkeypatch, api_base_url):
    monkeypatch.setenv("CARTRAY_STOREFRONT_API_BASE_URL", api_base_url)

    with pytest.raises(ValueError, match="absolute https URL"):
        _configuration("production")
