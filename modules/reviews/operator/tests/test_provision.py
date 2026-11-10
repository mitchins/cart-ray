"""Provider journal recovery and repeat provisioning without duplicate identities."""

import copy
from unittest.mock import patch

from test_operations import operations, planned


class Catalog:
    def __init__(self):
        self.items = {key: [] for key in ("d1/database", "queues", "workers/scripts", "pages/projects")}
        self.creates = 0

    def listing(self, path):
        return copy.deepcopy(self.items[path])

    def request(self, path, method="GET", data=None):
        if path == "workers/subdomain":
            return {"subdomain": "operator-account"}
        if method == "POST":
            self.creates += 1
            value = dict(data)
            if path == "d1/database":
                value["uuid"] = f"database-{self.creates}"
            elif path == "queues":
                value["queue_id"] = f"queue-{self.creates}"
            self.items[path].append(value)
            return value
        if method == "PATCH":
            item = next(value for value in self.items["queues"] if value["queue_id"] == path.split("/")[-1])
            item["settings"] = data["settings"]
            return {}
        if path.startswith("d1/database/"):
            return next(item for item in self.items["d1/database"] if item["uuid"] == path.split("/")[-1])
        if path.startswith("queues/"):
            return next(item for item in self.items["queues"] if item["queue_id"] == path.split("/")[-1])
        raise AssertionError(path)

    def upload(self, name, modules, metadata, create=False):
        assert create
        assert metadata["bindings"] == []
        assert b"status:503" in modules[0][1]
        self.creates += 1
        self.items["workers/scripts"].append({"id": name})


def test_provision_repeat_and_unknown_create_outcome_preserve_keys_and_resources(tmp_path):
    install = planned(tmp_path)
    install.credentials = {"stripe_test_key": "sk_test_only"}
    provider = Catalog()
    install.api = provider
    # A provider created this DB before the operator process died saving its response.
    provider.items["d1/database"].append({"name": install.name("commerce"), "uuid": "recovered-commerce"})
    install.state["pending"].append("database:commerce")
    install.save()

    def stripe(key, path, data=None, idempotency=None):
        if path == "account":
            return {"id": "acct_Test"}
        assert idempotency == install.name("webhook-v1")
        return {"id": "we_test", "secret": "whsec_test"}

    with patch.object(operations, "stripe", side_effect=stripe) as calls:
        first = copy.deepcopy(install.provision())
        keys = install.vault()
        count = provider.creates
        install.provision()
        assert install.state["resources"] == first
        assert install.vault() == keys
        assert provider.creates == count
        assert sum(call.args[1] == "webhook_endpoints" for call in calls.call_args_list) == 1
        install.state["phase"] = "paused"
        install.state["automation_paused"] = True
        install.save()
        install.provision()
        assert provider.creates == count
        for queue in provider.items["queues"]:
            assert queue["settings"]["delivery_paused"] is (queue["queue_name"] != install.name("dlq"))
    assert len(first) == 15
    assert install.state["pending"] == []
    assert first["database:commerce"]["id"] == "recovered-commerce"
    assert install.state["phase"] == "paused"


def test_initial_key_and_webhook_receipts_recover_after_inventory_write_interruption(tmp_path):
    install = planned(tmp_path)
    original = install.vault(create=True)
    install.state["secret_fingerprints"] = {}
    install.save()
    assert install.vault(create=True) == original
    assert install.state["secret_fingerprints"]
    original["webhook_secret"] = "whsec_retained"
    original["webhook_endpoint"] = {"id": "we_retained", "url": "https://source.example.test/stripe/webhook"}
    operations.atomic_json(install.vault_path, original)
    with patch.object(operations, "stripe") as provider:
        install.provision_webhook()
    provider.assert_not_called()
    assert install.state["webhook"] == original["webhook_endpoint"]
