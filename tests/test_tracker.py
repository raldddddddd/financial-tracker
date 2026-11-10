import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tracker.core import ROOT, connect, dashboard_data, gmail_execute, ingest, is_shopee_confirmation, mime_text, retryable_gmail_error, sanitize_receipt, sync
from tracker.__main__ import page


class FakeCall:
    def execute(self):
        return {"data": base64.urlsafe_b64encode(b"<p>Order DEMO-ORDER-0001</p>").decode()}


class FakeAttachments:
    def get(self, **kwargs):
        return FakeCall()


class FakeMessages:
    def attachments(self):
        return FakeAttachments()


class FakeUsers:
    def messages(self):
        return FakeMessages()


class FakeService:
    def users(self):
        return FakeUsers()


class FakeHttpError(Exception):
    def __init__(self, status, reason):
        self.resp = SimpleNamespace(status=status)
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / "data" / "test.sqlite3")
        self.fixture = json.loads((ROOT / "fixtures" / "two_orders.json").read_text())

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_multi_order_dedup_and_dashboard(self):
        self.assertEqual(ingest(self.db, "email-1", 1000, "Payment confirmed", self.fixture), "processed 2 order(s)")
        self.assertEqual(ingest(self.db, "email-1", 1000, "Payment confirmed", self.fixture), "already processed")
        data = dashboard_data(self.db)
        self.assertEqual(data["total_cents"], 209700)
        self.assertEqual(len(data["orders"]), 2)
        self.assertEqual(data["categories"]["Pet Supplies"], 209700)
        self.assertNotIn("Needs review", page(data))

    def test_refund_cancellation_and_older_email(self):
        ingest(self.db, "email-1", 1000, "Payment confirmed", self.fixture)
        refund = {"combined_payment": "", "orders": [{"order_id": "DEMO-ORDER-0001", "status": "partial_refund", "paid_total": "", "shipping_fee": "", "discount_total": "", "refund_amount": "125.00", "items": []}]}
        ingest(self.db, "email-2", 2000, "Refund", refund)
        cancel = {"combined_payment": "", "orders": [{"order_id": "DEMO-ORDER-0002", "status": "cancelled", "paid_total": "", "shipping_fee": "", "discount_total": "", "refund_amount": "", "items": []}]}
        ingest(self.db, "email-3", 3000, "Cancelled", cancel)
        self.assertEqual(dashboard_data(self.db)["total_cents"], 90000)
        ingest(self.db, "older", 500, "Payment confirmed", self.fixture)
        self.assertEqual(dashboard_data(self.db)["total_cents"], 90000)

    def test_discrepancy_and_invalid_id_are_visible(self):
        fixture = json.loads(json.dumps(self.fixture))
        fixture["combined_payment"] = "2000.00"
        fixture["orders"][0]["discount_total"] = "50.00"
        ingest(self.db, "email-1", 1000, "Payment confirmed", fixture)
        codes = {row["code"] for row in self.db.execute("SELECT code FROM issues")}
        self.assertEqual(codes, {"combined_mismatch", "order_mismatch"})
        with self.assertRaisesRegex(ValueError, "not present"):
            ingest(self.db, "email-2", 2000, "Payment confirmed", self.fixture, "unrelated receipt")

    def test_mime_and_privacy_filter(self):
        message = {"id": "x", "payload": {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Plain order").decode()}},
            {"mimeType": "text/html", "body": {"attachmentId": "part-1"}}
        ]}}
        self.assertIn("DEMO-ORDER-0001", mime_text(FakeService(), message))
        redacted = sanitize_receipt("Recipient: Person Name\n123 Example Street\nOrder DEMO-ORDER-0001\nContact 09171234567\nuser@example.com")
        self.assertNotIn("Person Name", redacted)
        self.assertNotIn("123 Example", redacted)
        self.assertNotIn("09171234567", redacted)
        self.assertNotIn("user@example.com", redacted)
        self.assertIn("DEMO-ORDER-0001", redacted)

    def test_sender_and_subject_filter(self):
        message = {"payload": {"headers": [{"name": "From", "value": "Shopee <notice@orders.shopee.ph>"}, {"name": "Subject", "value": "Payment confirmed"}]}}
        self.assertTrue(is_shopee_confirmation(message, "shopee.ph"))
        message["payload"]["headers"][0]["value"] = "Scammer <notice@notshopee.ph>"
        self.assertFalse(is_shopee_confirmation(message, "shopee.ph"))

    def test_gmail_rate_limit_is_retried(self):
        class Request:
            calls = 0

            def execute(self):
                self.calls += 1
                if self.calls == 1:
                    raise FakeHttpError(403, "rateLimitExceeded")
                return {"ok": True}

        class Pacer:
            units = []

            def wait(self, units):
                self.units.append(units)

        request, pacer = Request(), Pacer()
        with patch("tracker.core.time.sleep"):
            self.assertEqual(gmail_execute(request, 60, pacer), {"ok": True})
        self.assertEqual(pacer.units, [60, 60])
        self.assertFalse(retryable_gmail_error(FakeHttpError(403, "dailyLimitExceeded")))

    def test_sync_saves_progress_before_later_fetch_fails(self):
        receipt = b"Order DEMO-ORDER-0001"
        first = {"id": "first", "internalDate": "1000", "payload": {
            "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(receipt).decode()},
            "headers": [{"name": "From", "value": "Shopee <notice@shopee.ph>"},
                        {"name": "Subject", "value": "Payment confirmed"}]}}

        class Call:
            def __init__(self, result=None, error=None):
                self.result, self.error = result, error

            def execute(self):
                if self.error:
                    raise self.error
                return self.result

        class Messages:
            def list(self, **kwargs):
                return Call({"messages": [{"id": "later"}, {"id": "first"}]})

            def get(self, **kwargs):
                return Call(first) if kwargs["id"] == "first" else Call(error=RuntimeError("fetch failed"))

        class Service:
            def users(self):
                return self

            def messages(self):
                return Messages()

        one_order = {"combined_payment": "", "orders": [self.fixture["orders"][0]]}
        with patch("tracker.core.gmail_service", return_value=Service()), \
             patch("tracker.core.gemini_extract", return_value=one_order), \
             patch("tracker.core.GmailPacer.wait"):
            with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                sync(self.db)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
