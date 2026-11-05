"""Synthetic-only mailbox delivery for the local purchase-review journey.

This module deliberately has no production transport. Callers must establish
recipient binding and purpose-bound consent before invoking the test sink.
"""

import mailbox
import re
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def deliver_invitation(directory, *, recipient, store_name, landing_url, token):
    """Write one synthetic email into a local Maildir and return its key."""
    if not isinstance(recipient, str) or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", recipient):
        raise ValueError("invalid synthetic recipient")
    if not isinstance(store_name, str) or not 1 <= len(store_name.strip()) <= 200 or "\n" in store_name:
        raise ValueError("invalid store name")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invalid invitation capability")
    url = urlsplit(landing_url)
    if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1") or url.fragment or url.query:
        raise ValueError("test mailbox requires a local landing URL")
    link = urlunsplit((url.scheme, url.netloc, url.path, "", f"invitation={token}"))
    message = EmailMessage()
    message["From"] = "reviews@example.test"
    message["To"] = recipient
    message["Subject"] = f"Share your experience with {store_name.strip()}"
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
