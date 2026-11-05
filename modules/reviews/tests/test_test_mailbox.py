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
