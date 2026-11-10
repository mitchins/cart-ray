"""Resumable, test-only Acorn installation operator. Run with --help."""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from cloud import API, CloudError, stripe
from topology import graph, secret_bindings, wrangler_config

ROOT = Path(__file__).resolve().parents[3]
ROLES = ("source", "reviews", "consumer", "producer", "evidence", "scheduler", "sender")
DATABASES = ("commerce", "reviews", "context")
QUEUES = ("events", "schedule", "due", "dlq")
SECRET_NAMES = (
    "source_identity",
    "trigger",
    "activation",
    "event_key",
    "attestation_key",
    "attestation_token",
    "ingress",
    "consent",
    "recipient_evidence",
    "recipient_hash",
    "recipient_resolve",
    "automation_evidence",
    "automation_recipient",
    "delivery",
    "invitation",
    "reviewer",
    "admin",
    "settlement",
    "unsubscribe",
    "scheduler",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.geteuid() or mode.st_mode & 0o077:
            raise ValueError("credential/vault file must be a private regular file owned by the operator (chmod 600)")
        with os.fdopen(fd, closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        os.chmod(stream.name, 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)


def validate_config(config):
    required = {"namespace", "account_id", "stripe_account_id", "preflight_lock"}
    optional = {"merchant_name", "merchant_contact", "delay_seconds", "max_lateness"}
    if not isinstance(config, dict) or set(config) - required - optional or required - set(config):
        raise ValueError("installation config has missing or unknown fields")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,31}", config["namespace"]) or not config["namespace"].endswith("-test"):
        raise ValueError("namespace must be 3–32 lower-case characters ending in -test")
    if not re.fullmatch(r"[a-f0-9]{32}", config["account_id"]):
        raise ValueError("invalid Cloudflare account identity")
    if not re.fullmatch(r"acct_[A-Za-z0-9]+", config["stripe_account_id"]):
        raise ValueError("invalid Stripe test account identity")
    for field, default, maximum in [("delay_seconds", 0, 2592000), ("max_lateness", 86400, 2592000)]:
        value = config.get(field, default)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"invalid {field}")
    for name, limit in [("merchant_name", 100), ("merchant_contact", 200)]:
        if name in config and (
            not isinstance(config[name], str)
            or not config[name].strip()
            or len(config[name]) > limit
            or re.search(r"[\x00-\x1f\x7f]", config[name])
        ):
            raise ValueError(f"invalid public {name}")
    return {
        "merchant_name": "Squirrel’s Acorn Store",
        "merchant_contact": "Contact the test merchant",
        "delay_seconds": 0,
        "max_lateness": 86400,
        **config,
    }


class Installation:
    def __init__(self, directory, credentials=None, api=None):
        self.directory = Path(directory).resolve()
        self.state_path = self.directory / "inventory.json"
        self.vault_path = self.directory / "secrets.json"
        self.state = json.loads(self.state_path.read_text())
        self.config = validate_config(self.state["config"])
        if (
            self.state.get("format") != 1
            or self.state.get("environment") != "test"
            or self.state.get("installation") != self.config["namespace"]
        ):
            raise ValueError("inventory scope/format mismatch; no resource operation is permitted")
        if not Path(self.config["preflight_lock"]).is_absolute():
            self.config["preflight_lock"] = str(self.directory / self.config["preflight_lock"])
        self.credentials = credentials
        if credentials is not None:
            required = {
                "cloudflare_api_token",
                "stripe_test_key",
                "smtp2go_api_key",
                "approved_test_email",
                "test_sender",
            }
            if not required.issubset(credentials) or any(
                not isinstance(credentials[key], str) or not credentials[key] for key in required
            ):
                raise ValueError("credential handoff is incomplete; use the documented private JSON schema")
            if not credentials["stripe_test_key"].startswith(("sk_test_", "rk_test_")):
                raise ValueError("only a Stripe test credential is accepted")
            if not credentials["smtp2go_api_key"].startswith("api-"):
                raise ValueError("invalid test SMTP2GO credential")
            for field in ("approved_test_email", "test_sender"):
                if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}", credentials[field]):
                    raise ValueError(f"invalid approved test mailbox field {field}")
        self.api = api or (API(self.config["account_id"], credentials["cloudflare_api_token"]) if credentials else None)

    @classmethod
    def plan(cls, directory, config):
        directory = Path(directory).resolve()
        if directory.exists() and any(p.name != ".operator.lock" for p in directory.iterdir()):
            raise ValueError("plan requires an empty new directory; resume existing inventory instead")
        config = validate_config(config)
        lock = json.loads(Path(config["preflight_lock"]).read_text())
        if lock.get("stripe_mode") != "test" or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", lock.get("catalogue_version", "")
        ):
            raise ValueError("a compiled Stripe test preflight lock is required")
        atomic_json(directory / "inputs/preflight-lock.json", lock)
        config["preflight_lock"] = "inputs/preflight-lock.json"
        atomic_json(
            directory / "inventory.json",
            {
                "format": 1,
                "installation": config["namespace"],
                "environment": "test",
                "config": config,
                "phase": "planned",
                "resources": {},
                "pending": [],
                "releases": [],
                "secret_fingerprints": {},
                "activation_authorized": False,
            },
        )
        return cls(directory)

    def save(self):
        atomic_json(self.state_path, self.state)

    def name(self, role):
        return self.config["namespace"] + "-" + role

    def vault(self, create=False):
        if self.vault_path.exists():
            value = read_private(self.vault_path)
            expected = self.state["secret_fingerprints"]
            if not expected and create and not self.state["resources"] and self.state["phase"] == "planned":
                # A crash may occur after the private vault rename and before its
                # public fingerprints are saved. No resources can yet use these keys.
                required = {*SECRET_NAMES, "signing_private", "signing_public"}
                if set(value) != required or any(not isinstance(item, str) or not item for item in value.values()):
                    raise ValueError("incomplete initial vault; restore its original backup")
                self.state["secret_fingerprints"] = {key: digest(item.encode()) for key, item in value.items()}
                self.save()
                expected = self.state["secret_fingerprints"]
            if not expected or any(
                digest(value.get(key, "").encode()) != checksum for key, checksum in expected.items()
            ):
                raise ValueError("vault identity changed; restore original vault, never regenerate or rotate keys")
            return value
        if not create or self.state["secret_fingerprints"] or self.state["resources"]:
            raise ValueError("installation vault missing; restore its original backup before continuing")
        values = {name: secrets.token_hex(32) for name in SECRET_NAMES}
        generated = subprocess.run(
            [
                "node",
                "-e",
                "const c=require('node:crypto');const k=c.generateKeyPairSync('ed25519');"
                "console.log(JSON.stringify({private:k.privateKey.export({type:'pkcs8',format:'der'}).toString('base64'),"
                "public:k.publicKey.export({type:'spki',format:'der'}).toString('base64')}))",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pair = json.loads(generated.stdout)
        values.update(signing_private=pair["private"], signing_public=pair["public"])
        atomic_json(self.vault_path, values)
        self.state["secret_fingerprints"] = {name: digest(value.encode()) for name, value in values.items()}
        self.save()
        return values

    def assert_resources(self):
        for key, resource in self.state["resources"].items():
            kind, role = key.split(":", 1)
            if resource["name"] != self.name(role) and kind != "consumer":
                raise ValueError(f"{key}: inventory resource name differs from namespace")
            if kind == "database":
                actual = self.api.request("d1/database/" + resource["id"])
                if actual["name"] != self.name(role):
                    raise ValueError(f"{key}: remote database identity differs; no migration/deploy will run")
            elif kind == "queue":
                actual = self.api.request("queues/" + resource["id"])
                if actual["queue_name"] != self.name(role):
                    raise ValueError(f"{key}: remote queue identity differs")

    def provider_ready(self):
        if not self.api or not self.credentials:
            raise ValueError("this operation requires --credentials with documented test credentials")
        key = self.credentials.get("stripe_test_key", "")
        if not key.startswith(("sk_test_", "rk_test_")):
            raise ValueError("live or missing Stripe credentials are not accepted")
        if stripe(key, "account").get("id") != self.config["stripe_account_id"]:
            raise ValueError("Stripe credential account mismatch")

    def provision(self):
        self.provider_ready()
        self.assert_resources()
        self.vault(create=True)
        account = self.api.request("workers/subdomain")["subdomain"]
        self.state["workers_subdomain"] = account
        self.save()
        for kind, roles, endpoint, name_field, identity in [
            ("database", DATABASES, "d1/database", "name", "uuid"),
            ("queue", QUEUES, "queues", "queue_name", "queue_id"),
        ]:
            for role in roles:
                key = kind + ":" + role
                if key in self.state["resources"]:
                    continue
                matches = [item for item in self.api.listing(endpoint) if item.get(name_field) == self.name(role)]
                if len(matches) > 1 or (matches and key not in self.state["pending"]):
                    raise ValueError(f"unmanaged/colliding resource {self.name(role)}; select a fresh namespace")
                if not matches:
                    if key not in self.state["pending"]:
                        self.state["pending"].append(key)
                        self.save()
                    result = self.api.request(endpoint, "POST", {name_field: self.name(role)})
                else:
                    result = matches[0]
                self.state["resources"][key] = {"name": self.name(role), "id": result[identity]}
                self.state["pending"].remove(key)
                self.save()
        self.provision_workers()
        self.provision_webhook()
        self.provision_pages()
        self.configure_queues(pause_processing=self.state["phase"] not in ("planned", "provisioned", "active"))
        if self.state["phase"] == "planned":
            self.state["phase"] = "provisioned"
        self.save()
        return self.state["resources"]

    def configure_queues(self, pause_processing=False):
        # The verifier requires explicit false; the API omits its implicit default.
        for role in QUEUES:
            resource = self.state["resources"]["queue:" + role]
            self.api.request(
                "queues/" + resource["id"],
                "PATCH",
                {
                    "settings": {
                        "delivery_paused": pause_processing and role != "dlq",
                        "message_retention_period": 86400,
                    }
                },
            )

    def provision_workers(self):
        existing = {item["id"] for item in self.api.listing("workers/scripts")}
        for role in ROLES:
            key = "worker:" + role
            if key in self.state["resources"]:
                continue
            name = self.name(role)
            if name in existing and key not in self.state["pending"]:
                raise ValueError(f"unmanaged Worker {name}; choose a fresh namespace")
            if name not in existing:
                if key not in self.state["pending"]:
                    self.state["pending"].append(key)
                    self.save()
                self.api.upload(
                    name,
                    [
                        (
                            "bootstrap.mjs",
                            b"export default {fetch(){return new Response(null,{status:503})}}",
                            "application/javascript+module",
                        )
                    ],
                    {"main_module": "bootstrap.mjs", "bindings": [], "compatibility_date": "2026-08-31"},
                    create=True,
                )
            self.state["resources"][key] = {"name": name, "id": name}
            self.state["pending"].remove(key)
            self.save()

    def provision_webhook(self):
        vault = self.vault()
        if "webhook_secret" in vault:
            if "webhook" not in self.state:
                endpoint = vault.get("webhook_endpoint")
                if not endpoint:
                    raise ValueError("webhook endpoint receipt missing; restore inventory before continuing")
                self.state["webhook"] = endpoint
                self.save()
            return
        if "webhook" in self.state:
            raise ValueError("webhook secret is missing; restore the vault, never recreate the endpoint")
        if "webhook_pending_at" in self.state and time.time() - self.state["webhook_pending_at"] > 23 * 3600:
            raise ValueError(
                "unknown webhook create outcome exceeds Stripe idempotency window; inspect endpoint and restore vault"
            )
        self.state.setdefault("webhook_pending_at", time.time())
        self.save()
        url = f"https://{self.name('source')}.{self.state['workers_subdomain']}.workers.dev/stripe/webhook"
        result = stripe(
            self.credentials["stripe_test_key"],
            "webhook_endpoints",
            {
                "url": url,
                "enabled_events[0]": "checkout.session.completed",
                "enabled_events[1]": "checkout.session.expired",
                "enabled_events[2]": "checkout.session.async_payment_succeeded",
                "api_version": "2025-09-30.clover",
            },
            idempotency=self.name("webhook-v1"),
        )
        vault["webhook_secret"] = result["secret"]
        vault["webhook_endpoint"] = {"id": result["id"], "url": url}
        atomic_json(self.vault_path, vault)
        # Only the non-regenerable installation keys are fingerprinted; provider webhook
        # creation uses a stable Stripe idempotency key and is journalled separately.
        self.state["webhook"] = {"id": result["id"], "url": url}
        self.save()

    def provision_pages(self):
        key, name = "pages:store", self.name("store")
        if key in self.state["resources"]:
            self.ensure_pages_subdomain()
            return
        matches = [item for item in self.api.listing("pages/projects") if item["name"] == name]
        if matches and key not in self.state["pending"]:
            raise ValueError("unmanaged Pages project; choose a fresh namespace")
        if not matches:
            if key not in self.state["pending"]:
                self.state["pending"].append(key)
                self.save()
            project = self.api.request("pages/projects", "POST", {"name": name, "production_branch": "main"})
        else:
            project = matches[0]
        self.state["resources"][key] = {"name": name, "id": name}
        self.state["pending"].remove(key)
        self.save()
        self.ensure_pages_subdomain(project)

    def ensure_pages_subdomain(self, project=None):
        """Retain the provider-assigned origin, including collision suffixes."""
        if self.state.get("pages_subdomain"):
            return
        project = project or {}
        if not project.get("subdomain"):
            project = self.api.request("pages/projects/" + self.name("store"))
        domain = project.get("subdomain")
        if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.pages\.dev", domain):
            raise CloudError("Pages project has no valid assigned pages.dev subdomain; retry provision")
        self.state["pages_subdomain"] = domain
        self.save()

    def run(self, step, command, environment=None):
        logs = self.directory / "logs"
        logs.mkdir(exist_ok=True)
        output = logs / (step + ".log")
        with output.open("wb") as stream:
            os.chmod(output, 0o600)
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise CloudError(f"{step} failed (exit {result.returncode}); inspect protected {output}, then resume")

    def private(self, service, path, body=None, token=None, method="POST"):
        bindings = self.directory / "operator-bindings.toml"
        names = {"REVIEWS": "reviews", "PRODUCER": "producer", "CONSUMER": "consumer", "EVIDENCE": "evidence"}
        lines = [
            f'name = "{self.name("operator-local")}"',
            f'account_id = "{self.config["account_id"]}"',
            'compatibility_date = "2026-08-31"',
        ]
        for binding, role in names.items():
            lines += ["[[services]]", f'binding = "{binding}"', f'service = "{self.name(role)}"', "remote = true"]
        bindings.write_text("\n".join(lines) + "\n")
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = "Bearer " + token
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("bindings.mjs")), str(bindings)],
            input=json.dumps(
                {
                    "service": service,
                    "path": path,
                    "method": method,
                    "headers": headers,
                    **({"body": body} if body is not None else {}),
                }
            ),
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
            env={
                **os.environ,
                "CLOUDFLARE_API_TOKEN": self.credentials["cloudflare_api_token"],
                "WRANGLER_LOG": "error",
            },
        )
        if result.returncode:
            log = self.directory / "private-service-error.log"
            log.write_text(result.stdout + result.stderr)
            log.chmod(0o600)
            raise CloudError(f"private service operation failed; inspect protected {log}, then retry")
        payload = json.loads([line for line in result.stdout.splitlines() if line.startswith("{")][-1])
        if payload["status"] >= 400:
            raise CloudError(f"private {service} {method} {path}: HTTP {payload['status']}")
        return json.loads(payload["body"]) if payload["body"] else {}

    def check_schema(self):
        self.assert_resources()
        for role, expected in self.state.get("migration_ledgers", {}).items():
            actual = self.api.query(
                self.db(role), "SELECT position,path,checksum FROM cartray_operator_migrations ORDER BY position"
            )
            if actual != expected:
                raise ValueError(f"{role}: live migration ledger differs; no application rollback/deploy is permitted")
        if set(self.state.get("migration_ledgers", {})) != set(DATABASES):
            raise ValueError("all three migration ledgers are required")

    def upgrade(self):
        current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if self.state["phase"] in ("active", "paused") and self.state["deployment"]["commit"] == current:
            return self.doctor()
        was_active = self.state["activation_authorized"]
        stay_paused = self.state.get("automation_paused", False)
        self.migrate()
        self.build()
        self.deploy()
        if was_active and not stay_paused:
            return self.activate(True)
        result = self.verify()
        if was_active and stay_paused:
            self.verifier("record")
            self.state["phase"] = "paused"
            self.save()
            return {"phase": "paused", "activation_watermark": self.state["activation_watermark"]}
        return result

    def render(self, versions=None, sending=False):
        self.ensure_pages_subdomain()
        nodes, urls = graph(self, versions, sending)
        configs = self.directory / "configs"
        configs.mkdir(exist_ok=True)
        for role, node in nodes.items():
            (configs / (role + ".toml")).write_text(wrangler_config(self, role, node))
        self.state["urls"] = urls
        self.save()
        return nodes

    def build(self):
        self.vault()
        self.check_schema()
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT, check=True)
        release = self.directory / "releases" / commit
        if (release / "build.json").exists():
            self.state["build"] = commit
            self.save()
            return json.loads((release / "build.json").read_text())
        release.mkdir(parents=True, exist_ok=True)
        self.render()
        source = release / "source"
        if not (source / "receipt.json").exists():
            if source.exists():
                shutil.rmtree(source)
            self.run(
                "build-source",
                [
                    "uv",
                    "run",
                    "--locked",
                    "python",
                    "scripts/build_acorn_commerce.py",
                    "--preflight-lock",
                    self.config["preflight_lock"],
                    "--wrangler-config",
                    str(self.directory / "configs/source.toml"),
                    "--output",
                    str(source),
                ],
            )
        reviews = release / "reviews"
        if not (reviews / "receipt.json").exists():
            if reviews.exists():
                shutil.rmtree(reviews)
            self.run(
                "build-reviews",
                [
                    "uv",
                    "run",
                    "--locked",
                    "python",
                    "modules/reviews/tools/clean_qualification_build.py",
                    "--output",
                    str(reviews),
                ],
            )
        nodes, _ = graph(self)
        artifacts = {}
        for role, node in nodes.items():
            if role in ("source", "reviews"):
                directory = release / role / "bundle"
                files = {
                    str(item.relative_to(directory)): digest(item.read_bytes())
                    for item in directory.rglob("*")
                    if item.is_file() and item.name not in ("README.md", "wrangler.toml")
                }
            else:
                output = release / (role + ".mjs")
                self.run(
                    "build-" + role,
                    [
                        str(ROOT / "node_modules/.bin/esbuild"),
                        str(ROOT / node["main"]),
                        "--bundle",
                        "--format=esm",
                        "--outfile=" + str(output),
                    ],
                )
                files = {role + ".mjs": digest(output.read_bytes())}
            artifacts[role] = files
        result = {
            "commit": commit,
            "artifacts": artifacts,
            "schema": self.state.get("migration_ledgers"),
            "suppression_key_fingerprint": self.state["secret_fingerprints"]["recipient_hash"],
        }
        atomic_json(release / "build.json", result)
        self.state["build"] = commit
        self.save()
        return result

    def artifact_modules(self, role, build):
        release = self.directory / "releases" / build["commit"]
        folder = release / role / "bundle" if role in ("source", "reviews") else release
        modules = []
        for name, expected in build["artifacts"][role].items():
            value = (folder / name).read_bytes()
            if digest(value) != expected:
                raise ValueError(f"retained artifact changed: {role}/{name}")
            kind = (
                "text/x-python"
                if name.endswith(".py")
                else ("application/javascript+module" if name.endswith(".mjs") else "application/octet-stream")
            )
            modules.append((name, value, kind))
        return modules

    def deploy(self):
        self.ensure_pages_subdomain()
        self.check_schema()
        self.pin_suppression()
        if "build" not in self.state:
            raise ValueError("build the clean commit before deployment")
        build = json.loads((self.directory / "releases" / self.state["build"] / "build.json").read_text())
        if build["schema"] != self.state.get("migration_ledgers"):
            raise ValueError("build schema contract differs; migrate then build a new release")
        progress = self.state.setdefault("deployment", {"commit": build["commit"], "versions": {}, "complete": False})
        if progress["commit"] != build["commit"]:
            if not progress.get("complete"):
                raise ValueError("finish or roll back the interrupted deployment before another release")
            progress = {"commit": build["commit"], "versions": {}, "complete": False}
            self.state["deployment"] = progress
        # Validate every retained artifact before any runtime mutation.
        for role in ROLES:
            self.artifact_modules(role, build)
        # A disabled/stale sender must never consume and exhaust durable due hints.
        self.state["queue_recovery_required"] = True
        self.save()
        self.configure_queues(pause_processing=True)
        if "sender" not in progress["versions"]:
            progress["sending"] = False
        self.state["phase"] = "deploying"
        self.save()
        for role in ("source", "reviews", "producer", "evidence", "scheduler", "consumer", "sender"):
            if role in progress["versions"]:
                self.api.request(
                    f"workers/scripts/{self.name(role)}/subdomain",
                    "POST",
                    {"enabled": role in ("source", "sender"), "previews_enabled": False},
                )
                continue
            nodes, _ = graph(self, progress["versions"], sending=False)
            node = nodes[role]
            bindings = node["bindings"] + secret_bindings(self, role)
            metadata = {
                "main_module": "worker.py" if role in ("source", "reviews") else role + ".mjs",
                "bindings": bindings,
                "compatibility_date": "2026-08-31",
                "compatibility_flags": (
                    ["python_workers"] + (["disable_python_external_sdk"] if role == "reviews" else [])
                )
                if role in ("source", "reviews")
                else [],
                "annotations": {"workers/message": "CartRay test operator " + build["commit"]},
            }
            result = self.api.upload(self.name(role), self.artifact_modules(role, build), metadata)
            self.api.publish(self.name(role), result["id"])
            progress["versions"][role] = result["id"]
            self.save()
            self.api.request(
                f"workers/scripts/{self.name(role)}/subdomain",
                "POST",
                {"enabled": node.get("public", False), "previews_enabled": False},
            )
        for role in ("consumer", "scheduler", "sender"):
            nodes, _ = graph(self, progress["versions"])
            queue_role = nodes[role]["consumes"]
            key = "consumer:" + queue_role
            if key in self.state["resources"]:
                continue
            identifier = self.state["resources"]["queue:" + queue_role]["id"]
            current = self.api.request("queues/" + identifier)
            consumers = current.get("consumers", [])
            if consumers and not all(
                item.get("script_name", item.get("script")) == self.name(role) for item in consumers
            ):
                raise ValueError("queue has an unexpected consumer; doctor must be resolved before continuing")
            if not consumers:
                self.api.request(
                    f"queues/{identifier}/consumers",
                    "POST",
                    {
                        "type": "worker",
                        "script_name": self.name(role),
                        "settings": {"batch_size": 10, "max_wait_time_ms": 1000, "max_retries": 3},
                        "dead_letter_queue": self.name("dlq"),
                    },
                )
            self.state["resources"][key] = {"id": identifier, "name": self.name(role)}
            self.save()
        self.configure_queues(pause_processing=True)
        self.api.request(f"workers/scripts/{self.name('source')}/schedules", "PUT", [{"cron": "0 * * * *"}])
        self.render(progress["versions"])
        self.seed_subjects()
        self.deploy_storefront(build)
        self.state["deployment"]["complete"] = True
        self.state["phase"] = "deployed"
        self.save()
        return {"commit": build["commit"], "versions": progress["versions"], "activated": False}

    def seed_subjects(self):
        subjects = [
            ("acorn-growing-kit", "/products/acorn-growing-kit/", "Acorn growing kit"),
            ("woodland-planter", "/products/woodland-planter/", "Woodland planter"),
        ]
        for subject, path, label in subjects:
            self.api.query(
                self.db("reviews"),
                "INSERT INTO review_subjects VALUES ('acorn-store','test',?,'product',?) ON CONFLICT DO NOTHING",
                [subject, path],
            )
            self.api.query(
                self.db("reviews"),
                "INSERT INTO review_subject_display_names VALUES ('acorn-store','test',?,?) ON CONFLICT DO NOTHING",
                [subject, label],
            )
            self.api.query(
                self.db("reviews"),
                "INSERT INTO native_publication_versions "
                "(store_key,environment,subject_id,version,contribution_count,rated_count,rating_sum) "
                "VALUES ('acorn-store','test',?,0,0,0,0) ON CONFLICT DO NOTHING",
                [subject],
            )

    def deploy_storefront(self, build):
        output = self.directory / "releases" / build["commit"] / "storefront"
        if not (output / ".operator-build-complete").exists():
            staging = output.with_name("storefront.partial")
            if staging.exists():
                shutil.rmtree(staging)
            if output.exists():
                shutil.rmtree(output)
            self.run(
                "build-storefront",
                [
                    "uv",
                    "run",
                    "--locked",
                    "python",
                    "examples/acorn-store/build.py",
                    "--output",
                    str(staging),
                    "--test-checkout-api",
                    self.state["urls"]["source"],
                    "--reviews",
                    "--test-review-gateway",
                    "--preflight-lock",
                    self.config["preflight_lock"],
                ],
            )
            (staging / ".operator-build-complete").write_text(build["commit"] + "\n")
            os.replace(staging, output)
        origin = self.state["urls"]["storefront"]
        deployment = {
            "env_vars": {
                "ACORN_REVIEWS_GATEWAY": {"type": "plain_text", "value": "test"},
                "ACORN_REVIEWS_READ_MODE": {"type": "plain_text", "value": "test"},
                "ACORN_STOREFRONT_ORIGIN": {"type": "plain_text", "value": origin},
            },
            "services": {
                name: {"service": self.name("reviews"), "environment": "production"}
                for name in ("REVIEWS_API", "REVIEWS_PUBLIC")
            },
        }
        self.api.request(
            "pages/projects/" + self.name("store"), "PATCH", {"deployment_configs": {"production": deployment}}
        )
        self.run(
            "deploy-storefront",
            [
                str(ROOT / "node_modules/.bin/wrangler"),
                "pages",
                "deploy",
                str(output),
                "--project-name",
                self.name("store"),
                "--branch",
                "main",
            ],
            {
                **os.environ,
                "CLOUDFLARE_API_TOKEN": self.credentials["cloudflare_api_token"],
                "CLOUDFLARE_ACCOUNT_ID": self.config["account_id"],
            },
        )

    def verifier(self, mode):
        self.ensure_pages_subdomain()
        self.vault()
        nodes, _ = graph(self, self.state["deployment"]["versions"])
        env = {
            **nodes["producer"]["vars"],
            "CF_VERSION_METADATA": {"id": self.state["deployment"]["versions"]["producer"]},
            **{item["name"]: item["text"] for item in secret_bindings(self, "producer")},
            "CF_READ_TOKEN": self.credentials["cloudflare_api_token"],
            "OPERATOR_EXPECT_EVENT_QUEUE_PAUSED": self.state["phase"] != "active",
        }
        config = self.directory / "verifier-bindings.toml"
        lines = [
            f'name = "{self.name("operator-local")}"',
            f'account_id = "{self.config["account_id"]}"',
            'compatibility_date = "2026-08-31"',
        ]
        for name, role in [("SOURCE_API", "source"), ("REVIEWS_API", "reviews")]:
            lines += ["[[services]]", f'binding = "{name}"', f'service = "{self.name(role)}"', "remote = true"]
        config.write_text("\n".join(lines) + "\n")
        rows = self.api.query(
            self.db("commerce"),
            "SELECT COALESCE(MAX(revision),0) AS revision "
            "FROM reviews_verified_deployment WHERE subscriber_id=? AND installation_id=?",
            [self.config["namespace"], self.config["namespace"]],
        )
        value = subprocess.run(
            ["node", str(Path(__file__).with_name("verify.mjs")), str(config)],
            cwd=ROOT,
            input=json.dumps({"mode": mode, "env": env, "expectedRevision": rows[0]["revision"]}),
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "CLOUDFLARE_API_TOKEN": self.credentials["cloudflare_api_token"],
                "WRANGLER_LOG": "error",
            },
        )
        if value.returncode:
            log = self.directory / "verification-error.log"
            log.write_text(value.stdout + value.stderr)
            log.chmod(0o600)
            raise CloudError(f"private verification failed; inspect protected {log}, then retry")
        return json.loads([line for line in value.stdout.splitlines() if line.startswith("{")][-1])

    def verify(self):
        self.pin_suppression()
        result = self.verifier("inspect")
        if self.state["phase"] != "active":
            self.state["phase"] = "verified"
            self.save()
        return {
            "phase": self.state["phase"],
            "activation_authorized": self.state["activation_authorized"],
            "versions": {key: value for key, value in result.items() if key.endswith("_version")},
        }

    def activate(self, enable_test_delivery=False):
        if not enable_test_delivery:
            raise ValueError(
                "activation requires explicit --enable-test-delivery for the documented approved test mailbox"
            )
        if self.state["phase"] == "active":
            health = self.doctor()
            if not health["healthy"]:
                raise ValueError("active installation differs; resolve doctor findings before repeating activation")
            return {
                "phase": "active",
                "already_active": True,
                "activation_watermark": self.state["activation_watermark"],
            }
        if not self.state.get("deployment", {}).get("complete"):
            raise ValueError("finish the interrupted deploy before explicit activation")
        # Reconcile a partially completed queue resume before staged inspection.
        self.configure_queues(pause_processing=True)
        self.verify()
        vault = self.vault()
        # This is the sole initial activation authorization. Inspect/verify never
        # creates a record that would let the runtime implicitly activate itself.
        result = self.private(
            "REVIEWS",
            "/reviews-internal/v1/automation-activate",
            {
                "policy_version": "operator-v1",
                "delay_seconds": self.config["delay_seconds"],
                "max_lateness": self.config["max_lateness"],
            },
            vault["activation"],
        )
        self.state["activation_authorized"] = True
        self.state["activation_watermark"] = result["outbox_watermark"]
        self.save()
        record = self.verifier("record")
        nodes, _ = graph(self, self.state["deployment"]["versions"], sending=True)
        node = nodes["sender"]
        build = json.loads((self.directory / "releases" / self.state["build"] / "build.json").read_text())
        version = self.api.upload(
            self.name("sender"),
            self.artifact_modules("sender", build),
            {
                "main_module": "sender.mjs",
                "bindings": node["bindings"] + secret_bindings(self, "sender"),
                "compatibility_date": "2026-08-31",
            },
        )
        self.api.publish(self.name("sender"), version["id"])
        self.state["deployment"]["versions"]["sender"] = version["id"]
        self.state["deployment"]["sending"] = True
        self.api.request(f"workers/scripts/{self.name('producer')}/schedules", "PUT", [{"cron": "* * * * *"}])
        self.private("PRODUCER", "/run", token=vault["trigger"])
        if record.get("activation_status") != "active":
            # The first relay creates the immutable checkpoint. Reflect its actual
            # active state without changing its original activation watermark.
            record = self.verifier("record")
        self.recover_paused_hints()
        self.configure_queues()
        self.state["automation_paused"] = False
        self.state["queue_recovery_required"] = False
        self.state["phase"] = "active"
        self.save()
        release = {
            **build,
            "versions": dict(self.state["deployment"]["versions"]),
            "record_revision": record["revision"],
        }
        if not self.state["releases"] or self.state["releases"][-1] != release:
            self.state["releases"].append(release)
        self.save()
        return {
            "phase": "active",
            "record_revision": record["revision"],
            "activation_watermark": result["outbox_watermark"],
        }

    def recover_paused_hints(self):
        """Reconstruct expirable hints from durable post-fence facts before resume."""
        if not self.state.get("automation_paused") and not self.state.get("queue_recovery_required"):
            return
        row = self.api.query(
            self.db("commerce"),
            "SELECT activation_watermark,last_outbox_id FROM reviews_subscriber_checkpoint WHERE subscriber_id=?",
            [self.config["namespace"]],
        )
        if not row:
            return
        fence, upper = row[0]["activation_watermark"], row[0]["last_outbox_id"]
        if fence != self.state["activation_watermark"] or upper < fence:
            raise ValueError("pause recovery checkpoint/fence differs; no queue is resumed")
        now = int(time.time())
        recovery = self.state.get("pause_recovery")
        # Cloudflare hints are expirable. Never resume from an old partial replay:
        # all reconstructed pages must be fresh within this conservative deadline.
        if not recovery or not 0 <= now - recovery["started_at"] < 300:
            recovery = {"started_at": now, "fence": fence, "upper": upper, "last_id": fence, "complete": False}
            self.state["pause_recovery"] = recovery
            self.save()
        if recovery["fence"] != fence:
            raise ValueError("pause recovery fence changed; no queue is resumed")
        key = bytes.fromhex(self.vault()["event_key"])
        for _ in range(20):
            rows = self.api.query(
                self.db("commerce"),
                "SELECT id FROM outbox WHERE id>? AND id<=? AND event_type='OrderConfirmed' ORDER BY id LIMIT 20",
                [recovery["last_id"], recovery["upper"]],
            )
            if not rows:
                if int(time.time()) - recovery["started_at"] >= 300:
                    raise ValueError("pause recovery freshness window elapsed; repeat explicit activation")
                recovery["complete"] = True
                self.save()
                return
            messages = []
            for row in rows:
                identity = row["id"]
                payload = [
                    "cartray-reviews-order-confirmed-v1",
                    self.config["namespace"],
                    self.config["namespace"],
                    "acorn-store",
                    "test",
                    identity,
                ]
                signature = hmac.new(key, json.dumps(payload, separators=(",", ":")).encode(), "sha256").hexdigest()
                messages.append(
                    {
                        "content_type": "json",
                        "body": {
                            "version": 1,
                            "issuer": self.config["namespace"],
                            "installation": self.config["namespace"],
                            "store": "acorn-store",
                            "environment": "test",
                            "outbox_id": identity,
                            "signature": signature,
                        },
                    }
                )
            self.api.request(
                "queues/" + self.state["resources"]["queue:events"]["id"] + "/messages/batch",
                "POST",
                {"messages": messages},
            )
            recovery["last_id"] = rows[-1]["id"]
            self.save()
        raise ValueError("pause recovery page budget reached; repeat explicit activation within five minutes")

    def pause(self):
        """Stop new automation deliveries without changing checkout or unsubscribe."""
        self.assert_resources()
        self.vault()
        if not self.state.get("automation_paused"):
            self.state.pop("pause_recovery", None)
        self.state["automation_paused"] = True
        self.state["queue_recovery_required"] = True
        self.state["phase"] = "pausing"
        self.save()
        self.configure_queues(pause_processing=True)
        self.state["phase"] = "paused"
        self.save()
        return {
            "phase": "paused",
            "activation_authorized": self.state["activation_authorized"],
            "activation_watermark": self.state.get("activation_watermark"),
            "queued_work_retained": True,
        }

    def doctor(self):
        self.ensure_pages_subdomain()
        self.check_schema()
        self.vault()
        self.pin_suppression()
        nodes, _ = graph(
            self,
            self.state.get("deployment", {}).get("versions"),
            sending=self.state.get("deployment", {}).get("sending", self.state.get("activation_authorized", False)),
        )
        errors = []
        for role, expected in nodes.items():
            actual = self.api.version(self.name(role))
            observed = {item["name"]: item for item in actual["resources"]["bindings"]}
            for binding in expected["bindings"]:
                name, kind = binding["name"], binding["type"]
                value = observed.get(name, {})
                if value.get("type") != kind:
                    errors.append(f"{role}.bindings.{name}: missing/wrong type")
                elif kind == "d1" and value.get("database_id") != binding["id"]:
                    errors.append(f"{role}.bindings.{name}: wrong database")
                elif kind == "service" and value.get("service") != binding["service"]:
                    errors.append(f"{role}.bindings.{name}: wrong service")
                elif kind == "queue" and value.get("queue_name") != binding["queue_name"]:
                    errors.append(f"{role}.bindings.{name}: wrong queue")
                elif kind in ("plain_text", "json") and value.get("text", value.get("json")) != binding.get(
                    "text", binding.get("json")
                ):
                    errors.append(f"{role}.vars.{name}: differs from inventory")
        if errors:
            return {
                "healthy": False,
                "phase": self.state["phase"],
                "errors": errors,
                "activation_authorized": self.state["activation_authorized"],
            }
        record = self.verifier("doctor")
        if self.state["activation_authorized"] and record["status"] != "active":
            errors.append("installation record: " + record["status"])
        return {
            "healthy": not errors,
            "phase": self.state["phase"],
            "activation_authorized": self.state["activation_authorized"],
            "errors": errors,
            "record": record,
            "activation_watermark": self.state.get("activation_watermark"),
            "urls": self.state.get("urls"),
        }

    def receipt(self):
        health = self.doctor()
        choices = self.api.query(
            self.db("commerce"),
            "SELECT b.id AS outbox_id,c.selected,c.merchant_default_selected,g.selection_basis,"
            "CASE WHEN g.order_id IS NULL THEN 0 ELSE 1 END AS granted,"
            "(SELECT COUNT(*) FROM order_items i WHERE i.order_id=b.order_id "
            "AND i.unit_amount_minor>0) AS paid_item_count "
            "FROM outbox b LEFT JOIN review_invitation_checkout_choices c ON c.order_id=b.order_id "
            "LEFT JOIN review_invitation_checkout_grants g ON g.order_id=b.order_id "
            "WHERE b.event_type='OrderConfirmed' ORDER BY b.id",
        )
        checkpoint = self.api.query(
            self.db("commerce"),
            "SELECT activation_watermark,last_outbox_id FROM reviews_subscriber_checkpoint WHERE subscriber_id=?",
            [self.config["namespace"]],
        )
        deliveries = self.api.query(
            self.db("reviews"),
            "SELECT delivery_identity,provider_email_id,accepted_at FROM review_provider_deliveries "
            "WHERE store_key='acorn-store' AND environment='test' ORDER BY accepted_at",
        )
        states = self.api.query(
            self.db("reviews"),
            "SELECT state,COUNT(*) AS count FROM invitation_generations "
            "WHERE store_key='acorn-store' AND environment='test' GROUP BY state",
        )
        public = self.api.query(
            self.db("reviews"),
            "SELECT subject_id,contribution_count,rated_count,version FROM native_publication_versions "
            "WHERE store_key='acorn-store' AND environment='test' ORDER BY subject_id",
        )
        suppressed = self.api.query(
            self.db("reviews"),
            "SELECT COUNT(*) AS count FROM review_recipient_suppressions "
            "WHERE store_key='acorn-store' AND environment='test'",
        )
        result = {
            "format": 1,
            "namespace": self.config["namespace"],
            "environment": "test",
            "doctor": health,
            "commit": self.state.get("build"),
            "resources": self.state["resources"],
            "worker_versions": self.state.get("deployment", {}).get("versions"),
            "migration_ledgers": self.state.get("migration_ledgers"),
            "suppression_key_fingerprint": self.state["secret_fingerprints"]["recipient_hash"],
            "checkout_choices": choices,
            "checkpoint": checkpoint,
            "provider_acceptances": deliveries,
            "generation_states": states,
            "public_subjects": public,
            "recipient_suppression_count": suppressed[0]["count"],
        }
        atomic_json(self.directory / "receipt.json", result)
        return result

    def rollback(self, index):
        self.check_schema()
        self.pin_suppression()
        if not self.state["releases"] or not -len(self.state["releases"]) <= index < len(self.state["releases"]):
            raise ValueError("requested compatible release is not in this installation inventory")
        release = self.state["releases"][index]
        if (
            release["schema"] != self.state.get("migration_ledgers")
            or release["suppression_key_fingerprint"] != self.state["secret_fingerprints"]["recipient_hash"]
        ):
            raise ValueError("rollback schema/key contract is incompatible; databases are never rewound")
        self.state["queue_recovery_required"] = True
        self.save()
        self.configure_queues(pause_processing=True)
        self.state["phase"] = "rolling-back"
        self.save()
        for role, version in release["versions"].items():
            self.api.publish(self.name(role), version)
        self.state["build"] = release["commit"]
        self.state["deployment"] = {
            "commit": release["commit"],
            "versions": release["versions"],
            "complete": True,
            "sending": True,
        }
        self.save()
        record = self.verifier("record")
        paused = self.state.get("automation_paused", False)
        if not paused:
            self.recover_paused_hints()
        self.configure_queues(pause_processing=paused)
        self.state["queue_recovery_required"] = paused
        self.state["phase"] = "paused" if paused else "active"
        self.save()
        return {
            "phase": self.state["phase"],
            "rollback_commit": release["commit"],
            "record_revision": record["revision"],
            "database_rewind": False,
        }

    def db(self, role):
        return self.state["resources"]["database:" + role]["id"]

    def migrations(self):
        return {
            "commerce": [
                *sorted((ROOT / "migrations").glob("*.sql")),
                *sorted((ROOT / "modules/reviews/automation-intake-producer/migrations").glob("*.sql")),
            ],
            "reviews": [
                ROOT / "modules/reviews" / item
                for item in json.loads((ROOT / "modules/reviews/module.json").read_text())["migrations"]
            ],
            "context": sorted((ROOT / "modules/reviews/consent/migrations").glob("*.sql")),
        }

    def migrate(self):
        self.assert_resources()
        self.vault()
        for role, paths in self.migrations().items():
            db = self.db(role)
            self.api.query(
                db,
                "CREATE TABLE IF NOT EXISTS cartray_operator_migrations "
                "(position INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, checksum TEXT NOT NULL)",
            )
            rows = self.api.query(
                db, "SELECT position,path,checksum FROM cartray_operator_migrations ORDER BY position"
            )
            expected = [
                {"position": i + 1, "path": str(path.relative_to(ROOT)), "checksum": digest(path.read_bytes())}
                for i, path in enumerate(paths)
            ]
            if rows != expected[: len(rows)]:
                raise ValueError(
                    f"{role}: unknown, reordered or changed migration; no downgrade or repair is automatic"
                )
            for item, path in zip(expected[len(rows) :], paths[len(rows) :], strict=True):
                sql = (
                    path.read_text()
                    + "\nINSERT INTO cartray_operator_migrations VALUES "
                    + (f"({item['position']},'{item['path']}','{item['checksum']}');\n")
                )
                self.execute_file(db, sql)
            self.state.setdefault("migration_ledgers", {})[role] = expected
            self.save()
        self.pin_suppression(allow_create=True)
        self.state["phase"] = "migrated"
        self.save()
        return {role: len(paths) for role, paths in self.migrations().items()}

    def execute_file(self, database, sql):
        config = self.directory / "d1.toml"
        config.write_text(
            f'account_id = "{self.config["account_id"]}"\n'
            f'[[d1_databases]]\nbinding = "DB"\ndatabase_name = "{database}"\n'
            f'database_id = "{database}"\n'
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", dir=self.directory) as stream:
            stream.write(sql)
            stream.flush()
            subprocess.run(
                [
                    str(ROOT / "node_modules/.bin/wrangler"),
                    "d1",
                    "execute",
                    "DB",
                    "--remote",
                    "--yes",
                    "--config",
                    str(config),
                    "--file",
                    stream.name,
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                env={**os.environ, "CLOUDFLARE_API_TOKEN": self.credentials["cloudflare_api_token"]},
            )

    def pin_suppression(self, allow_create=False):
        self.assert_resources()
        vault = self.vault()
        params = ["acorn-store", "test", self.config["namespace"]]
        check = hmac.new(
            vault["recipient_hash"].encode(),
            json.dumps(["reviews-suppression-key-check-v1", *params, "key1"], separators=(",", ":")).encode(),
            "sha256",
        ).hexdigest()
        current = self.api.query(
            self.db("context"),
            "SELECT c.key_version,r.key_check FROM "
            "review_suppression_key_current c JOIN review_suppression_key_registry r "
            "USING(store_key,environment,installation_id,key_version) "
            "WHERE c.store_key=? AND c.environment=? AND c.installation_id=?",
            params,
        )
        if current:
            if current != [{"key_version": "key1", "key_check": check}]:
                raise ValueError("suppression key identity mismatch; restore original key, no rotation is performed")
            return
        if not allow_create:
            raise ValueError("suppression registry is missing; run the explicit migration step")
        registered = self.api.query(
            self.db("context"),
            "SELECT key_check FROM review_suppression_key_registry WHERE store_key=? AND environment=? "
            "AND installation_id=? AND key_version='key1'",
            params,
        )
        if registered and registered != [{"key_check": check}]:
            raise ValueError("suppression registry differs; restore the original key")
        self.api.query(
            self.db("context"),
            "INSERT INTO review_suppression_key_registry VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            [*params, "key1", check],
        )
        self.api.query(
            self.db("context"), "INSERT INTO review_suppression_key_current VALUES (?,?,?,?)", [*params, "key1"]
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True, help="private installation state directory")
    parser.add_argument("--credentials", type=Path, help="private documented test credential handoff JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="write a non-secret plan; creates no cloud resources or keys")
    plan.add_argument("--config", type=Path, required=True)
    for name in ("provision", "migrate", "build", "deploy", "verify", "doctor", "upgrade", "receipt", "pause"):
        commands.add_parser(name)
    activation = commands.add_parser(
        "activate", help="explicitly authorize the fixed no-backfill fence and test delivery"
    )
    activation.add_argument("--enable-test-delivery", action="store_true")
    rollback = commands.add_parser("rollback", help="compatible recorded Worker versions only; never rewind D1")
    rollback.add_argument("--release", type=int, default=-2, help="inventory release index (default previous)")
    request = commands.add_parser("request", help="operator-side call to the existing private Reviews service")
    request.add_argument("--path", required=True)
    request.add_argument("--method", choices=["GET", "POST"], default="GET")
    request.add_argument("--body", type=Path)
    request.add_argument("--admin", action="store_true")
    args = parser.parse_args()
    try:
        args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (args.directory / ".operator.lock").open("w") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "plan":
                # The lock is not installation content.
                if (args.directory / "inventory.json").exists():
                    raise ValueError("inventory already exists; resume its commands instead")
                config = json.loads(args.config.read_text())
                installation = Installation.plan(args.directory, config)
                result = {
                    "phase": "planned",
                    "workers": {role: installation.name(role) for role in ROLES},
                    "databases": {role: installation.name(role) for role in DATABASES},
                    "queues": {role: installation.name(role) for role in QUEUES},
                    "pages": installation.name("store"),
                }
            else:
                credentials = read_private(args.credentials) if args.credentials else None
                installation = Installation(args.directory, credentials)
                if args.command == "activate":
                    result = installation.activate(args.enable_test_delivery)
                elif args.command == "rollback":
                    result = installation.rollback(args.release)
                elif args.command == "upgrade":
                    result = installation.upgrade()
                elif args.command == "request":
                    result = installation.private(
                        "REVIEWS",
                        args.path,
                        json.loads(args.body.read_text()) if args.body else None,
                        installation.vault()["admin"] if args.admin else None,
                        args.method,
                    )
                else:
                    result = getattr(installation, args.command)()
            print(json.dumps(result, indent=2))
    except (ValueError, CloudError, OSError, subprocess.CalledProcessError) as error:
        message = (
            str(error)
            if not isinstance(error, subprocess.CalledProcessError)
            else ("required local build/migration command failed; retained installation state is resumable")
        )
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
