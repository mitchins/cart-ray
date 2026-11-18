import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "post_purchase_recovery", Path(__file__).resolve().parents[1] / "scripts/post_purchase_recovery.py"
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["recovery", *args])
    calls = []
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    recovery.main()
    return calls


def test_recovery_requires_test_scope_and_explicit_paused_operator(monkeypatch):
    scope = ["--account", "a" * 32, "--database", "merchant-test", "--merchant", "merchant"]
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "hold", *scope)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "inspect", "--account", "a" * 32, "--database", "merchant-prod", "--merchant", "merchant")
    calls = invoke(monkeypatch, "inspect", *scope)
    assert "state IN ('claimed','uncertain')" in calls[0][0][0][-1]


def test_hold_quotes_evidence_and_binds_inspected_digest(monkeypatch):
    calls = invoke(
        monkeypatch, "hold", "--account", "a" * 32, "--database", "merchant-test", "--merchant", "merchant",
        "--order", "order-one", "--workflow", "flow-one", "--digest", "b" * 64, "--operator", "staff",
        "--evidence", "provider's receipt unknown", "--confirm-worker-paused",
    )
    command = calls[0][0][0]
    assert "--remote" in command
    assert "provider''s receipt unknown" in command[-1]
    assert "b" * 64 in command[-1]
    assert calls[0][1]["env"]["CLOUDFLARE_ACCOUNT_ID"] == "a" * 32
