from pathlib import Path

from cartray.fingerprints import fingerprint_entries


def test_fingerprint_encoding_is_stable_for_ordered_relative_paths():
    entries = [(Path("nested/a.txt"), b"alpha"), (Path("root.txt"), b"beta")]
    assert fingerprint_entries(entries) == "df2d88f6c0f8d2886739f3e9e120345fffb9dd417a6e3c233bf3899f8cf60577"
