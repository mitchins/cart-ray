"""Create, qualify and delete isolated synthetic Cloudflare resources.

The source Worker replays a captured local R4 public projection; it is not a
hosted deployment of the R4 Python runtime. No production resource is used.
"""

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/reviews/publication"
WRANGLER = str(ROOT / "node_modules/.bin/wrangler")


def command(args, *, env, cwd, stdin=None, timeout=180):
    result = subprocess.run(
        [WRANGLER, *args], cwd=cwd, env=env, input=stdin, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        diagnostic = "\n".join(
            line[:300] for line in (result.stdout + result.stderr).splitlines() if "ERROR" in line.upper()
        )
        raise RuntimeError(f"wrangler {args[0]} failed: {diagnostic or 'no error detail'}")
    return result.stdout if "--json" in args else result.stdout + result.stderr


def directory_digest(path):
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise RuntimeError("deployed bundle output is empty")
    for item in files:
        relative = item.relative_to(path).as_posix().encode()
        content = item.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: hosted_qualification.py <captured-synthetic-r4-projection.json>")
    fixture_path = Path(sys.argv[1])
    fixture_bytes = fixture_path.read_bytes()
    fixture = json.loads(fixture_bytes)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if (
        fixture["subject"]["id"] != "example-policy"
        or fixture["summary"]["snapshot_id"] != "one"
        or fixture["summary"]["contribution_count"] != 3
        or {item["id"] for item in fixture["pages"][0]["contributions"]} != {"r", "r2", "u"}
    ):
        raise RuntimeError("unexpected fixture; synthetic R4 data required")
    capture_digest = hashlib.sha256(
        json.dumps(fixture, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    if capture_digest != "18aa6d1266a8ccf81d34a615863ea622d16f662988ee28dfbe786bbce2bdee7e":
        raise RuntimeError("fixture differs from the pinned synthetic R4 public projection")
    expected = json.loads((ROOT / "modules/reviews/tests/fixtures/approved-synthetic.json").read_text())
    expected_rows = {item["id"]: (item["author"], item["body"], item["rating"]) for item in expected["contributions"]}

    def flatten(items):
        return [nested for item in items for nested in [item, *flatten(item.get("replies", []))]]

    actual_rows = {
        item["id"]: (item["author"], item["body"], item["rating"])
        for item in flatten(fixture["pages"][0]["contributions"])
    }
    if actual_rows != expected_rows:
        raise RuntimeError("fixture content does not match the checked-in synthetic source")
    suffix = secrets.token_hex(6)
    source_name = f"r5-src-{suffix}"
    gate_name = f"r5-gate-{suffix}"
    database_name = f"r5-db-{suffix}"
    token = secrets.token_hex(32)
    resources = {"source": False, "gate": False, "database": False}
    retain_seconds = int(os.environ.get("R5_DIAGNOSTIC_RETAIN_SECONDS", "0"))
    if not 0 <= retain_seconds <= 120:
        raise RuntimeError("diagnostic retention must be between 0 and 120 seconds")
    diagnostics = Path(tempfile.mkdtemp(prefix="r5-hosted-diagnostics-"))
    print(f"Non-secret diagnostic output: {diagnostics}", flush=True)
    with tempfile.TemporaryDirectory(prefix="r5-hosted-qualification-") as temporary:
        temp = Path(temporary)
        frozen = {}
        for relative in (
            "worker.mjs",
            "projection.mjs",
            "schema.sql",
            "tests/synthetic_source.mjs",
            "tests/qualify.mjs",
            "tests/stalled_upload.mjs",
            "tests/source_health.mjs",
            "tests/hosted_qualification.py",
        ):
            repository_path = f"modules/reviews/publication/{relative}"
            committed = subprocess.check_output(["git", "show", f"{commit}:{repository_path}"], cwd=ROOT)
            if (ROOT / repository_path).read_bytes() != committed:
                raise RuntimeError("publication source differs from the pinned commit")
            destination = temp / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(committed)
            frozen[relative] = hashlib.sha256(committed).hexdigest()
        (temp / "fixture.json").write_bytes(fixture_bytes)
        manifest = temp / "disposable-resources.json"
        manifest.write_text(
            json.dumps({"source_worker": source_name, "gate_worker": gate_name, "database": database_name})
        )
        manifest.chmod(0o600)
        print(f"Disposable resource recovery manifest: {manifest}", flush=True)
        env = os.environ.copy()
        identity = command(["whoami"], env=env, cwd=ROOT)
        login = re.search(r"associated with the email ([^\s.]+(?:\.[^\s.]+)+)", identity, re.I)
        if not login:
            raise RuntimeError("cannot identify signed-in personal account")
        rows = [
            line
            for line in identity.splitlines()
            if login.group(1).lower() in line.lower() and re.search(r"\b[0-9a-f]{32}\b", line)
        ]
        if len(rows) != 1:
            raise RuntimeError("personal account is ambiguous")
        env["CLOUDFLARE_ACCOUNT_ID"] = re.search(r"\b[0-9a-f]{32}\b", rows[0]).group(0)
        source_config = temp / "source.toml"
        gate_config = temp / "gate.toml"
        source_url = None
        receipt = None
        cleanup_errors = []
        try:
            resources["database"] = True
            output = command(["d1", "create", database_name], env=env, cwd=ROOT)
            found = re.search(r'database_id\s*=\s*["\']([0-9a-f-]{36})["\']', output)
            if not found:
                raise RuntimeError("could not identify newly created test database")
            database_id = found.group(1)
            (temp / "synthetic_source.mjs").write_bytes((temp / "tests/synthetic_source.mjs").read_bytes())
            (temp / "synthetic_source_data.mjs").write_text(
                "export default " + json.dumps(fixture, separators=(",", ":")) + ";\n"
            )
            source_config.write_text(
                f'name = "{source_name}"\n'
                'main = "synthetic_source.mjs"\ncompatibility_date = "2026-08-31"\nworkers_dev = true\n'
                f'[vars]\nR5_BUILD_ID = "{frozen["tests/synthetic_source.mjs"]}"\n'
            )
            resources["source"] = True
            output = command(
                ["deploy", "--config", str(source_config), "--outdir", str(temp / "source-bundle")], env=env, cwd=temp
            )
            (diagnostics / "source-deploy.txt").write_text(output)
            found = re.search(r"https://r5-src-[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", output)
            if not found:
                raise RuntimeError("source Worker URL unavailable")
            source_url = found.group(0)
            health = subprocess.run(
                ["node", str(temp / "tests/source_health.mjs"), source_url + "/__test/health",
                 frozen["tests/synthetic_source.mjs"]],
                capture_output=True, text=True, timeout=40, check=False, env=env,
            )
            (diagnostics / "source-health.json").write_text(health.stdout + health.stderr)
            if health.returncode:
                raise RuntimeError("synthetic source health did not match deployed build; see diagnostic output")
            gate_config.write_text(
                f'name = "{gate_name}"\n'
                'main = "worker.mjs"\ncompatibility_date = "2026-08-31"\nworkers_dev = true\n'
                '[vars]\nPUBLICATION_MODE = "test"\nPUBLICATION_ENVIRONMENT = "test"\n'
                'PUBLICATION_CANONICAL_ORIGIN = "https://example.invalid"\n'
                f'REVIEWS_PUBLIC_ORIGIN = "{source_url}"\nREVIEWS_SOURCE_SCOPE = "example-store:test"\n'
                '[[d1_databases]]\nbinding = "PUBLICATION_DB"\n'
                f'database_name = "{database_name}"\ndatabase_id = "{database_id}"\n'
            )
            command(
                [
                    "d1",
                    "execute",
                    database_name,
                    "--remote",
                    "--config",
                    str(gate_config),
                    "--file",
                    str(temp / "schema.sql"),
                ],
                env=env,
                cwd=temp,
            )
            resources["gate"] = True
            output = command(
                ["deploy", "--config", str(gate_config), "--outdir", str(temp / "gate-bundle")], env=env, cwd=temp
            )
            (diagnostics / "gate-deploy.txt").write_text(output)
            found = re.search(r"https://r5-gate-[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev", output)
            if not found:
                raise RuntimeError("publication Worker URL unavailable")
            gate_url = found.group(0)
            command(
                ["secret", "put", "PUBLICATION_TEST_TOKEN", "--config", str(gate_config)],
                env=env,
                cwd=temp,
                stdin=token,
            )
            started = datetime.now(UTC).isoformat()
            stalled = subprocess.run(
                ["node", str(temp / "tests/stalled_upload.mjs"), gate_url],
                cwd=ROOT,
                env=env | {"PUBLICATION_TEST_TOKEN": token},
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            transport = [json.loads(line) for line in stalled.stdout.splitlines() if line.strip()]
            transport_passed = stalled.returncode == 0
            result = subprocess.run(
                ["node", str(temp / "tests/qualify.mjs"), gate_url, str(temp / "fixture.json")],
                cwd=ROOT,
                env=env
                | {"PUBLICATION_TEST_TOKEN": token, "REVIEWS_PUBLIC_ORIGIN": source_url, "REVIEWS_SOURCE_CONTROL": ""},
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(f"hosted publication qualification failed:\n{result.stdout}\n{result.stderr}")
            measurement = json.loads(result.stdout.strip().splitlines()[-1])
            state = command(
                [
                    "d1",
                    "execute",
                    database_name,
                    "--remote",
                    "--config",
                    str(gate_config),
                    "--json",
                    "--command",
                    "SELECT (SELECT count(*) FROM publication_media_tombstones) media_count, "
                    "(SELECT count(*) FROM publication_tombstones) lineage_count, "
                    "(SELECT state FROM publication_gate WHERE singleton=1) gate_state",
                ],
                env=env,
                cwd=temp,
            )
            actual = json.loads(state)[0]["results"][0]
            if actual != {"media_count": 1, "lineage_count": 2, "gate_state": "safe"}:
                raise RuntimeError("hosted tombstone assertion failed")
            receipt = {
                "date_utc": started,
                "source_commit": commit,
                "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
                "frozen_sources_sha256": frozen,
                "deployed_source_bundle_sha256": directory_digest(temp / "source-bundle"),
                "deployed_gate_bundle_sha256": directory_digest(temp / "gate-bundle"),
                "wrangler_version": command(["--version"], env=env, cwd=ROOT).strip(),
                "node_version": subprocess.check_output(["node", "--version"], text=True).strip(),
                "source_kind": "synthetic replay of local R4 public projection",
                "database_assertions": actual,
                "transport": transport,
                "transport_test_passed": transport_passed,
                "measurements": measurement,
            }
        finally:
            if retain_seconds and sys.exc_info()[0] is not None:
                print(f"Retaining disposable resources for {retain_seconds}s before cleanup", flush=True)
                time.sleep(retain_seconds)
            for key, args in [
                ("gate", ["delete", gate_name, "--force"]),
                ("source", ["delete", source_name, "--force"]),
                ("database", ["d1", "delete", database_name, "--skip-confirmation"]),
            ]:
                if resources[key]:
                    try:
                        subprocess.run(
                            [WRANGLER, *args], cwd=temp, env=env, text=True, capture_output=True, timeout=180
                        )
                        if key == "database":
                            listing = subprocess.run(
                                [WRANGLER, "d1", "list", "--json"],
                                cwd=temp,
                                env=env,
                                text=True,
                                capture_output=True,
                                timeout=180,
                                check=True,
                            )
                            absent = all(item.get("name") != database_name for item in json.loads(listing.stdout))
                        else:
                            name = gate_name if key == "gate" else source_name
                            listing = subprocess.run(
                                [WRANGLER, "deployments", "list", "--name", name, "--json"],
                                cwd=temp,
                                env=env,
                                text=True,
                                capture_output=True,
                                timeout=180,
                            )
                            absent = listing.returncode != 0 and bool(
                                re.search(r"\b10007\b", listing.stdout + listing.stderr)
                            )
                        if not absent:
                            cleanup_errors.append(
                                f"{key}: {dict(source=source_name, gate=gate_name, database=database_name)[key]}"
                            )
                    except Exception:
                        cleanup_errors.append(
                            f"{key}: {dict(source=source_name, gate=gate_name, database=database_name)[key]}"
                        )
            if cleanup_errors:
                raise RuntimeError("disposable resource cleanup failed: " + ", ".join(cleanup_errors))
        if receipt:
            receipt["cleanup"] = "all disposable resources deleted"
            receipt["qualification"] = (
                "passed" if receipt["transport_test_passed"] else "incomplete: stalled-upload transport"
            )
            output = ROOT / "modules/reviews/publication/hosted-receipt.json"
            output.write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupt)
    main()
