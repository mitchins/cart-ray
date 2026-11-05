"""The local email sink never sends mail and preserves fragment capabilities."""

import importlib.util
import mailbox
from pathlib import Path
from urllib.parse import urlsplit

import pytest

MODULE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reviews_test_mailbox", MODULE / "tools/test_mailbox.py")
sink = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sink)


def test_synthetic_mailbox_renders_local_link_without_order_details(tmp_path):
    token = "a" * 64
    key = sink.deliver_invitation(
        tmp_path / "mail",
        recipient="buyer@example.test",
        store_name="Example Store",
        landing_url="http://127.0.0.1:3000/review/",
        token=token,
    )
    box = mailbox.Maildir(tmp_path / "mail", create=False)
    message = box[key]
    assert message["To"] == "buyer@example.test"
    assert "Example Store" in message["Subject"]
    text = message.get_payload(decode=True).decode()
    link = next(line.strip() for line in text.splitlines() if line.startswith("http://"))
    parsed = urlsplit(link)
    assert parsed.path == "/review/" and parsed.query == "" and parsed.fragment == "invitation=" + token
    assert "order_" not in text


@pytest.mark.parametrize("url", ["https://example.com/", "http://example.com/", "http://localhost/#token"])
def test_test_mailbox_refuses_nonlocal_or_fragment_urls(tmp_path, url):
    with pytest.raises(ValueError):
        sink.deliver_invitation(
            tmp_path / "mail",
            recipient="buyer@example.test",
            store_name="Store",
            landing_url=url,
            token="a" * 64,
        )


def test_bound_delivery_rejects_redirected_or_unknown_recipient(tmp_path):
    import hashlib

    address = "buyer@example.test"
    kwargs = dict(
        acquisition_id="purchase-1",
        expected_recipient_hash=hashlib.sha256(address.encode()).hexdigest(),
        store_name="Store",
        landing_url="http://localhost/review/",
        token="a" * 64,
    )
    for resolver in (lambda _: "different@example.test", lambda _: None):
        with pytest.raises(ValueError):
            sink.deliver_bound_invitation(tmp_path / "mail", resolve_recipient=resolver, **kwargs)
    assert not (tmp_path / "mail").exists()
    sink.deliver_bound_invitation(tmp_path / "mail", resolve_recipient=lambda _: address, **kwargs)
    box = mailbox.Maildir(tmp_path / "mail", create=False)
    assert len(box) == 1


@pytest.mark.parametrize(
    "recipient",
    [
        "@example.test",
        "a@",
        "a@@example.test",
        "a @example.test",
        "alice@example.test,bob",
        "alice@example.test;bob",
        "a\x00@example.test",
        "a@" + "x" * 10000,
    ],
)
def test_test_mailbox_rejects_invalid_or_oversized_recipients(tmp_path, recipient):
    with pytest.raises(ValueError, match="invalid synthetic recipient"):
        sink.deliver_invitation(
            tmp_path / "mail",
            recipient=recipient,
            store_name="Store",
            landing_url="http://localhost/review/",
            token="a" * 64,
        )
