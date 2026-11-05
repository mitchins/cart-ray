"""Synthetic-only mailbox delivery for the local purchase-review journey.

This module deliberately has no production transport. Callers must establish
recipient binding and purpose-bound consent before invoking the test sink.
"""

import hashlib
import hmac
import mailbox
import re
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def deliver_bound_invitation(directory, *, acquisition_id, expected_recipient_hash, resolve_recipient, **message):
    """Resolve a synthetic recipient from the trusted acquisition binding."""
    recipient = resolve_recipient(acquisition_id)
    if not isinstance(recipient, str) or not isinstance(expected_recipient_hash, str):
        raise ValueError("recipient unavailable")
    # This test-only fixture uses SHA-256; a real recipient adapter must use its
    # own authenticated, privacy-preserving binding and transport.
    actual = hashlib.sha256(recipient.encode()).hexdigest()
    if not hmac.compare_digest(actual, expected_recipient_hash):
        raise ValueError("recipient unavailable")
    return deliver_invitation(directory, recipient=recipient, **message)


def deliver_invitation(directory, *, recipient, store_name, landing_url, token, delivery_identity=None):
    """Write one synthetic email into a local Maildir and return its key."""
    if not isinstance(recipient, str) or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", recipient):
        raise ValueError("invalid synthetic recipient")
    if not isinstance(store_name, str) or not 1 <= len(store_name.strip()) <= 200 or "\n" in store_name:
        raise ValueError("invalid store name")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invalid invitation capability")
    if delivery_identity is not None and (
        not isinstance(delivery_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", delivery_identity)
    ):
        raise ValueError("invalid delivery identity")
    url = urlsplit(landing_url)
    if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1") or url.fragment or url.query:
        raise ValueError("test mailbox requires a local landing URL")
    link = urlunsplit((url.scheme, url.netloc, url.path, "", f"invitation={token}"))
    message = EmailMessage()
    message["From"] = "reviews@example.test"
    message["To"] = recipient
    message["Subject"] = f"Share your experience with {store_name.strip()}"
    if delivery_identity is not None:
        message["X-Reviews-Delivery-Identity"] = delivery_identity
    message.set_content(
        f"Thank you for your purchase from {store_name.strip()}.\n\n"
        "If you would like to share your experience, you can leave a review here:\n\n"
        f"{link}\n\n"
        "Your review will be checked before it appears publicly. "
        "This link can be used by anyone who has it, so please keep it private.\n"
    )
    box = mailbox.Maildir(Path(directory), create=True)
    try:
        return box.add(message)
    finally:
        box.close()
