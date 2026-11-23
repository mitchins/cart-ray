"""Connected operator lifecycle with real SQLite schemas and a bounded provider fake."""

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_operations import migrated, operations


class Provider:
    def __init__(self, database):
        self.database = database
        self.versions = {}
        self.active = {}
        self.calls = []
        self.consumers = {}
        self.serial = 0
        self.queue_settings = {}

    def query(self, *args):
        return self.database.query(*args)

    def request(self, path, method="GET", data=None):
        self.calls.append((path, method))
        if path.startswith("d1/"):
            return self.database.request(path)
        if path.startswith("queues/") and method == "GET":
            return {
                "queue_name": self.queue_names[path.rsplit("/", 1)[1]],
                "consumers": self.consumers.get(path.rsplit("/", 1)[1], []),
                "settings": self.queue_settings.get(path.rsplit("/", 1)[1], {}),
            }
        if path.startswith("queues/") and method == "PATCH":
            self.queue_settings[path.split("/")[1]] = data["settings"]
            return {}
        if path.startswith("queues/") and method == "POST":
            self.consumers[path.split("/")[1]] = [data]
            return {"consumer_id": "test-consumer"}
        if path.startswith("pages/projects/"):
            return {"subdomain": path.rsplit("/", 1)[1] + ".pages.dev"}
        if path.startswith("workers/scripts/"):
            return {}
        raise AssertionError(path)

    def upload(self, name, modules, metadata):
        self.serial += 1
        identifier = f"00000000-0000-4000-8000-{self.serial:012d}"
        self.versions[identifier] = {"id": identifier, "metadata": copy.deepcopy(metadata)}
        self.calls.append(("upload:" + name, "POST"))
        return {"id": identifier}

    def publish(self, name, identifier):
        assert identifier in self.versions
        self.active[name] = identifier
        self.calls.append(("publish:" + name, "POST"))

    def version(self, name):
        value = self.versions[self.active[name]]
        bindings = copy.deepcopy(value["metadata"]["bindings"])
        for item in bindings:
            if item["type"] == "d1":
                item["database_id"] = item.pop("id")
            if item["type"] == "secret_text":
                item.pop("text")
        return {"id": value["id"], "resources": {"bindings": bindings}}


def lifecycle(tmp_path, reviews_enabled=True):
    install, database = migrated(tmp_path)
    install.config["reviews_enabled"] = reviews_enabled
    install.migrate()
    install.credentials = {
        "cloudflare_api_token": "operator-token",
        "stripe_test_key": "sk_test_only",
        "smtp2go_api_key": "api-test",
        "approved_test_email": "approved@example.test",
        "test_sender": "sender@example.test",
    }
    install.state["workers_subdomain"] = "operator-test"
    vault = install.vault()
    vault["webhook_secret"] = "whsec_test"
    operations.atomic_json(install.vault_path, vault)
    for role in install.queues:
        install.state["resources"]["queue:" + role] = {
            "name": install.name(role),
            "id": hashlib.md5(role.encode(), usedforsecurity=False).hexdigest(),
        }
    for role in install.roles:
        install.state["resources"]["worker:" + role] = {"name": install.name(role), "id": install.name(role)}
    install.state["resources"]["pages:store"] = {"name": install.name("store"), "id": install.name("store")}
    provider = Provider(database)
    provider.queue_names = {
        value["id"]: value["name"] for key, value in install.state["resources"].items() if key.startswith("queue:")
    }
    install.api = provider
    install.save()

    def tools(step, command, environment=None):
        if step.startswith("build-") and "--output" in command:
            out = Path(command[command.index("--output") + 1])
            out.mkdir(parents=True)
            if step in ("build-source", "build-reviews"):
                (out / "bundle").mkdir()
                (out / "bundle/worker.py").write_text("# retained test artifact\n")
                (out / "receipt.json").write_text("{}")
            else:
                (out / "index.html").write_text("storefront")
        elif step.startswith("build-"):
            out = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--outfile=")))
            out.write_text("export default {}")

    install.run = tools
    revision = [0]
    checkpoint = [False]

    def verifier(mode):
        if mode == "record":
            revision[0] += 1
        if mode == "doctor":
            return {"status": "active" if revision[0] else "mismatch", "revision": revision[0], "mismatches": []}
        return {"revision": revision[0], "activation_status": "active" if checkpoint[0] else "verified"}

    install.verifier = verifier
    private_calls = []

    def private(service, path, body=None, token=None, method="POST"):
        private_calls.append((service, path))
        if service == "PRODUCER":
            checkpoint[0] = True
        return {"outbox_watermark": 42} if path.endswith("automation-activate") else {}

    install.private = private
    return install, provider, database, private_calls, revision


def build_at(install, commit):
    with (
        patch.object(operations.subprocess, "check_output", return_value=commit + "\n"),
        patch.object(operations.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
    ):
        return install.build()


































def test_read_only_reviews_install_has_no_sender_or_queue(tmp_path):
    install, provider, database, calls, revision = lifecycle(tmp_path)
    original = install.vault()
    build_at(install, "a" * 40)
    install.deploy()
    assert len(provider.active) == 2
    assert install.queues == ()
    assert install.vault() == original
    assert install.state["activation_authorized"] is False
    with pytest.raises(ValueError, match="shared order-confirmation"):
        install.activate(True)


def test_commerce_only_deploy_remains_independent(tmp_path):
    install, provider, database, calls, revision = lifecycle(tmp_path, False)
    build_at(install, "b" * 40)
    install.deploy()
    assert len(provider.active) == 1
    assert set(install.roles) == {"source"}
    assert not any(path.startswith("queues/") for path, _ in provider.calls)
