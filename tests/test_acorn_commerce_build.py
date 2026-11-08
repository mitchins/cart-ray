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
    assert build.attest_version(tmp_path, token, version, bundle_hash, config_hash, "a" * 40)["verified_modules"] == 1
    secret = remote["bindings"].pop()
    with pytest.raises(ValueError, match="secret binding"):
        build.attest_version(tmp_path, token, version, bundle_hash, config_hash, "a" * 40)
    remote["bindings"].append(secret)
    remote["modules"][0]["content_base64"] = base64.b64encode(b"changed").decode()
    with pytest.raises(ValueError, match="modules differ"):
        build.attest_version(tmp_path, token, version, bundle_hash, config_hash, "a" * 40)
    remote["modules"][0]["content_base64"] = base64.b64encode(b"source").decode()
    remote["bindings"][0]["database_id"] = "wrong-db"
    with pytest.raises(ValueError, match="binding mismatch"):
        build.attest_version(tmp_path, token, version, bundle_hash, config_hash, "a" * 40)
    remote["bindings"][0]["database_id"] = "db-one"
    deployments["deployments"][0]["versions"][0]["percentage"] = 50
    with pytest.raises(ValueError, match="version mismatch"):
        build.attest_version(tmp_path, token, version, bundle_hash, config_hash, "a" * 40)


def test_attestation_rejects_receipt_identity_or_source_revision_tampering(tmp_path, monkeypatch):
    _, bundle_hash, config_hash = fixture_artifact(tmp_path)
    token = tmp_path / "token"
    token.write_text("synthetic-token")
    token.chmod(0o600)
    version = "11111111-2222-3333-4444-555555555555"
    receipt_path = tmp_path / "receipt.json"
    original = json.loads(receipt_path.read_text())
    monkeypatch.setattr(build, "_cloudflare_json", lambda *_: pytest.fail("must fail before network"))
    for field, value, message in (
        ("account_id", "b" * 32, "deployment identity"),
        ("worker_name", "other-worker", "deployment identity"),
        ("source_commit", "b" * 40, "source commit"),
    ):
        receipt_path.write_text(json.dumps({**original, field: value}))
        with pytest.raises(ValueError, match=message):
            build.attest_version(tmp_path, token, version, bundle_hash, config_hash, "a" * 40)


def test_private_token_rejects_symlinks_broad_permissions_and_oversize(tmp_path):
    token = tmp_path / "token"
    token.write_text("synthetic-token")
    token.chmod(0o600)
    assert build.read_private_token(token) == "synthetic-token"
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(OSError):
        build.read_private_token(link)
    token.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        build.read_private_token(token)
    token.chmod(0o600)
    token.write_text("x" * 4097)
    with pytest.raises(ValueError, match="too large"):
        build.read_private_token(token)
    with pytest.raises(ValueError, match="private regular file"):
        build.read_private_token(tmp_path)


def test_clean_build_checks_source_and_no_bundle_output(tmp_path, monkeypatch):
    import io
    import sys
    import tarfile

    archive = io.BytesIO()
    files = {
        "uv.lock": "[metadata]\n",
        "examples/acorn-store/catalogue.csv": "catalogue",
        "src/cartray/worker.py": "worker source",
        "src/cartray/compiled_catalogue.py": "catalogue",
    }
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    lock = tmp_path / "lock.json"
    lock.write_text("{}")
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        build.subprocess,
        "check_output",
        lambda args, **kwargs: "a" * 40 if args[1] == "rev-parse" else archive.getvalue(),
    )
    monkeypatch.setattr(build, "lock_seed", lambda lock: ("seed", "manifest"))
    monkeypatch.setattr(build, "verify_vendor", lambda project, lock: {"kinglet": "1.8.3"})
    state = {"tamper_source": False, "tamper_redeploy": False}

    def fake_run(args, project, env):
        if "--outdir" not in args:
            return
        out = Path(args[args.index("--outdir") + 1])
        out.mkdir(parents=True)
        if "--no-bundle" in args:
            (out / "worker.py").write_text("wrong" if state["tamper_redeploy"] else "bundled")
        else:
            (out / "worker.py").write_text("bundled")
            vendor = project / "python_modules/cartray"
            vendor.mkdir(parents=True)
            (vendor / "worker.py").write_text("wrong" if state["tamper_source"] else "worker source")
            (vendor / "compiled_catalogue.py").write_text("catalogue")

    monkeypatch.setattr(build, "run", fake_run)

    def invoke(output):
        monkeypatch.setattr(
            sys,
            "argv",
            ["build", "--preflight-lock", str(lock), "--wrangler-config", str(config), "--output", str(output)],
        )
        build.main()

    output = tmp_path / "good"
    invoke(output)
    receipt = json.loads((output / "receipt.json").read_text())
    build.verify_output(output, receipt["bundle_sha256"], receipt["deployment_config_sha256"])
    state["tamper_source"] = True
    with pytest.raises(RuntimeError, match="Worker source mismatch"):
        invoke(tmp_path / "bad-source")
    state["tamper_source"] = False
    state["tamper_redeploy"] = True
    with pytest.raises(RuntimeError, match="changed during no-bundle"):
        invoke(tmp_path / "bad-redeploy")


def test_locked_wheel_integrity_and_vendor_parity(tmp_path, monkeypatch):
    import io
    import zipfile

    vendor = tmp_path / "python_modules"
    bundle = tmp_path / "bundle/python_modules"
    vendor.mkdir(parents=True)
    bundle.mkdir(parents=True)
    for base in (vendor, bundle):
        (base / ".synced").write_text("")
        (base / "pyvenv.cfg").write_text("")
    wheels = {}
    lock = {"package": []}
    pylock = []
    for name in build.PACKAGES:
        package = name.replace("-", "_")
        dist = package + "-1.0.dist-info"
        contents = {
            package + "/__init__.py": b"# package\n",
            dist + "/METADATA": b"Metadata-Version: 2.1\nVersion: 1.0\n",
            dist + "/RECORD": b"original wheel metadata\n",
        }
        blob = io.BytesIO()
        with zipfile.ZipFile(blob, "w") as wheel:
            for path, data in contents.items():
                wheel.writestr(path, data)
        raw = blob.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        url = "https://example.test/" + name
        wheels[url] = raw
        lock["package"].append({"name": name, "version": "1.0", "wheels": [{"url": url, "hash": "sha256:" + digest}]})
        pylock.append(
            f'[[packages]]\nname = "{name}"\nversion = "1.0"\nwheels = [{{hashes = {{sha256 = "{digest}"}}}}]\n'
        )
        for base in (vendor, bundle):
            for path, data in contents.items():
                file = base / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data if not path.endswith("RECORD") else b"host-specific")
            for filename in ("INSTALLER", "REQUESTED"):
                (base / dist / filename).write_text("")
    (tmp_path / "pylock.toml").write_text("\n".join(pylock))
    monkeypatch.setattr(build.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(wheels[url]))
    assert build.verify_vendor(tmp_path, lock) == {name: "1.0" for name in build.PACKAGES}
    assert (vendor / "kinglet-1.0.dist-info/RECORD").read_bytes() == b"original wheel metadata\n"
    assert (bundle / "kinglet-1.0.dist-info/RECORD").read_bytes() == b"original wheel metadata\n"
    source = vendor / "kinglet/__init__.py"
    source.write_text("tampered")
    with pytest.raises(RuntimeError, match="vendored wheel file mismatch"):
        build.verify_vendor(tmp_path, lock)
    source.write_bytes(b"# package\n")
    (bundle / "kinglet/__init__.py").write_text("tampered")
    with pytest.raises(RuntimeError, match="bundle/vendor bytes mismatch"):
        build.verify_vendor(tmp_path, lock)
    (bundle / "kinglet/__init__.py").write_bytes(b"# package\n")
    wheels[next(iter(wheels))] = b"bad wheel"
    with pytest.raises(RuntimeError, match="locked wheel hash mismatch"):
        build.verify_vendor(tmp_path, lock)


def test_lock_seed_requires_wheel_hash_and_pins_versions():
    lock = {
        "package": [
            {
                "name": name,
                "version": "1.0",
                "wheels": [{"url": "https://example.test/wheel", "hash": "sha256:" + "a" * 64}],
            }
            for name in build.PACKAGES
        ]
    }
    seed, project = build.lock_seed(lock)
    assert "kinglet==1.0" in project
    assert "a" * 64 in seed
    lock["package"][0]["wheels"][0]["hash"] = "bad"
    with pytest.raises(ValueError, match="invalid locked wheel hash"):
        build.lock_seed(lock)
