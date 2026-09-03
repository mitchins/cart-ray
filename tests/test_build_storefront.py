from pathlib import Path
from runpy import run_path

import pytest

from cartray.build_profiles import selected_catalogue_profile

_configuration = run_path(Path(__file__).parents[1] / "scripts" / "build_storefront.py")["_configuration"]
_assert_selected_catalogue_is_valid = run_path(Path(__file__).parents[1] / "scripts" / "build_storefront.py")[
    "_assert_selected_catalogue_is_valid"
]
_copy_profile_assets = run_path(Path(__file__).parents[1] / "scripts" / "build_storefront.py")["_copy_profile_assets"]
_selected_profile = run_path(Path(__file__).parents[1] / "scripts" / "build_storefront.py")["_selected_profile"]


@pytest.mark.parametrize(
    "api_base_url",
    ("https://username@example.test", "https://username:password@example.test"),
)
def test_production_storefront_configuration_rejects_userinfo(monkeypatch, api_base_url):
    monkeypatch.setenv("CARTRAY_STOREFRONT_API_BASE_URL", api_base_url)

    with pytest.raises(ValueError, match="absolute https URL"):
        _configuration("production")


def test_production_storefront_can_validate_the_locked_real_test_subset_profile():
    profile = selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "real-test-subset"})

    _assert_selected_catalogue_is_valid(profile)


def test_preview_storefront_rejects_the_real_test_subset(monkeypatch):
    monkeypatch.setenv("CARTRAY_CATALOGUE_PROFILE", "real-test-subset")

    with pytest.raises(ValueError, match="preview requires the synthetic"):
        _selected_profile("preview")


def test_storefront_copies_selected_profile_images_at_the_public_image_url_path(tmp_path):
    profile = selected_catalogue_profile({"CARTRAY_CATALOGUE_PROFILE": "real-test-subset"})

    _copy_profile_assets(profile, tmp_path)

    assert (tmp_path / "assets" / "products" / "ep-sil-2026-cover-v1.webp").is_file()
    assert (tmp_path / "assets" / "products" / "ep-lms-training-catalogue-cover-v1.webp").is_file()
