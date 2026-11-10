"""Public command dispatch and private operator transport contracts."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_operations import operations, planned


@pytest.mark.parametrize(
    "command",
    [
        "provision",
        "migrate",
        "build",
        "deploy",
        "verify",
        "doctor",
        "upgrade",
        "receipt",
        "activate",
        "rollback",
        "request",
        "pause",
    ],
)
def test_cli_dispatches_documented_commands(tmp_path, capsys, command):
    install = planned(tmp_path)
    install.credentials = {"cloudflare_api_token": "private-token"}
    args = ["operate.py", "--directory", str(install.directory), command]
    if command == "activate":
        args += ["--enable-test-delivery"]
    if command == "request":
        args += ["--path", "/reviews-admin/v1/status", "--admin"]
    method = "private" if command == "request" else command
    with (
        patch.object(sys, "argv", args),
        patch.object(operations, "Installation", return_value=install),
        patch.object(install, method, return_value={"ok": True}) as action,
    ):
        if command == "request":
            install.vault(create=True)
        operations.main()
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    if command == "activate":
        action.assert_called_once_with(True)
    elif command == "rollback":
        action.assert_called_once_with(-2)
    else:
        action.assert_called_once()


def test_private_transport_passes_credentials_only_in_environment_and_protected_input(tmp_path):
    install = planned(tmp_path)
    install.credentials = {"cloudflare_api_token": "private-token"}
    response = SimpleNamespace(returncode=0, stdout='diagnostic\n{"status":200,"body":"{\\"ok\\":true}"}\n', stderr="")
    with patch.object(operations.subprocess, "run", return_value=response) as run:
        assert install.private("REVIEWS", "/reviews-admin/v1/status", token="admin-secret", method="GET") == {
            "ok": True
        }
    args, kwargs = run.call_args
    assert "private-token" not in str(args)
    assert "admin-secret" not in str(args)
    assert kwargs["env"]["CLOUDFLARE_API_TOKEN"] == "private-token"
    assert json.loads(kwargs["input"])["headers"]["authorization"] == "Bearer admin-secret"
    with patch.object(
        operations.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="private detail", stderr="")
    ):
        with pytest.raises(operations.CloudError, match="protected"):
            install.private("REVIEWS", "/status")
    assert (install.directory / "private-service-error.log").stat().st_mode & 0o777 == 0o600
