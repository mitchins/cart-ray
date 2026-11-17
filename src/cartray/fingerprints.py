"""Shared encoding for operator and build artifact fingerprints."""

import hashlib


def fingerprint_entries(entries):
    """Hash ordered relative paths and their content digests using the build format."""
    digest = hashlib.sha256()
    for path, content in entries:
        digest.update(str(path).encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()
