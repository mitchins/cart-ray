"""Single-recipient, test-only transactional delivery for the EP UAT harness."""

from __future__ import annotations

import asyncio
import json
import urllib.request


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

    def dispatch(self, order_id, session_id, links):
        if not links:
            return "no_followup"
        if self.db.execute("SELECT 1 FROM uat_delivery_emails WHERE order_id=?", (order_id,)).fetchone():
            return "already_claimed"
        session = asyncio.run(self.stripe.get("/v1/checkout/sessions/" + session_id, {}))
        contact = session.get("customer_details") or {}
        if (
            session.get("id") != session_id
            or session.get("livemode") is not False
            or session.get("payment_status") != "paid"
            or contact.get("email", "").casefold() != self.recipient.casefold()
        ):
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
            result = self.transport(
                {
                    "sender": self.sender,
                    "to": [self.recipient],
                    "subject": "Your Effective Policy UAT test resources",
                    "text_body": "This is a test purchase. These links contain dummy materials only.\n\n"
                    + "\n".join(links),
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
            with self.db:
                self.db.execute(
                    "UPDATE uat_delivery_emails SET state='accepted',provider_id=? WHERE order_id=?",
                    (data["email_id"], order_id),
                )
            return "accepted"
        except Exception:
            with self.db:
                self.db.execute("UPDATE uat_delivery_emails SET state='uncertain' WHERE order_id=?", (order_id,))
            return "uncertain"
