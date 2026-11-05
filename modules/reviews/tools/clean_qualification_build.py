"""Build a disposable, source-verified Reviews Worker bundle from a Git commit."""

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from importlib.metadata import PathDistribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCES = (
    "worker.py",
    "native_lifecycle.py",
    "cartray_reviews/__init__.py",
    "cartray_reviews/acquisition.py",
    "cartray_reviews/invitations.py",
    "cartray_reviews/snapshot.py",
    "cartray_reviews/storage.py",
)


def run(args, cwd, **kwargs):
    """Run a required build step and retain any failure."""
    return subprocess.run(args, cwd=cwd, check=True, **kwargs)


def main():
    """Archive HEAD, recreate generated artifacts, and compare every runtime module."""
    run(["git", "diff", "--quiet", "HEAD", "--", "modules/reviews/src"], ROOT)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="cartray-reviews-build-") as name:
        project = Path(name)
        archive = subprocess.check_output(["git", "archive", commit], cwd=ROOT)
        with tempfile.TemporaryFile() as stream:
            stream.write(archive)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as tar:
                tar.extractall(project, filter="data")
        # These paths are generated solely within this fresh temporary project.
        for name in ("python_modules", ".venv-workers", ".wrangler"):
            generated = project / name
            if generated.is_symlink():
                raise RuntimeError(f"refusing symlinked generated directory: {name}")
            if generated.exists():
                shutil.rmtree(generated)
        run(["uv", "sync", "--locked", "--all-groups"], project)
        run(["npm", "ci", "--ignore-scripts"], project)
        uv_lock = tomllib.loads((project / "uv.lock").read_text())
        locked = {package["name"]: package for package in uv_lock["package"]}
        seed = ['lock-version = "1.0"', 'created-by = "uv"', 'requires-python = ">=3.13.2"']
        # Reviews imports `workers`, provided by this SDK. Kinglet belongs to
        # CartRay's commerce Worker and is not a Reviews runtime dependency.
        for package_name in ("workers-runtime-sdk",):
            package = locked[package_name]
            wheel = package["wheels"][0]
            sha256 = wheel["hash"].removeprefix("sha256:")
            seed.extend(
                [
                    "",
                    "[[packages]]",
                    f"name = {json.dumps(package_name)}",
                    f"version = {json.dumps(package['version'])}",
                    f"wheels = [{{ url = {json.dumps(wheel['url'])}, hashes = {{ sha256 = {json.dumps(sha256)} }} }}]",
                ]
            )
        (project / "pylock.toml").write_text("\n".join(seed) + "\n")
        config = (project / "modules/reviews/wrangler.test.toml").read_text()
        config = config.replace('main = "src/worker.py"', 'main = "modules/reviews/src/worker.py"')
        config = config.replace('migrations_dir = "migrations"', 'migrations_dir = "modules/reviews/migrations"')
        (project / "qualification.toml").write_text(config)
        run(
            [
                "uv",
                "run",
                "--locked",
                "pywrangler",
                "deploy",
                "--config",
                "qualification.toml",
                "--dry-run",
                "--outdir",
                str(project / "bundle"),
            ],
            project,
        )
        vendor_lock = tomllib.loads((project / "pylock.toml").read_text())
        versions = {package["name"]: package["version"] for package in vendor_lock["packages"]}
        if set(versions) != {"workers-runtime-sdk"} or any(
            name not in locked or locked[name]["version"] != version for name, version in versions.items()
        ):
            raise RuntimeError("vendored dependency versions do not match uv.lock")
        for package in vendor_lock["packages"]:
            expected = {wheel["hash"].removeprefix("sha256:") for wheel in locked[package["name"]]["wheels"]}
            actual = {wheel["hashes"]["sha256"] for wheel in package["wheels"]}
            if not actual or not actual <= expected:
                raise RuntimeError(f"vendored artifact hashes do not match uv.lock: {package['name']}")
        vendor = project / "python_modules"
        bundled_vendor = project / "bundle/python_modules"
        for package_name, version in versions.items():
            normalized = package_name.replace("-", "_")
            metadata = list(vendor.glob(f"{normalized}-*.dist-info"))
            if len(metadata) != 1 or PathDistribution(metadata[0]).version != version:
                raise RuntimeError(
                    f"vendored package metadata does not match: {package_name}; "
                    "check that the Pyodide interpreter runs under the selected Node version"
                )
        if not (vendor / "workers/__init__.py").is_file():
            raise RuntimeError("Workers SDK Python module missing from vendor directory")
        for path in (path for path in vendor.rglob("*") if path.is_file()):
            counterpart = bundled_vendor / path.relative_to(vendor)
            if not counterpart.is_file() or path.read_bytes() != counterpart.read_bytes():
                raise RuntimeError(f"vendored file missing or changed in bundle: {path.relative_to(vendor)}")
        hashes = {}
        for relative in SOURCES:
            source = (project / "modules/reviews/src" / relative).read_bytes()
            bundled = (project / "bundle" / relative).read_bytes()
            if source != bundled:
                raise RuntimeError(f"stale or missing bundled source: {relative}")
            hashes[relative] = hashlib.sha256(bundled).hexdigest()
        if not (project / "bundle/python_modules").is_dir():
            raise RuntimeError("vendored Python modules absent from bundle")
        bundle_hash = hashlib.sha256()
        for path in sorted(path for path in (project / "bundle").rglob("*") if path.is_file()):
            bundle_hash.update(str(path.relative_to(project / "bundle")).encode() + b"\0")
            bundle_hash.update(hashlib.sha256(path.read_bytes()).digest())
        workerd = subprocess.check_output(
            [str(project / "node_modules/.bin/workerd"), "--version"], cwd=project, text=True
        ).strip()
        wrangler = subprocess.check_output(
            [str(project / "node_modules/.bin/wrangler"), "--version"], cwd=project, text=True
        ).strip()
        print(
            json.dumps(
                {
                    "source_commit": commit,
                    "wrangler": wrangler,
                    "workerd": workerd,
                    "vendored_versions": versions,
                    "runtime_source_sha256": hashes,
                    "bundle_content_sha256": bundle_hash.hexdigest(),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
