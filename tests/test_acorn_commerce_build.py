"""Qualification guardrails for retained Acorn commerce artifacts."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acorn_commerce_build", ROOT / "scripts/build_acorn_commerce.py")
assert spec and spec.loader
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

CONFIG = """name = "acorn-test"
account_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
main = "/tmp/old/src/cartray/worker.py"
compatibility_date = "2026-08-31"
compatibility_flags = ["python_workers"]
[build]
command = "uv run python scripts/sync_worker_source.py"
cwd = "/tmp/old"
[vars]
CARTRAY_ENVIRONMENT = "test"
[[d1_databases]]
binding = "DB"
database_id = "db-one"
"""


def test_normalize_forces_archive_source_and_only_allows_test_configuration():
    clean = build.normalized_config(CONFIG)
    assert 'main = "src/cartray/worker.py"' in clean
    assert "/tmp/old" not in clean
    assert 'main = "worker.py"' in build.deployment_config(clean)
    with pytest.raises(ValueError):
        build.normalized_config(CONFIG.replace('"test"', '"production"'))
    with pytest.raises(ValueError):
        build.normalized_config(CONFIG.replace("scripts/sync_worker_source.py", "scripts/other.py"))
    with pytest.raises(ValueError):
        build.normalized_config(
            CONFIG.replace('CARTRAY_ENVIRONMENT = "test"', 'CARTRAY_ENVIRONMENT = "test"\nSTRIPE_API_KEY = "oops"')
        )


def fixture_artifact(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "worker.py").write_text("source")
    config = build.deployment_config(build.normalized_config(CONFIG))
    (root / "wrangler.toml").write_text(config)
    bundle_hash = build.digest_tree(root)
    config_hash = hashlib.sha256(config.encode()).hexdigest()
    receipt = {
        "bundle_sha256": bundle_hash,
        "deployment_config_sha256": config_hash,
        "account_id": "a" * 32,
        "worker_name": "acorn-test",
        "source_commit": "a" * 40,
    }
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    return root, bundle_hash, config_hash


def test_artifact_and_config_tampering_fail_closed(tmp_path):
    root, bundle_hash, config_hash = fixture_artifact(tmp_path)
    build.verify_output(tmp_path, bundle_hash, config_hash)
    (root / "worker.py").write_text("tampered")
    with pytest.raises(ValueError, match="bundle digest"):
        build.verify_output(tmp_path, bundle_hash, config_hash)
    (root / "worker.py").write_text("source")
    (root / "wrangler.toml").write_text("modified")
    with pytest.raises(ValueError, match="configuration digest"):
        build.verify_output(tmp_path, bundle_hash, config_hash)


def test_attestation_rejects_changed_modules_bindings_or_split_version(tmp_path, monkeypatch):
    root, bundle_hash, config_hash = fixture_artifact(tmp_path)
    token = tmp_path / "token"
    token.write_text("synthetic-token")
    token.chmod(0o600)
    version = "1" * 8 + "-" + "2" * 4 + "-" + "3" * 4 + "-" + "4" * 4 + "-" + "5" * 12
    deployments = {
        "deployments": [{"created_on": "2026-01-01", "versions": [{"version_id": version, "percentage": 100}]}]
    }
    remote = {
        "id": version,
        "main_module": "worker.py",
        "compatibility_date": "2026-08-31",
        "compatibility_flags": ["python_workers"],
        "modules": [{"name": "worker.py", "content_base64": base64.b64encode(b"source").decode()}],
        "bindings": [
            {"name": "DB", "type": "d1", "database_id": "db-one"},
            {"name": "CARTRAY_ENVIRONMENT", "type": "plain_text", "text": "test"},
            *[
                {"name": name, "type": "secret_text"}
                for name in (
                    "STRIPE_API_KEY",
                    "STRIPE_WEBHOOK_SECRET",
                    "CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64",
                    "CARTRAY_PROJECTION_PUBLIC_KEYS_JSON",
                )
            ],
        ],
    }
    monkeypatch.setattr(
        build, "_cloudflare_json", lambda path, _: deployments if path.endswith("/deployments") else remote
    )
    assert build.attest_version(tmp_path, token, version, bundle_hash, config_hash)["verified_modules"] == 1
    secret = remote["bindings"].pop()
    with pytest.raises(ValueError, match="secret binding"):
        build.attest_version(tmp_path, token, version, bundle_hash, config_hash)
    remote["bindings"].append(secret)
    remote["modules"][0]["content_base64"] = base64.b64encode(b"changed").decode()
    with pytest.raises(ValueError, match="modules differ"):
        build.attest_version(tmp_path, token, version, bundle_hash, config_hash)
    remote["modules"][0]["content_base64"] = base64.b64encode(b"source").decode()
    remote["bindings"][0]["database_id"] = "wrong-db"
    with pytest.raises(ValueError, match="binding mismatch"):
        build.attest_version(tmp_path, token, version, bundle_hash, config_hash)
    remote["bindings"][0]["database_id"] = "db-one"
    deployments["deployments"][0]["versions"][0]["percentage"] = 50
    with pytest.raises(ValueError, match="version mismatch"):
        build.attest_version(tmp_path, token, version, bundle_hash, config_hash)
