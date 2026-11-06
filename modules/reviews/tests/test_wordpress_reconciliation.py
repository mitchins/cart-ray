"""The restricted reconciliation accounts for every source and metadata row."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_wordpress_draft import build_draft, candidate

PATH = Path(__file__).resolve().parents[1] / "migration/wordpress/reconcile.py"
SPEC = importlib.util.spec_from_file_location("wordpress_reconciliation", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MAPPING = {"99": {"id": "subject", "kind": "service", "path": "/service/"}}


def source_candidate():
    value = candidate()
    value["subjects"] = [{"ID": "99", "post_type": "product", "post_status": "publish", "password_protected": "0"}]
    return value


def test_reconciliation_accounts_for_public_and_restricted_rows():
    value = source_candidate()
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    spam = dict(value["comments"][0], comment_ID="2", comment_approved="spam", comment_content="private canary")
    value["comments"].append(spam)
    value["commentmeta"].append(
        {"meta_id": "3", "comment_id": "2", "meta_key": "attachment_url", "meta_value": "private"}
    )
    raw = json.dumps(value).encode()
    draft = build_draft(raw, MAPPING, store="test-store", environment="test", expected_source="https://example.invalid")
    result = MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid")
    assert result["complete"] is False
    assert result["record_counts"] == {"candidate_review": 1, "restricted_spam": 1}
    assert len(result["metadata_dispositions"]) == 3
    assert "private canary" not in json.dumps(result)
    assert "private" not in json.dumps(result)
    value["commentmeta"].append({"meta_id": "4", "comment_id": "missing", "meta_key": "rating", "meta_value": "1"})
    bad_raw = json.dumps(value).encode()
    bad_draft = build_draft(
        bad_raw, MAPPING, store="test-store", environment="test", expected_source="https://example.invalid"
    )
    with pytest.raises(ValueError, match="orphaned"):
        MODULE.reconcile(bad_raw, bad_draft, MAPPING, expected_source="https://example.invalid")


def test_reconciliation_rejects_changed_draft_content():
    value = source_candidate()
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    raw = json.dumps(value).encode()
    draft = build_draft(raw, MAPPING, store="test-store", environment="test", expected_source="https://example.invalid")
    draft["snapshot"]["contributions"][0]["body"] = "fabricated"
    with pytest.raises(ValueError, match="match source"):
        MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid")
    draft = build_draft(raw, MAPPING, store="test-store", environment="test", expected_source="https://example.invalid")
    draft["snapshot"]["contributions"][0]["verification"] = "email_only"
    with pytest.raises(ValueError, match="metadata does not match"):
        MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid")


def test_reconciliation_rejects_undispositioned_approved_media():
    value = source_candidate()
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    raw = json.dumps(value).encode()
    draft = build_draft(raw, MAPPING, store="test-store", environment="test", expected_source="https://example.invalid")
    value["commentmeta"].append({"meta_id": "3", "comment_id": "1", "meta_key": "ivole_media_count", "meta_value": "1"})
    raw = json.dumps(value).encode()
    draft["snapshot"]["source"]["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="media requires"):
        MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid")


def test_approved_review_on_private_subject_requires_disposition():
    value = source_candidate()
    value["subjects"][0]["post_status"] = "private"
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    raw = json.dumps(value).encode()
    draft = build_draft(raw, MAPPING, store="test-store", environment="test", expected_source="https://example.invalid")
    with pytest.raises(ValueError, match="nonpublic source subject"):
        MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid")


@pytest.mark.parametrize("protection", ["1", None, 0.0, False])
def test_password_protected_source_subject_requires_disposition(protection):
    value = source_candidate()
    value["subjects"][0]["password_protected"] = protection
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    raw = json.dumps(value).encode()
    draft = build_draft(raw, MAPPING, store="test-store", environment="test", expected_source="https://example.invalid")
    with pytest.raises(ValueError, match="password-protected"):
        MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid")


def test_cli_writes_restricted_incomplete_output_without_overwriting(tmp_path):
    value = source_candidate()
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    source, mapping, output = tmp_path / "source.json", tmp_path / "mapping.json", tmp_path / "restricted"
    source.write_text(json.dumps(value))
    mapping.write_text(json.dumps(MAPPING))
    cmd = [
        sys.executable,
        str(PATH),
        "--source",
        str(source),
        "--mapping",
        str(mapping),
        "--expected-source",
        "https://example.invalid",
        "--store",
        "test-store",
        "--output-directory",
        str(output),
    ]
    env = os.environ | {"PYTHONPATH": str(PATH.parents[2] / "src")}
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((output / "draft.json").read_text())["snapshot"]["source"]["complete"] is False
    assert json.loads((output / "reconciliation.json").read_text())["complete"] is False
    assert output.stat().st_mode & 0o077 == 0
    assert (output / "draft.json").stat().st_mode & 0o077 == 0
    assert subprocess.run(cmd, env=env, capture_output=True).returncode != 0
    mapping.write_bytes(b" " * 1_000_001)
    oversized_output = tmp_path / "oversized"
    oversized_cmd = cmd[:-1] + [str(oversized_output)]
    assert subprocess.run(oversized_cmd, env=env, capture_output=True).returncode != 0
    assert not oversized_output.exists()


def test_reconciliation_rejects_source_email_in_candidate_text():
    value = source_candidate()
    value["comments"][0]["comment_author"] = value["comments"][0]["comment_author_email"]
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    raw = json.dumps(value).encode()
    with pytest.raises(ValueError, match="email-like public text"):
        build_draft(raw, MAPPING, store="test-store", environment="test", expected_source="https://example.invalid")


def test_explicit_email_redaction_is_recorded_without_exposing_source_email():
    value = source_candidate()
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    email = value["comments"][0]["comment_author_email"]
    value["comments"][0]["comment_content"] = "Contact " + email + "."
    raw = json.dumps(value).encode()
    draft = build_draft(
        raw,
        MAPPING,
        store="test-store",
        environment="test",
        expected_source="https://example.invalid",
        redact_source_emails=True,
    )
    report = MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid", redact_source_emails=True)
    assert draft["snapshot"]["contributions"][0]["body"] == "Contact [email removed]."
    assert report["privacy_redactions"] == [{"source_id": "1", "field": "body", "reason": "source_email"}]
    assert email not in json.dumps(draft) and email not in json.dumps(report)
    with pytest.raises(ValueError, match="match source"):
        MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid")


@pytest.mark.parametrize(
    "email,body,expected",
    [
        ("alice@example.com", "Contact 'alice@example.com'.", "Contact '[email removed]'."),
        ("o'neil@example.com", "Email o'neil@example.com.", "Email [email removed]."),
        ("alice@example.com", "prefixalice@example.com", "prefix[email removed]"),
    ],
)
def test_redaction_handles_punctuation_and_address_substrings(email, body, expected):
    value = source_candidate()
    value["comments"][0]["comment_author_email"] = email
    value["comments"][0]["comment_content"] = body
    for index, row in enumerate(value["commentmeta"]):
        row["meta_id"] = str(index + 1)
    raw = json.dumps(value).encode()
    draft = build_draft(
        raw,
        MAPPING,
        store="test-store",
        environment="test",
        expected_source="https://example.invalid",
        redact_source_emails=True,
    )
    report = MODULE.reconcile(raw, draft, MAPPING, expected_source="https://example.invalid", redact_source_emails=True)
    assert draft["snapshot"]["contributions"][0]["body"] == expected
    assert report["privacy_redactions"] == [{"source_id": "1", "field": "body", "reason": "source_email"}]
