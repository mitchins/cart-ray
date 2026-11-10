"""Authenticated operator transport. Credentials never enter inventories or output."""

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE = 40 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args):
        raise CloudError("provider redirect refused; credentials are not forwarded")


class CloudError(RuntimeError):
    """A bounded provider operation failed without exposing request credentials."""


class API:
    def __init__(self, account, token):
        self.account = account
        self.token = token

    def request(self, path, method="GET", data=None, raw=False, headers=None):
        request_headers = {"Authorization": "Bearer " + self.token, **(headers or {})}
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        url = "https://api.cloudflare.com/client/v4/accounts/" + self.account + "/" + path
        request = urllib.request.Request(url, method=method, headers=request_headers, data=data)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise CloudError("provider response exceeds operator bound")
                if raw:
                    return response.headers.get("Content-Type"), body
        except urllib.error.HTTPError as error:
            try:
                codes = [item.get("code") for item in json.loads(error.read(65536)).get("errors", [])]
            except (ValueError, TypeError):
                codes = []
            remedy = "; account resource limit: supply approved capacity then resume" if 7406 in codes else ""
            raise CloudError(f"Cloudflare {method} {path}: HTTP {error.code}, provider codes {codes}{remedy}") from None
        except (urllib.error.URLError, TimeoutError):
            raise CloudError(f"Cloudflare {method} {path}: unavailable; retry the same command") from None
        result = json.loads(body)
        if result.get("success") is not True:
            codes = [item.get("code") for item in result.get("errors", [])]
            raise CloudError(f"Cloudflare {method} {path}: provider codes {codes}")
        return result.get("result")

    def listing(self, path):
        rows = []
        page_size = 10 if path.startswith("pages/") else 100
        for page in range(1, 101):
            batch = self.request(path + ("&" if "?" in path else "?") + f"page={page}&per_page={page_size}")
            if isinstance(batch, dict):
                batch = batch.get("queues", batch.get("items", []))
            if not isinstance(batch, list):
                raise CloudError("provider list has an unexpected shape")
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
        raise CloudError("resource list exceeds operator bound")

    def query(self, database, sql, params=()):
        result = self.request(f"d1/database/{database}/query", "POST", {"sql": sql, "params": list(params)})
        if not isinstance(result, list) or any(row.get("success") is not True for row in result):
            raise CloudError("D1 operator query did not succeed")
        return result[0].get("results", [])

    def upload(self, name, modules, metadata, create=False):
        boundary = "----cartray-operator-" + secrets.token_hex(16)
        chunks = []
        for filename, content, kind in [("metadata", json.dumps(metadata).encode(), "application/json"), *modules]:
            disposition = f'form-data; name="{filename}"'
            if filename != "metadata":
                disposition += f'; filename="{filename}"'
            chunks.append(
                (f"--{boundary}\r\nContent-Disposition: {disposition}\r\nContent-Type: {kind}\r\n\r\n").encode()
                + content
                + b"\r\n"
            )
        body = b"".join(chunks) + f"--{boundary}--\r\n".encode()
        return self.request(
            f"workers/scripts/{name}" + ("" if create else "/versions"),
            "PUT" if create else "POST",
            body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        )

    def publish(self, name, version):
        return self.request(
            f"workers/scripts/{name}/deployments",
            "POST",
            {"strategy": "percentage", "versions": [{"version_id": version, "percentage": 100}]},
        )

    def version(self, name):
        deployments = self.request(f"workers/scripts/{name}/deployments")
        rows = deployments if isinstance(deployments, list) else deployments.get("deployments", [])
        if not rows:
            raise CloudError(f"{name}: no deployed version")
        current = max(rows, key=lambda item: item["created_on"])
        if len(current["versions"]) != 1 or current["versions"][0]["percentage"] != 100:
            raise CloudError(f"{name}: traffic split is incompatible with verified installation")
        return self.request(f"workers/scripts/{name}/versions/{current['versions'][0]['version_id']}")


def stripe(key, path, form=None, idempotency=None):
    if not key.startswith(("sk_test_", "rk_test_")):
        raise CloudError("only a Stripe test key is accepted")
    headers = {"Authorization": "Bearer " + key, "Stripe-Version": "2025-09-30.clover"}
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            urllib.request.Request("https://api.stripe.com/v1/" + path, headers=headers, data=data), timeout=30
        ) as response:
            return json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        status = getattr(error, "code", "unavailable")
        raise CloudError(f"Stripe test operation {path}: {status}; retry without changing the installation") from None
