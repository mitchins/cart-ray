"""Build an isolated, locked Acorn test Worker and retain its deployable artifact."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from importlib.metadata import PathDistribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("kinglet", "workers-runtime-sdk")


def digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(root) not in {Path("README.md"), Path("wrangler.toml")}
    ):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def lock_seed(lock: dict) -> tuple[str, str]:
    packages = {p["name"]: p for p in lock["package"]}
    seed = ['lock-version = "1.0"', 'created-by = "uv"', 'requires-python = ">=3.13.2"']
    dependencies = []
    for name in PACKAGES:
        package = packages[name]
        wheel = package["wheels"][0]
        sha = wheel["hash"].removeprefix("sha256:")
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("invalid locked wheel hash")
        seed.extend(
            [
                "",
                "[[packages]]",
                f"name = {json.dumps(name)}",
                f"version = {json.dumps(package['version'])}",
                f"wheels = [{{ url = {json.dumps(wheel['url'])}, hashes = {{ sha256 = {json.dumps(sha)} }} }}]",
            ]
        )
        dependencies.append(f"{name}=={package['version']}")
    project = (
        '[project]\nname = "acorn-commerce-build"\nversion = "0.0.0"\nrequires-python = ">=3.13.2"\n'
        f"dependencies = {json.dumps(dependencies)}\n"
    )
    return "\n".join(seed) + "\n", project


def checked_config(raw: str) -> dict:
    config = tomllib.loads(raw)
    if (
        not str(config.get("main", "")).endswith("src/cartray/worker.py")
        or config.get("vars", {}).get("CARTRAY_ENVIRONMENT") != "test"
    ):
        raise ValueError("expected a test-only CartRay Worker configuration")
    build = config.get("build", {})
    if build.get("command") != "uv run python scripts/sync_worker_source.py" or set(build) - {"command", "cwd"}:
        raise ValueError("unexpected Worker build command")
    if not config.get("name") or not config.get("account_id"):
        raise ValueError("Worker and account identity are required")
    if any(re.search(r"SECRET|PASSWORD|PRIVATE|API_KEY|TOKEN", name, re.I) for name in config.get("vars", {})):
        raise ValueError("sensitive values must not be stored in Worker vars")
    dbs = [item for item in config.get("d1_databases", []) if item.get("binding") == "DB"]
    if len(dbs) != 1 or not dbs[0].get("database_id"):
        raise ValueError("exactly one identified commerce D1 binding is required")
    return config


def normalized_config(raw: str) -> str:
    checked_config(raw)
    raw, main_count = re.subn(r'^main\s*=\s*"[^"\n]*"\s*$', 'main = "src/cartray/worker.py"', raw, flags=re.M)
    raw, build_count = re.subn(
        r"^\[build\][^\n]*\n(?:[^\[\n][^\n]*\n|\n)*",
        '[build]\ncommand = "uv run python scripts/sync_worker_source.py"\n\n',
        raw,
        flags=re.M,
    )
    if main_count != 1 or build_count != 1:
        raise ValueError("cannot normalize Worker configuration")
    checked_config(raw)
    return raw


def deployment_config(raw: str) -> str:
    raw, n = re.subn(r'^main\s*=\s*"src/cartray/worker.py"\s*$', 'main = "worker.py"', raw, flags=re.M)
    raw, b = re.subn(
        r'^\[build\]\s*\ncommand\s*=\s*"uv run python scripts/sync_worker_source.py"\s*\n', "", raw, flags=re.M
    )
    if n != 1 or b != 1:
        raise ValueError("cannot safely derive deployment config")
    return raw


def verify_vendor(project: Path, lock: dict) -> dict:
    locked = {p["name"]: p for p in lock["package"]}
    actual = tomllib.loads((project / "pylock.toml").read_text())["packages"]
    versions = {p["name"]: p["version"] for p in actual}
    if set(versions) != set(PACKAGES) or any(locked[n]["version"] != v for n, v in versions.items()):
        raise RuntimeError("vendored dependency versions differ from uv.lock")
    vendor, bundle = project / "python_modules", project / "bundle/python_modules"
    expected = {Path(".synced"), Path("pyvenv.cfg")}
    for name, version in versions.items():
        wheel = locked[name]["wheels"][0]
        wanted = wheel["hash"].removeprefix("sha256:")
        recorded = {w["hashes"]["sha256"] for p in actual if p["name"] == name for w in p["wheels"]}
        if not recorded or recorded != {wanted}:
            raise RuntimeError(f"vendored lock artifact mismatch: {name}")
        metadata = list(vendor.glob(f"{name.replace('-', '_')}-*.dist-info"))
        if len(metadata) != 1 or PathDistribution(metadata[0]).version != version:
            raise RuntimeError(f"vendored package metadata missing: {name}")
        with urllib.request.urlopen(wheel["url"], timeout=30) as response:
            raw = response.read()
        if hashlib.sha256(raw).hexdigest() != wanted:
            raise RuntimeError(f"locked wheel hash mismatch: {name}")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                relative = Path(entry.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise RuntimeError("unsafe wheel path")
                expected.add(relative)
                if entry.filename.endswith(".dist-info/RECORD"):
                    # Installers add a host-specific executable hash; restore the
                    # locked wheel's metadata to make the retained bundle stable.
                    (vendor / relative).write_bytes(archive.read(entry))
                    (bundle / relative).write_bytes(archive.read(entry))
                else:
                    installed = vendor / relative
                    if not installed.is_file() or installed.read_bytes() != archive.read(entry):
                        raise RuntimeError(f"vendored wheel file mismatch: {relative}")
        expected.update(metadata[0].relative_to(vendor) / n for n in ("INSTALLER", "REQUESTED"))
    # The CartRay package is synchronized after vendoring and separately checked below.
    vendor_files = {
        p.relative_to(vendor) for p in vendor.rglob("*") if p.is_file() and p.relative_to(vendor).parts[0] != "cartray"
    }
    if vendor_files != expected:
        raise RuntimeError("unexpected vendored dependency files")
    all_vendor = {p.relative_to(vendor) for p in vendor.rglob("*") if p.is_file()}
    if all_vendor != {p.relative_to(bundle) for p in bundle.rglob("*") if p.is_file()}:
        raise RuntimeError("bundle/vendor file set mismatch")
    if any((vendor / p).read_bytes() != (bundle / p).read_bytes() for p in all_vendor):
        raise RuntimeError("bundle/vendor bytes mismatch")
    return versions


def run(args: list[str], project: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=project, env=env, check=True)


def verify_output(path: Path, expected_bundle: str, expected_config: str) -> None:
    if not all(re.fullmatch(r"[a-f0-9]{64}", value) for value in (expected_bundle, expected_config)):
        raise ValueError("expected digests must be SHA-256")
    root = path / "bundle"
    if root.is_symlink() or any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("symlink in deployment artifact")
    if digest_tree(root) != expected_bundle:
        raise ValueError("deployment bundle digest mismatch")
    if hashlib.sha256((root / "wrangler.toml").read_bytes()).hexdigest() != expected_config:
        raise ValueError("deployment configuration digest mismatch")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args):
        raise ValueError("control plane redirect rejected")


def _cloudflare_json(path: str, token: str) -> dict:
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/" + path, headers={"Authorization": "Bearer " + token}
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
        raw = response.read(40 * 1024 * 1024 + 1)
    if len(raw) > 40 * 1024 * 1024:
        raise ValueError("control plane response too large")
    data = json.loads(raw)
    if data.get("success") is not True or not isinstance(data.get("result"), dict):
        raise ValueError("control plane response unavailable")
    return data["result"]


def attest_version(
    path: Path, token_file: Path, expected_version: str, expected_bundle: str, expected_config: str
) -> dict:
    if not re.fullmatch(r"[a-f0-9-]{36}", expected_version):
        raise ValueError("invalid expected version")
    if token_file.stat().st_mode & 0o077:
        raise ValueError("token file must be private")
    token = token_file.read_text().strip()
    if not token:
        raise ValueError("missing control plane token")
    receipt = json.loads((path / "receipt.json").read_text())
    if receipt["bundle_sha256"] != expected_bundle or receipt["deployment_config_sha256"] != expected_config:
        raise ValueError("receipt differs from independently retained digests")
    verify_output(path, expected_bundle, expected_config)
    config = tomllib.loads((path / "bundle/wrangler.toml").read_text())
    account, worker = receipt["account_id"], receipt["worker_name"]
    if not re.fullmatch(r"[a-f0-9]{32}", account) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", worker):
        raise ValueError("invalid account or Worker identity")
    prefix = f"accounts/{account}/workers"
    deployments = _cloudflare_json(f"{prefix}/scripts/{worker}/deployments", token)["deployments"]
    if not deployments:
        raise ValueError("no active deployment")
    latest = max(deployments, key=lambda item: item["created_on"])
    if latest.get("versions") != [{"version_id": expected_version, "percentage": 100}]:
        raise ValueError("active Worker version mismatch or split deployment")
    remote = _cloudflare_json(f"{prefix}/workers/{worker}/versions/{expected_version}?include=modules", token)
    if (
        remote.get("id") != expected_version
        or remote.get("main_module") != "worker.py"
        or remote.get("compatibility_date") != config.get("compatibility_date")
        or set(remote.get("compatibility_flags", [])) != set(config.get("compatibility_flags", []))
    ):
        raise ValueError("deployed Worker metadata mismatch")
    root = path / "bundle"
    local = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).digest()
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(root) not in {Path("README.md"), Path("wrangler.toml")}
    }
    modules = remote.get("modules")
    if not isinstance(modules, list):
        raise ValueError("deployed modules unavailable")
    observed = {}
    for module in modules:
        name = module.get("name")
        if not isinstance(name, str) or name in observed:
            raise ValueError("invalid deployed module")
        observed[name] = hashlib.sha256(base64.b64decode(module["content_base64"], validate=True)).digest()
    if observed != local:
        raise ValueError("deployed Worker modules differ from retained artifact")
    bindings = remote.get("bindings", [])
    required_secrets = {
        "STRIPE_API_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "CARTRAY_SIGNING_PRIVATE_KEY_PKCS8_B64",
        "CARTRAY_PROJECTION_PUBLIC_KEYS_JSON",
    } | set(config.get("secrets", {}).get("required", []))
    if any(service.get("binding") == "REVIEWS_SOURCE_TRIGGER" for service in config.get("services", [])):
        required_secrets.update({"CARTRAY_REVIEWS_IDENTITY_TOKEN", "CARTRAY_REVIEWS_TRIGGER_TOKEN"})
    secret_names = [b.get("name") for b in bindings if b.get("type") == "secret_text"]
    if len(secret_names) != len(set(secret_names)) or not required_secrets.issubset(secret_names):
        raise ValueError("required deployed secret binding missing or ambiguous")
    expected = {}
    for name, value in config.get("vars", {}).items():
        if not isinstance(value, str):
            raise ValueError("only plain-text vars are supported by attestation")
        expected[name] = ("plain_text", {"text": value})
    for db in config.get("d1_databases", []):
        expected[db["binding"]] = ("d1", {"database_id": db["database_id"]})
    for service in config.get("services", []):
        if service.get("environment") not in (None, "", "production") or service.get("entrypoint"):
            raise ValueError("non-default service binding unsupported")
        expected[service["binding"]] = ("service", {"service": service["service"]})
    for rate in config.get("ratelimits", []):
        expected[rate["name"]] = ("ratelimit", {"namespace_id": str(rate["namespace_id"]), "simple": rate["simple"]})
    if config.get("version_metadata"):
        expected[config["version_metadata"]["binding"]] = ("version_metadata", {})
    nonsecret = [b for b in bindings if b.get("type") != "secret_text"]
    if len(nonsecret) != len(expected) or {b.get("name") for b in nonsecret} != set(expected):
        raise ValueError("deployed binding set differs from configuration")
    for binding in nonsecret:
        kind, fields = expected[binding["name"]]
        if binding.get("type") != kind or any(binding.get(k) != v for k, v in fields.items()):
            raise ValueError("deployed binding mismatch: " + binding["name"])
        if kind == "service" and (
            binding.get("environment") not in (None, "", "production") or binding.get("entrypoint") not in (None, "")
        ):
            raise ValueError("deployed service target mismatch")
    return {
        "source_commit": receipt["source_commit"],
        "worker": worker,
        "account": account,
        "version": expected_version,
        "bundle_sha256": receipt["bundle_sha256"],
        "deployment_config_sha256": receipt["deployment_config_sha256"],
        "verified_modules": len(observed),
        "verified_nonsecret_bindings": len(nonsecret),
        "verified_required_secret_bindings": len(required_secrets),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-lock", type=Path)
    parser.add_argument("--wrangler-config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-output", type=Path)
    parser.add_argument("--expected-bundle-sha256")
    parser.add_argument("--expected-config-sha256")
    parser.add_argument("--attest-output", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--control-plane-token-file", type=Path)
    args = parser.parse_args()
    if args.attest_output:
        if (
            not args.version
            or not args.control_plane_token_file
            or not args.expected_bundle_sha256
            or not args.expected_config_sha256
        ):
            parser.error("attestation requires --version, --control-plane-token-file and both expected digests")
        print(
            json.dumps(
                attest_version(
                    args.attest_output,
                    args.control_plane_token_file,
                    args.version,
                    args.expected_bundle_sha256,
                    args.expected_config_sha256,
                ),
                indent=2,
            )
        )
        return
    if args.verify_output:
        if not args.expected_bundle_sha256 or not args.expected_config_sha256:
            parser.error("verification requires both independently retained digests")
        verify_output(args.verify_output, args.expected_bundle_sha256, args.expected_config_sha256)
        print("verified deployment bundle and configuration")
        return
    if not args.preflight_lock or not args.wrangler_config or not args.output:
        parser.error("build requires --preflight-lock, --wrangler-config and --output")
    if args.output.exists():
        raise SystemExit("output must not exist")
    subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    lock_bytes = args.preflight_lock.read_bytes()
    input_config = args.wrangler_config.read_text()
    config_raw = normalized_config(input_config)
    config = checked_config(config_raw)
    env = os.environ.copy()
    env["CARTRAY_CATALOGUE_PROFILE"] = "acorn-stripe-test"
    with tempfile.TemporaryDirectory(prefix="acorn-commerce-clean-") as tmp:
        project = Path(tmp)
        archive = subprocess.check_output(["git", "archive", commit], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as src:
            src.extractall(project, filter="data")
        private = project / "examples/acorn-store/.private"
        private.mkdir(mode=0o700)
        (private / "stripe-test-preflight.lock.json").write_bytes(lock_bytes)
        run(["uv", "sync", "--locked", "--all-groups"], project, env)
        run(["npm", "ci", "--ignore-scripts"], project, env)
        lock = tomllib.loads((project / "uv.lock").read_text())
        seed, manifest = lock_seed(lock)
        (project / "pylock.toml").write_text(seed)
        (project / "pyproject.toml").write_text(manifest)
        (project / "acorn-build.toml").write_text(config_raw)
        run(
            [
                str(project / ".venv/bin/pywrangler"),
                "deploy",
                "--config",
                "acorn-build.toml",
                "--dry-run",
                "--outdir",
                str(project / "bundle"),
            ],
            project,
            env,
        )
        versions = verify_vendor(project, lock)
        source = project / "src/cartray"
        vendor = project / "python_modules/cartray"
        source_files = {p.relative_to(source) for p in source.rglob("*.py")}
        vendor_files = {p.relative_to(vendor) for p in vendor.rglob("*.py")}
        if source_files != vendor_files:
            raise RuntimeError("Worker source files differ from vendored package")
        for path in source_files - {Path("compiled_catalogue.py")}:
            if (source / path).read_bytes() != (vendor / path).read_bytes():
                raise RuntimeError(f"Worker source mismatch: {path}")
        # The locked profile compiler independently verifies catalogue version and exact lookup keys.
        run(
            [
                str(project / ".venv/bin/python"),
                str(project / "scripts/compile_catalogue.py"),
                "--catalogue",
                str(project / "examples/acorn-store/catalogue.csv"),
                "--fulfilment-expansions",
                str(project / "examples/acorn-store/fulfilment-expansions.json"),
                "--presentation",
                str(project / "examples/acorn-store/catalogue-presentation.csv"),
                "--product-assets",
                str(project / "examples/acorn-store/assets/products"),
                "--preflight-lock",
                str(private / "stripe-test-preflight.lock.json"),
                "--output",
                str(vendor / "compiled_catalogue.py"),
                "--check",
            ],
            project,
            env,
        )
        args.output.mkdir(mode=0o700, parents=True)
        shutil.copytree(project / "bundle", args.output / "bundle")
        (args.output / "bundle/wrangler.toml").write_text(deployment_config(config_raw))
        # Prove Wrangler uploads the retained layout without a hidden rebuild.
        run(
            [
                str(project / "node_modules/.bin/wrangler"),
                "deploy",
                "--no-bundle",
                "--dry-run",
                "--config",
                str(args.output.resolve() / "bundle/wrangler.toml"),
                "--outdir",
                str(project / "redeploy"),
            ],
            project,
            env,
        )
        if digest_tree(project / "redeploy") != digest_tree(project / "bundle"):
            raise RuntimeError("retained deployment artifact changed during no-bundle dry run")
        receipt = {
            "source_commit": commit,
            "profile": "acorn-stripe-test",
            "versions": versions,
            "catalogue_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
            "input_configuration_sha256": hashlib.sha256(input_config.encode()).hexdigest(),
            "configuration_sha256": hashlib.sha256(config_raw.encode()).hexdigest(),
            "worker_name": config["name"],
            "account_id": config["account_id"],
            "commerce_d1_id": next(x["database_id"] for x in config["d1_databases"] if x["binding"] == "DB"),
            "bundle_sha256": digest_tree(project / "bundle"),
            "deployment_config_sha256": hashlib.sha256(deployment_config(config_raw).encode()).hexdigest(),
        }
        (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
