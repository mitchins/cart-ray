"""Single-recipient, test-only transactional delivery for the EP UAT harness."""

from __future__ import annotations

import asyncio
import json
import urllib.request

TEST_SUBJECT = "Your Effective Policy UAT test resources"
TEST_BODY = "This is a test purchase. These links contain dummy materials only.\n\n"


class TestDelivery:
    """Claim before sending; uncertain provider outcomes are never automatically retried."""

    def __init__(self, connection, stripe, key, sender, recipient, transport=None):
        if (
            not key.startswith("api-")
            or not sender.endswith("@mitchellcurrie.com")
            or recipient != "test-1034@mitchellcurrie.com"
        ):
            raise ValueError("approved test sender and recipient are required")
        self.db, self.stripe, self.key = connection, stripe, key
        self.sender, self.recipient = sender, recipient
        self.transport = transport or self._send
        self.db.execute("""CREATE TABLE IF NOT EXISTS uat_delivery_emails (
            order_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('sending','accepted','uncertain')),
            provider_id TEXT)""")
        self.db.commit()

    def _send(self, body):
        request = urllib.request.Request(
            "https://api.smtp2go.com/v3/email/send",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Smtp2go-Api-Key": self.key},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def _verified_purchase(self, session_id):
        session = asyncio.run(self.stripe.get("/v1/checkout/sessions/" + session_id, {}))
        contact = session.get("customer_details") or {}
        return not (
            session.get("id") != session_id
            or session.get("livemode") is not False
            or session.get("payment_status") != "paid"
            or contact.get("email", "").casefold() != self.recipient.casefold()
        )

    def _send_links(self, links):
        result = self.transport(
            {
                "sender": self.sender,
                "to": [self.recipient],
                "subject": TEST_SUBJECT,
                "text_body": TEST_BODY + "\n".join(links),
            }
        )
        data = result.get("data", {})
        if (
            data.get("succeeded") != 1
            or data.get("failed") != 0
            or not isinstance(data.get("email_id"), str)
            or not data["email_id"]
        ):
            raise ValueError("provider acceptance not confirmed")
        return data["email_id"]

    def send_verified(self, session_id, links):
        """Perform one already-claimed test send and return its provider reference."""
        if not links or not self._verified_purchase(session_id):
            raise ValueError("test purchase or recipient is not verified")
        return self._send_links(links)

    def dispatch(self, order_id, session_id, links):
        if not links:
            return "no_followup"
        if self.db.execute("SELECT 1 FROM uat_delivery_emails WHERE order_id=?", (order_id,)).fetchone():
            return "already_claimed"
        if not self._verified_purchase(session_id):
            return "recipient_or_payment_unverified"
        # Commit the unique claim before the network operation. A crash or timeout
        # leaves a held claim, never an automatic resend of a potentially sent mail.
        with self.db:
            claimed = self.db.execute(
                "INSERT OR IGNORE INTO uat_delivery_emails(order_id,state) VALUES (?, 'sending')", (order_id,)
            ).rowcount
        if not claimed:
            return "already_claimed"
        try:
            provider_id = self._send_links(links)
            with self.db:
                self.db.execute(
                    "UPDATE uat_delivery_emails SET state='accepted',provider_id=? WHERE order_id=?",
                    (provider_id, order_id),
                )
            return "accepted"
        except Exception:
            with self.db:
                self.db.execute("UPDATE uat_delivery_emails SET state='uncertain' WHERE order_id=?", (order_id,))
            return "uncertain"
