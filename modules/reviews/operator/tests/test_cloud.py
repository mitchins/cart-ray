"""Provider adapter contracts without live credentials or cloud mutation."""

import io
import json
import urllib.error
from unittest.mock import patch

import pytest
from test_operations import operations

cloud = operations.API.__module__
cloud = __import__(cloud)


class Reply:
    def __init__(self, body, content_type="application/json"):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum=None):
        return self.body if maximum is None else self.body[:maximum]


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_transport_scopes_auth_and_json_without_exposing_key():
    opener = Opener(Reply({"success": True, "result": {"id": "created"}}))
    api = cloud.API("a" * 32, "private-operator-key")
    with patch.object(cloud.urllib.request, "build_opener", return_value=opener):
        assert api.request("d1/database", "POST", {"name": "fresh-test"}) == {"id": "created"}
    request, timeout = opener.calls[0]
    assert request.full_url.startswith("https://api.cloudflare.com/client/v4/accounts/" + "a" * 32)
    assert json.loads(request.data) == {"name": "fresh-test"}
    assert timeout == 60
    assert request.headers["Authorization"] == "Bearer private-operator-key"


@pytest.mark.parametrize(
    "response,match",
    [
        (
            urllib.error.HTTPError(
                "https://provider", 403, "Denied", {}, io.BytesIO(json.dumps({"errors": [{"code": 7406}]}).encode())
            ),
            "7406",
        ),
        (urllib.error.URLError("offline"), "resume|retry"),
        (TimeoutError(), "resume|retry"),
        (Reply({"success": False, "errors": [{"code": 10000}]}), "10000"),
    ],
)
def test_transport_errors_preserve_operation_and_never_credentials(response, match):
    with patch.object(cloud.urllib.request, "build_opener", return_value=Opener(response)):
        with pytest.raises(cloud.CloudError, match=match) as error:
            cloud.API("a" * 32, "private-operator-key").request("d1/database")
    assert "private-operator-key" not in str(error.value)


def test_transport_bounds_raw_response_and_rejects_redirects():
    with patch.object(cloud.urllib.request, "build_opener", return_value=Opener(Reply(b"abcd"))):
        assert cloud.API("a" * 32, "key").request("worker/content", raw=True) == ("application/json", b"abcd")
        with patch.object(cloud, "MAX_RESPONSE", 2):
            with pytest.raises(cloud.CloudError, match="bound"):
                cloud.API("a" * 32, "key").request("worker/content")
    with pytest.raises(cloud.CloudError, match="redirect"):
        cloud.NoRedirect().redirect_request(None, None, None, None, None, None)


def test_pagination_handles_pages_limit_and_queue_shapes():
    api = cloud.API("a" * 32, "key")
    with patch.object(api, "request", side_effect=[[{}] * 10, [{"name": "last"}]]) as request:
        assert len(api.listing("pages/projects")) == 11
        assert "per_page=10" in request.call_args_list[0].args[0]
    with patch.object(api, "request", return_value={"queues": [{"queue_name": "one"}]}):
        assert api.listing("queues") == [{"queue_name": "one"}]
    with patch.object(api, "request", return_value="invalid"):
        with pytest.raises(cloud.CloudError, match="shape"):
            api.listing("queues")
    with patch.object(api, "request", return_value=[{}] * 100):
        with pytest.raises(cloud.CloudError, match="bound"):
            api.listing("d1/database")


def test_query_upload_and_deployment_request_shapes():
    api = cloud.API("a" * 32, "key")
    with patch.object(api, "request", return_value=[{"success": True, "results": [{"value": 3}]}]) as request:
        assert api.query("db", "SELECT ?", [3]) == [{"value": 3}]
        assert request.call_args.args[2]["params"] == [3]
    with patch.object(api, "request", return_value=[{"success": False}]):
        with pytest.raises(cloud.CloudError, match="D1"):
            api.query("db", "SELECT 1")
    with patch.object(api, "request", return_value={"id": "version"}) as request:
        api.upload(
            "namespace-test-source",
            [("main.mjs", b"export default {}", "application/javascript+module")],
            {"main_module": "main.mjs"},
        )
        assert request.call_args.args[0].endswith("/versions")
        assert b"export default {}" in request.call_args.args[2]
        api.upload("namespace-test-source", [], {"main_module": "main.mjs"}, create=True)
        assert request.call_args.args[1] == "PUT"
        api.publish("namespace-test-source", "version")
        assert request.call_args.args[2]["versions"] == [{"version_id": "version", "percentage": 100}]


def test_version_resolves_only_one_hundred_percent_deployment():
    api = cloud.API("a" * 32, "key")
    deployments = {"deployments": [{"created_on": "today", "versions": [{"version_id": "version", "percentage": 100}]}]}
    with patch.object(api, "request", side_effect=[deployments, {"id": "version"}]):
        assert api.version("namespace-test-source") == {"id": "version"}
    for value in [[], [{"created_on": "today", "versions": [{"version_id": "version", "percentage": 50}]}]]:
        with patch.object(api, "request", return_value=value):
            with pytest.raises(cloud.CloudError):
                api.version("namespace-test-source")


def test_stripe_refuses_live_key_and_preserves_stable_create_idempotency():
    with pytest.raises(cloud.CloudError, match="test key"):
        cloud.stripe("sk_live_forbidden", "account")
    opener = Opener(Reply({"id": "endpoint"}))
    with patch.object(cloud.urllib.request, "build_opener", return_value=opener):
        assert cloud.stripe("sk_test_allowed", "webhook_endpoints", {"url": "https://test.example"}, "stable") == {
            "id": "endpoint"
        }
    assert opener.calls[0][0].headers["Idempotency-key"] == "stable"
    opener = Opener(urllib.error.URLError("offline"))
    with patch.object(cloud.urllib.request, "build_opener", return_value=opener):
        with pytest.raises(cloud.CloudError, match="retry without changing"):
            cloud.stripe("sk_test_allowed", "account")


@pytest.mark.parametrize("endpoint", ["workers/scripts"])
def test_unpaged_provider_lists_do_not_repeat_full_sized_responses(endpoint):
    api = cloud.API("a" * 32, "key")
    values = [{"id": str(index)} for index in range(150)]
    with patch.object(api, "request", return_value=values) as request:
        assert api.listing(endpoint) == values
    request.assert_called_once_with(endpoint)


@pytest.mark.parametrize("body", [b"not json", [], None, {"success": False, "errors": [None, 1, {"code": 10000}]}])
def test_malformed_provider_responses_stay_bounded_cloud_errors(body):
    api = cloud.API("a" * 32, "private-key")
    with patch.object(cloud.urllib.request, "build_opener", return_value=Opener(Reply(body))):
        with pytest.raises(cloud.CloudError) as error:
            api.request("d1/database")
    assert "private-key" not in str(error.value)


@pytest.mark.parametrize("result", [[], [None], [3], [{"success": True, "results": [None]}]])
def test_malformed_d1_rows_report_cloud_error(result):
    api = cloud.API("a" * 32, "key")
    with patch.object(api, "request", return_value=result):
        with pytest.raises(cloud.CloudError, match="D1"):
            api.query("database", "SELECT 1")


@pytest.mark.parametrize("payload", [[], {"errors": [None, 1, {"code": 10000}]}])
def test_malformed_http_error_entries_never_escape_transport(payload):
    response = urllib.error.HTTPError("https://provider", 403, "Denied", {}, io.BytesIO(json.dumps(payload).encode()))
    api = cloud.API("a" * 32, "private-key")
    with patch.object(cloud.urllib.request, "build_opener", return_value=Opener(response)):
        with pytest.raises(cloud.CloudError, match="HTTP 403"):
            api.request("d1/database")


def test_queue_pagination_uses_provider_total_pages_and_includes_later_queues():
    api = cloud.API("a" * 32, "key")
    first = [{"queue_id": str(index)} for index in range(100)]
    second = [{"queue_id": "later-owned-queue"}]
    responses = [
        {"result": first, "result_info": {"total_pages": 2}},
        {"result": second, "result_info": {"total_pages": 2}},
    ]
    with patch.object(api, "request", side_effect=responses) as request:
        assert api.listing("queues") == first + second
    assert [call.args[0] for call in request.call_args_list] == ["queues?page=1", "queues?page=2"]
    assert all(call.kwargs == {"envelope": True} for call in request.call_args_list)
    with patch.object(api, "request", return_value={"result": first, "result_info": {"total_pages": 1}}) as request:
        assert api.listing("queues") == first
    request.assert_called_once_with("queues?page=1", envelope=True)
