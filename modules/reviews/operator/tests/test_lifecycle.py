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
        if path.startswith(("workers/scripts/", "pages/projects/")):
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


def lifecycle(tmp_path):
    install, database = migrated(tmp_path)
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
    for role in operations.QUEUES:
        install.state["resources"]["queue:" + role] = {
            "name": install.name(role),
            "id": hashlib.md5(role.encode(), usedforsecurity=False).hexdigest(),
        }
    for role in operations.ROLES:
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


def test_connected_build_deploy_verify_explicit_activation_upgrade_and_rollback(tmp_path):
    install, provider, database, calls, revision = lifecycle(tmp_path)
    keys = install.vault()
    first = build_at(install, "a" * 40)
    assert build_at(install, "a" * 40) == first
    install.deploy()
    assert len(provider.active) == 7
    install.verify()
    assert calls == []
    assert revision[0] == 0
    assert install.state["activation_authorized"] is False
    with pytest.raises(ValueError, match="explicit"):
        install.activate()
    install.activate(True)
    fence = install.state["activation_watermark"]
    writes = database.writes
    assert install.doctor()["healthy"] is True
    assert database.writes == writes
    first_versions = dict(install.state["deployment"]["versions"])
    install.activate(True)
    assert install.state["deployment"]["versions"] == first_versions
    assert revision[0] == 2
    build_at(install, "b" * 40)
    install.deploy()
    install.activate(True)
    assert install.state["activation_watermark"] == fence
    assert install.vault() == keys
    assert len(install.state["releases"]) == 2
    writes = database.writes
    result = install.rollback(0)
    assert result["database_rewind"] is False
    assert database.writes == writes
    assert install.state["deployment"]["versions"] == first_versions
    assert install.doctor()["healthy"] is True


def test_tampered_retained_artifact_stops_before_upload(tmp_path):
    install, provider, _, _, _ = lifecycle(tmp_path)
    build_at(install, "a" * 40)
    artifact = install.directory / "releases" / ("a" * 40) / "source/bundle/worker.py"
    artifact.write_text("different code")
    before = list(provider.calls)
    with pytest.raises(ValueError, match="retained artifact changed"):
        install.deploy()
    assert [call for call in provider.calls if call[1] != "GET"] == [call for call in before if call[1] != "GET"]


def test_wrong_live_binding_is_diagnosed_without_provider_mutation(tmp_path):
    install, provider, database, _, _ = lifecycle(tmp_path)
    build_at(install, "a" * 40)
    install.deploy()
    install.activate(True)
    value = provider.versions[provider.active[install.name("producer")]]
    binding = next(item for item in value["metadata"]["bindings"] if item["name"] == "COMMERCE_DB")
    binding["id"] = "foreign-db"
    calls, writes = list(provider.calls), database.writes
    result = install.doctor()
    assert result["healthy"] is False
    assert "producer.bindings.COMMERCE_DB: wrong database" in result["errors"]
    assert [call for call in provider.calls if call[1] != "GET"] == [call for call in calls if call[1] != "GET"]
    assert database.writes == writes
