"""Build a disposable, source-verified Reviews Worker bundle from a Git commit."""

import hashlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from importlib.metadata import PathDistribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCES = (
    "worker.py",
    "native_lifecycle.py",
    "cartray_reviews/__init__.py",
    "cartray_reviews/acquisition.py",
    "cartray_reviews/invitations.py",
    "cartray_reviews/management.py",
    "cartray_reviews/recovery.py",
    "cartray_reviews/snapshot.py",
    "cartray_reviews/storage.py",
)


def run(args, cwd, **kwargs):
    """Run a required build step and retain any failure."""
    return subprocess.run(args, cwd=cwd, check=True, **kwargs)


def exercise_worker(project):
    """Execute Python modules from the built bundle directly under Workerd."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    bundle = project / "bundle"
    main = bundle / "worker.py"
    modules = [main, *sorted(path for path in bundle.rglob("*.py") if path != main)]
    entries = [
        f"(name = {json.dumps(str(path.relative_to(bundle)))}, "
        f"pythonModule = embed {json.dumps(str(path.relative_to(project)))})"
        for path in modules
    ]
    config = project / "direct-qualification.capnp"
    config.write_text(
        'using Workerd = import "/workerd/workerd.capnp";\n'
        'const config :Workerd.Config = (services = [(name = "main", worker = (modules = ['
        + ",".join(entries)
        + '], compatibilityDate = "2026-08-31", '
        'compatibilityFlags = ["python_workers", "disable_python_external_sdk"], '
        'bindings = [(name = "REVIEWS_MODE", text = "disabled")]))], '
        f'sockets = [(name = "http", address = "127.0.0.1:{port}", http = (), service = "main")]);'
    )
    process = subprocess.Popen(
        [str(project / "node_modules/.bin/workerd"), "serve", str(config)],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("isolated Reviews Workerd exited before readiness")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/reviews/v1/subjects/test/summary", timeout=1)
            except urllib.error.HTTPError as error:
                if error.code == 404 and json.loads(error.read()) == {"error": "not found"}:
                    return
                raise RuntimeError(f"isolated Reviews Workerd returned HTTP {error.code}") from error
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.25)
        raise RuntimeError("isolated Reviews Workerd did not become ready")
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


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
        # pywrangler derives its vendoring requirements from the project
        # manifest. The repository-root manifest belongs to commerce and would
        # silently add kinglet back to the independent Reviews deployment.
        sdk_version = locked["workers-runtime-sdk"]["version"]
        (project / "pyproject.toml").write_text(
            '[project]\nname = "cartray-reviews-qualification"\nversion = "0.0.0"\n'
            'requires-python = ">=3.13.2"\n'
            f'dependencies = ["workers-runtime-sdk=={sdk_version}"]\n'
        )
        config = (project / "modules/reviews/wrangler.test.toml").read_text()
        config = config.replace('main = "src/worker.py"', 'main = "modules/reviews/src/worker.py"')
        config = config.replace('migrations_dir = "migrations"', 'migrations_dir = "modules/reviews/migrations"')
        (project / "qualification.toml").write_text(config)
        run(
            [
                str(project / ".venv/bin/pywrangler"),
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
        expected_vendor_files = {Path(".synced"), Path("pyvenv.cfg")}
        for package_name, version in versions.items():
            normalized = package_name.replace("-", "_")
            metadata = list(vendor.glob(f"{normalized}-*.dist-info"))
            if len(metadata) != 1 or PathDistribution(metadata[0]).version != version:
                raise RuntimeError(
                    f"vendored package metadata does not match: {package_name}; "
                    "check that the Pyodide interpreter runs under the selected Node version"
                )
            locked_wheel = locked[package_name]["wheels"][0]
            with urllib.request.urlopen(locked_wheel["url"], timeout=30) as response:
                wheel_bytes = response.read()
            if hashlib.sha256(wheel_bytes).hexdigest() != locked_wheel["hash"].removeprefix("sha256:"):
                raise RuntimeError(f"locked wheel hash mismatch: {package_name}")
            with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as wheel:
                for entry in wheel.infolist():
                    if entry.is_dir():
                        continue
                    parts = Path(entry.filename).parts
                    if not parts or ".." in parts or Path(entry.filename).is_absolute():
                        raise RuntimeError("unsafe wheel entry")
                    expected_vendor_files.add(Path(entry.filename))
                    if entry.filename.endswith(".dist-info/RECORD"):
                        continue  # installers rewrite RECORD to describe the installation
                    installed = vendor / entry.filename
                    if not installed.is_file() or installed.read_bytes() != wheel.read(entry):
                        raise RuntimeError(f"vendored file differs from locked wheel: {entry.filename}")
            expected_vendor_files.update(
                {metadata[0].relative_to(vendor) / "INSTALLER", metadata[0].relative_to(vendor) / "REQUESTED"}
            )
        if not (vendor / "workers/__init__.py").is_file():
            raise RuntimeError("Workers SDK Python module missing from vendor directory")
        vendor_files = {path.relative_to(vendor) for path in vendor.rglob("*") if path.is_file()}
        bundle_files = {path.relative_to(bundled_vendor) for path in bundled_vendor.rglob("*") if path.is_file()}
        if vendor_files != expected_vendor_files:
            raise RuntimeError("vendored file set differs from locked wheel and installer metadata")
        if vendor_files != bundle_files:
            raise RuntimeError("vendored file set differs from bundle")
        for relative in vendor_files:
            if (vendor / relative).read_bytes() != (bundled_vendor / relative).read_bytes():
                raise RuntimeError(f"vendored file changed in bundle: {relative}")
        exercise_worker(project)
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
        deployable_hash = hashlib.sha256()
        for path in sorted(path for path in (project / "bundle").rglob("*") if path.is_file()):
            relative = path.relative_to(project / "bundle")
            entry = str(relative).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest()
            bundle_hash.update(entry)
            # Wrangler generates this local explanation with a timestamp on
            # each dry run. It is not a Worker module or deployment asset.
            if relative != Path("README.md"):
                deployable_hash.update(entry)
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
                    "deployable_content_sha256": deployable_hash.hexdigest(),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
