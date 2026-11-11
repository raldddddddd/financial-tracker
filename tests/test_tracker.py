import base64
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from tracker.core import ROOT, SHOPEE_QUERY, TIKTOK_QUERY, connect, dashboard_data, gemini_extract, gmail_execute, ingest, is_shopee_confirmation, is_shop_confirmation, mime_text, retryable_gmail_error, sanitize_receipt, shopee_approved_refund, sync
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
        self.assertNotIn("Combined payments", page(data))
        self.assertNotIn("Shopee coins —", page(data))
        self.assertNotIn("Shopee coins ₱0.00", page(data))

    def test_receipt_order_id_repairs_one_missing_extraction(self):
        order = json.loads(json.dumps(self.fixture["orders"][0]))
        order["order_id"] = ""
        self.assertEqual(
            ingest(self.db, "email-1", 1000, "Payment confirmed", {"orders": [order]},
                   "Order ID:\n#DEMO-ORDER-0001"),
            "processed 1 order(s)",
        )
        self.assertEqual(self.db.execute("SELECT order_id FROM orders").fetchone()[0], "DEMO-ORDER-0001")

    def test_ambiguous_missing_ids_report_receipt_ids(self):
        orders = json.loads(json.dumps(self.fixture["orders"]))
        for order in orders:
            order["order_id"] = ""
        with self.assertRaisesRegex(ValueError, "Order 1 of 2 has no valid extracted ID; receipt IDs: DEMO-ORDER-0001, DEMO-ORDER-0002"):
            ingest(self.db, "email-1", 1000, "Payment confirmed", {"orders": orders},
                   "Order ID:\n#DEMO-ORDER-0001\nOrder ID:\n#DEMO-ORDER-0002")
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)

    def test_dashboard_shows_all_failed_emails_with_identifiers(self):
        for index in range(55):
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?)",
                            (f"gmail-{index}", index * 1000, "Your payment has been confirmed", "error", "Missing order ID"))
        self.db.commit()
        dashboard = page(dashboard_data(self.db))
        self.assertEqual(dashboard.count("Run <code>python -m tracker sync</code> to retry"), 55)
        self.assertIn("gmail-0", dashboard)
        self.assertIn("gmail-54", dashboard)

    def test_orders_pagination_keeps_overall_totals(self):
        ingest(self.db, "email-1", 1000, "Payment confirmed", self.fixture)
        data = dashboard_data(self.db)
        template = data["orders"][0]
        data["orders"] = [{**template, "order_id": f"PAGED-{n:02d}"} for n in range(21)]

        first = page(data, 1)
        second = page(data, 2)
        self.assertIn("PAGED-00", first)
        self.assertIn("PAGED-19", first)
        self.assertNotIn("PAGED-20", first)
        self.assertIn('href="/?page=2#orders"', first)
        self.assertIn("PAGED-20", second)
        self.assertNotIn("PAGED-19", second)
        self.assertIn('href="/?page=1#orders"', second)
        self.assertIn("Showing 21–21 of 21", second)
        self.assertIn('<div class="total">₱21,525.00</div>', second)
        self.assertEqual(page(data, 999), second)
        self.assertIn('href="/?shop=shopee&amp;page=2#orders"', page(data, 1, "shopee"))

    def test_refund_cancellation_and_older_email(self):
        ingest(self.db, "email-1", 1000, "Payment confirmed", self.fixture)
        refund = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "partial_refund", "paid_total": "", "shipping_fee": "", "protection_subtotal": "", "discount_total": "", "coins_used": "", "refund_amount": "125.00", "items": []}]}
        ingest(self.db, "email-2", 2000, "Refund", refund)
        cancel = {"orders": [{"order_id": "DEMO-ORDER-0002", "status": "cancelled", "paid_total": "", "shipping_fee": "", "protection_subtotal": "", "discount_total": "", "coins_used": "", "refund_amount": "", "items": []}]}
        ingest(self.db, "email-3", 3000, "Cancelled", cancel)
        self.assertEqual(dashboard_data(self.db)["total_cents"], 197200)
        ingest(self.db, "older", 500, "Payment confirmed", self.fixture)
        self.assertEqual(dashboard_data(self.db)["total_cents"], 197200)
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='unknown_refund'").fetchone()[0], 1)

    def test_shopee_refund_waits_for_review_result(self):
        paid = json.loads(json.dumps(self.fixture["orders"][0]))
        ingest(self.db, "paid", 1000, "Payment confirmed", {"orders": [paid]})
        pending_subject = "Return/Refund under review: #DEMO-ORDER-0001"
        event = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "partial_refund",
                             "paid_total": "", "refund_amount": "125.00", "items": []}]}
        self.assertEqual(ingest(self.db, "pending", 2000, pending_subject, event),
                         "ignored pending refund review")
        self.assertEqual(self.db.execute("SELECT status,refund_cents FROM orders WHERE order_id='DEMO-ORDER-0001'").fetchone()[:],
                         ("paid", 0))
        result_subject = "Review result for request DEMO-ORDER-0001 - Shopee"
        event["orders"][0]["order_id"] = ""
        self.assertEqual(ingest(self.db, "result", 3000, result_subject, event,
                                "Request DEMO-ORDER-0001: Your refund was approved. Refund amount: ₱125.00"),
                         "processed 1 refund request")
        saved = self.db.execute("SELECT reference_type,reference_id,amount_cents FROM refund_events").fetchone()
        self.assertEqual(tuple(saved), ("request", "DEMO-ORDER-0001", 12500))
        self.assertEqual(self.db.execute("SELECT status,refund_cents FROM orders WHERE order_id='DEMO-ORDER-0001'").fetchone()[:],
                         ("paid", 0))
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='orphan_event'").fetchone()[0], 0)
        dashboard = page(dashboard_data(self.db), shop="shopee")
        self.assertIn("Request DEMO-ORDER-0001", dashboard)
        self.assertIn("₱125.00", dashboard)
        self.assertNotIn("Approved refund needs order link", dashboard)
        self.assertEqual(dashboard_data(self.db)["total_cents"], 90000)

    def test_shopee_review_result_without_amount_needs_review(self):
        event = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "refunded",
                             "paid_total": "", "refund_amount": "", "items": []}]}
        with self.assertRaisesRegex(ValueError, "no explicit refund amount"):
            ingest(self.db, "result", 3000,
                   "Review result for request DEMO-ORDER-0001 - Shopee", event,
                   "Order DEMO-ORDER-0001 refund was approved")
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)

    def test_shopee_denied_review_is_not_recorded_as_refund(self):
        event = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "partial_refund",
                             "paid_total": "", "refund_amount": "258", "items": []}]}
        with self.assertRaisesRegex(ValueError, "does not explicitly approve"):
            ingest(self.db, "result", 3000,
                   "Review result for request DEMO-ORDER-0001 - Shopee", event,
                   "Request DEMO-ORDER-0001 was denied. The requested amount was ₱258.")
        self.assertEqual(self.db.execute("SELECT count(*) FROM refund_events").fetchone()[0], 0)

    def test_shopee_approved_refund_reads_result_amount(self):
        subject = "Review result for request DEMO-ORDER-0001 - Shopee"
        text = ("Shopee has reviewed your return request #DEMO-ORDER-0001\n"
                "This refund request has been decided in your favor. "
                "The refund worth ₱258 has been approved. Shopee will process your refund within 24 hours.")
        event = shopee_approved_refund(subject, text)
        self.assertEqual(event["orders"][0]["refund_amount"], "258")
        self.assertEqual(event["orders"][0]["status"], "partial_refund")
        self.assertIsNone(shopee_approved_refund(subject, text.replace("has been approved", "was denied")))
        self.assertIsNone(shopee_approved_refund(subject, text.replace("₱258", "₱258 and ₱400")))

    def test_old_review_result_orphan_becomes_unlinked_refund(self):
        with self.db:
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?)",
                            ("result", 3000, "Review result for request DEMO-ORDER-0001", "processed", None))
            self.db.execute("""INSERT INTO orders
                (order_id,ordered_ms,updated_ms,status,refund_cents,source_gmail_id,platform)
                VALUES (?,?,?,?,?,?,?)""", ("DEMO-ORDER-0001", 3000, 3000, "partial_refund", 25800, "result", "Shopee"))
            self.db.execute("INSERT INTO issues VALUES (?,?,?,?)",
                            ("result", "DEMO-ORDER-0001", "orphan_event", "No paid order"))
        self.db.close()
        self.db = connect(Path(self.temp.name) / "data" / "test.sqlite3")
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT amount_cents FROM refund_events").fetchone()[0], 25800)
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues").fetchone()[0], 0)

    def test_legacy_refunds_migrate_once_and_leave_orders_paid(self):
        paid = self.fixture["orders"][0]
        ingest(self.db, "paid", 1000, "Payment confirmed", {"orders": [paid]})
        with self.db:
            self.db.execute("UPDATE orders SET refund_cents=47200,updated_ms=3000,source_gmail_id='old-refund' WHERE order_id=?",
                            (paid["order_id"],))
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", ("old-refund", 3000, "Refund", "processed", None))
            self.db.execute("INSERT INTO refund_requests VALUES (?,?,?,?,?)", ("REQUEST-1", 4000, 25800, "request-mail", None))
        self.db.close()
        path = Path(self.temp.name) / "data" / "test.sqlite3"
        self.db = connect(path)
        self.assertEqual(self.db.execute("SELECT refund_cents FROM orders").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*),sum(amount_cents) FROM refund_events").fetchone()[:], (2, 73000))
        self.assertEqual(dashboard_data(self.db)["total_cents"], 102500 - 73000)
        self.db.close()
        self.db = connect(path)
        self.assertEqual(self.db.execute("SELECT count(*),sum(amount_cents) FROM refund_events").fetchone()[:], (2, 73000))

    def test_refund_before_paid_order_is_counted_once(self):
        event = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "partial_refund",
                             "paid_total": "", "refund_amount": "25.00", "items": []}]}
        ingest(self.db, "refund", 2000, "Refund", event)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(dashboard_data(self.db)["total_cents"], -2500)
        self.assertEqual(ingest(self.db, "refund", 2000, "Refund", event), "already processed")
        ingest(self.db, "paid", 1000, "Payment confirmed", {"orders": [self.fixture["orders"][0]]})
        self.assertEqual(dashboard_data(self.db)["total_cents"], 100000)
        self.assertEqual(self.db.execute("SELECT count(*) FROM refund_events").fetchone()[0], 1)

    def test_distinct_refunds_for_one_order_add_without_changing_categories(self):
        ingest(self.db, "paid", 1000, "Payment confirmed", {"orders": [self.fixture["orders"][0]]})
        for gmail_id, amount, timestamp in (("refund-one", "25.00", 2000), ("refund-two", "10.00", 3000)):
            event = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "partial_refund",
                                 "paid_total": "", "refund_amount": amount, "items": []}]}
            ingest(self.db, gmail_id, timestamp, "Refund", event)
        data = dashboard_data(self.db)
        self.assertEqual(data["paid_total_cents"], 102500)
        self.assertEqual(data["refund_total_cents"], 3500)
        self.assertEqual(data["total_cents"], 99000)
        self.assertEqual(data["categories"], {"Pet Supplies": 102500})
        self.assertEqual(self.db.execute("SELECT count(*) FROM refund_events").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT refund_cents FROM orders").fetchone()[0], 0)

    def test_category_drilldown_shows_only_matching_items_and_allocated_paid(self):
        paid = {"orders": [{"order_id": "MIXED-ORDER-0001", "status": "paid", "paid_total": "100.01",
                            "shipping_fee": "", "discount_total": "199.99", "refund_amount": "", "items": [
                                {"name": "Cat food", "quantity": 1, "unit_price": "200.00", "line_total": "200.00", "category": "Pet Supplies"},
                                {"name": "Rice", "quantity": 1, "unit_price": "100.00", "line_total": "100.00", "category": "Groceries"}]}]}
        ingest(self.db, "paid", 1000, "Payment confirmed", paid)
        result = {"orders": [{"order_id": "REQUEST-1", "status": "partial_refund", "refund_amount": "10.00"}]}
        ingest(self.db, "result", 2000, "Review result for request REQUEST-1", result,
               "Request REQUEST-1 refund was approved for ₱10.00")
        data = dashboard_data(self.db)
        self.assertEqual(data["total_cents"], 9001)
        self.assertEqual(data["categories"], {"Pet Supplies": 6667, "Groceries": 3334})
        html = page(data, category="Pet Supplies")
        self.assertIn('<div class="total">₱90.01</div>', html)
        self.assertIn('Category paid: ₱66.67', html)
        self.assertIn('<td class="number strong">₱66.67</td>', html)
        self.assertIn('Cat food ×1', html)
        self.assertNotIn('Rice ×1', html)
        self.assertIn('Full order paid ₱100.01', html)
        self.assertIn('aria-label="Purchase spending by category"', html)
        self.assertIn('href="/?category=Pet+Supplies#orders"', html)
        self.assertIn('All categories', html)
        self.assertIn('<h2>Refunds</h2><strong>₱10.00</strong>', html)

    def test_old_pending_shopee_orphan_is_removed_on_open(self):
        subject = "Return/Refund under review: #DEMO-ORDER-0001"
        with self.db:
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", ("pending", 1000, subject, "processed", None))
            self.db.execute("""INSERT INTO orders
                (order_id,ordered_ms,updated_ms,status,refund_cents,source_gmail_id,platform)
                VALUES (?,?,?,?,?,?,?)""", ("DEMO-ORDER-0001", 1000, 1000, "refunded", 0, "pending", "Shopee"))
            self.db.execute("INSERT INTO issues VALUES (?,?,?,?)",
                            ("pending", "DEMO-ORDER-0001", "orphan_event", "Pending notice"))
        self.db.close()
        self.db = connect(Path(self.temp.name) / "data" / "test.sqlite3")
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT state FROM messages WHERE gmail_id='pending'").fetchone()[0], "ignored")

    def test_refund_is_separate_from_order_total_and_follows_shop_filter(self):
        shopee = json.loads(json.dumps(self.fixture["orders"][0]))
        shopee["paid_total"] = "500.00"
        shopee["items"][0].update({"unit_price": "500.00", "line_total": "500.00"})
        tiktok = json.loads(json.dumps(self.fixture["orders"][0]))
        tiktok["paid_total"] = "200.00"
        tiktok["items"][0].update({"unit_price": "200.00", "line_total": "200.00", "category": "Electronics"})
        ingest(self.db, "shopee-paid", 1000, "Payment confirmed", {"orders": [shopee]})
        ingest(self.db, "tiktok-paid", 1000, "TikTok Shop order", {"orders": [tiktok]}, platform="TikTok Shop")
        shopee_refund = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "partial_refund", "paid_total": "", "refund_amount": "50.00", "items": []}]}
        tiktok_refund = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "partial_refund", "paid_total": "", "refund_amount": "25.00", "items": []}]}
        ingest(self.db, "shopee-refund", 2000, "Refund", shopee_refund)
        ingest(self.db, "tiktok-refund", 2000, "Refund", tiktok_refund, platform="TikTok Shop")

        shopee_view = page(dashboard_data(self.db), shop="shopee")
        self.assertIn('<th class="number">Paid</th>', shopee_view)
        self.assertNotIn('<th class="number">Refund</th>', shopee_view)
        self.assertNotIn('<th class="number">Net</th>', shopee_view)
        self.assertIn('<td class="number strong">₱500.00</td>', shopee_view)
        self.assertIn('<div class="total">₱450.00</div>', shopee_view)
        self.assertIn('Pet Supplies</span><strong>₱500.00</strong>', shopee_view)
        self.assertIn('<section class="card" id="refunds">', shopee_view)
        self.assertIn('<h2>Refunds</h2><strong>₱50.00</strong>', shopee_view)
        self.assertNotIn('₱25.00', shopee_view)

        tiktok_view = page(dashboard_data(self.db), shop="tiktok")
        self.assertIn('<td class="number strong">₱200.00</td>', tiktok_view)
        self.assertIn('<h2>Refunds</h2><strong>₱25.00</strong>', tiktok_view)
        self.assertNotIn('₱50.00', tiktok_view)
        all_view = page(dashboard_data(self.db))
        self.assertIn('<div class="total">₱625.00</div>', all_view)
        self.assertIn('<h2>Refunds</h2><strong>₱75.00</strong>', all_view)

    def test_tiktok_refund_uses_summary_total(self):
        paid = json.loads(json.dumps(self.fixture["orders"][0]))
        paid["paid_total"] = "472.00"
        paid["items"][0].update({"unit_price": "472.00", "line_total": "472.00"})
        ingest(self.db, "tiktok-paid", 1000, "TikTok Shop order", {"orders": [paid]}, platform="TikTok Shop")
        event = {"orders": [{"order_id": "DEMO-ORDER-0001", "status": "cancelled", "paid_total": "", "refund_amount": "897.00", "items": []}]}
        source = """Your order DEMO-ORDER-0001 was canceled
Refund summary
Product refund
₱897.00
Shipping fee refund
₱0.00
Total (1 item)
₱472.00"""
        ingest(self.db, "tiktok-refund", 2000, "Refund", event, source, platform="TikTok Shop")
        saved = self.db.execute("SELECT amount_cents FROM refund_events WHERE platform='TikTok Shop'").fetchone()
        self.assertEqual(saved[0], 47200)
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='refund_exceeds_paid'").fetchone()[0], 0)
        html = page(dashboard_data(self.db), shop="tiktok")
        self.assertIn('<td class="number strong">₱472.00</td>', html)
        self.assertIn('<div class="total">₱0.00</div>', html)
        self.assertIn('<h2>Refunds</h2><strong>₱472.00</strong>', html)

    def test_tiktok_order_with_same_id_is_separate(self):
        shopee_order = json.loads(json.dumps(self.fixture["orders"][0]))
        shopee_order["paid_total"] = "1000.00"
        shopee_order["coins_used"] = "25.00"
        tiktok_order = json.loads(json.dumps(self.fixture["orders"][0]))
        tiktok_order["paid_total"] = "200.00"
        tiktok_order["items"][0].update({"unit_price": "200.00", "line_total": "200.00", "category": "Electronics"})
        ingest(self.db, "shopee-email", 1000, "Payment confirmed", {"orders": [shopee_order]}, "Order DEMO-ORDER-0001")
        ingest(self.db, "tiktok-email", 2000, "TikTok Shop order", {"orders": [tiktok_order]}, "Order DEMO-ORDER-0001", platform="TikTok Shop")
        data = dashboard_data(self.db)
        self.assertEqual(data["total_cents"], 120000)
        self.assertEqual({order["platform"] for order in data["orders"]}, {"Shopee", "TikTok Shop"})
        self.assertIn('<span class="shop-tag shop-tiktok">TikTok Shop</span><strong>DEMO-ORDER-0001</strong>', page(data))
        self.assertIn('<span class="shop-tag shop-shopee">Shopee</span><strong>DEMO-ORDER-0001</strong>', page(data))
        tiktok_view = page(data, shop="tiktok")
        self.assertIn('<h1>TikTok Shop spending</h1>', tiktok_view)
        self.assertIn('<div class="total">₱200.00</div>', tiktok_view)
        self.assertIn('<span class="category-name">Electronics</span>', tiktok_view)
        self.assertNotIn('<span>Pet Supplies</span>', tiktok_view)
        self.assertNotIn('shop-tag shop-shopee', tiktok_view)
        self.assertNotIn('Shopee coins ₱25.00', tiktok_view)
        shopee_view = page(data, shop="shopee")
        self.assertIn('<div class="total">₱1,000.00</div>', shopee_view)
        self.assertIn('<span class="category-name">Pet Supplies</span>', shopee_view)
        self.assertNotIn('<span>Electronics</span>', shopee_view)
        self.assertNotIn('shop-tag shop-tiktok', shopee_view)
        self.assertIn('Shopee coins ₱25.00', shopee_view)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 2)

    def test_tiktok_package_summary_does_not_double_count_coupons(self):
        order = json.loads(json.dumps(self.fixture["orders"][0]))
        order.update({"paid_total": "309.36", "shipping_fee": "58.00", "discount_total": "35.70"})
        order["items"][0].update({"unit_price": "306.30", "line_total": "306.30"})
        source = """Order DEMO-ORDER-0001
Subtotal
₱342.00
Handling fee
₱3.06
Shipping
₱58.00
Coupons
- ₱17.85
Seller coupons
- ₱17.85
TikTok shipping coupons
- ₱58.00
Total (1 item)
₱309.36"""
        ingest(self.db, "tiktok-email", 2000, "TikTok Shop order", {"orders": [order]}, source, platform="TikTok Shop")
        saved = self.db.execute("SELECT subtotal_cents,shipping_cents,handling_cents,discount_cents,paid_cents,coins_cents FROM orders WHERE order_id='tiktok:DEMO-ORDER-0001'").fetchone()
        self.assertEqual(tuple(saved), (34200, 5800, 306, 9370, 30936, None))
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='order_mismatch'").fetchone()[0], 0)
        html = page(dashboard_data(self.db), shop="tiktok")
        self.assertIn("discounts ₱93.70", html)
        self.assertIn("handling fee ₱3.06", html)
        self.assertNotIn("Shopee coins", html)

    def test_shopee_unlisted_coins_are_marked_as_inferred(self):
        order = json.loads(json.dumps(self.fixture["orders"][0]))
        order.update({"paid_total": "257.00", "shipping_fee": "0.00", "discount_total": "67.00", "coins_used": "0.00"})
        order["items"][0].update({"unit_price": "332.00", "line_total": "332.00"})
        source = """Order ID:
#DEMO-ORDER-0001
Subtotal:
₱332.00
Shopee Voucher Discount:
₱67.00
Shipping Fee:
₱0.00
Total Payment:
₱257.00"""
        ingest(self.db, "shopee-email", 2000, "Payment confirmed", {"orders": [order]}, source)
        saved = self.db.execute("SELECT coins_cents,coins_inferred FROM orders WHERE order_id='DEMO-ORDER-0001'").fetchone()
        self.assertEqual(tuple(saved), (800, 1))
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='order_mismatch'").fetchone()[0], 0)
        self.assertIn("Shopee coins (inferred) ₱8.00", page(dashboard_data(self.db), shop="shopee"))

    def test_shopee_multi_order_receipt_infers_each_gap_separately(self):
        orders = json.loads(json.dumps(self.fixture["orders"]))
        for order, subtotal, discount, paid in zip(orders, (1351, 1494), (293, 387), (1025, 1072)):
            order.update({"paid_total": f"{paid}.00", "shipping_fee": "0.00", "discount_total": f"{discount}.00"})
            order["items"][0].update({"unit_price": f"{subtotal}.00", "line_total": f"{subtotal}.00"})
        source = """Order ID:
#DEMO-ORDER-0001
Subtotal:
₱1,351.00
Shopee Voucher Discount:
₱293.00
Shipping Fee:
₱0.00
Total Payment:
₱1,025.00
Order ID:
#DEMO-ORDER-0002
Subtotal:
₱1,494.00
Shopee Voucher Discount:
₱307.00
Shop Voucher Discount:
₱80.00
Shipping Fee:
₱0.00
Total Payment:
₱1,072.00"""
        ingest(self.db, "combined-email", 2000, "Payment confirmed", {"orders": orders}, source)
        rows = self.db.execute("SELECT coins_cents,coins_inferred FROM orders ORDER BY order_id").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(3300, 1), (3500, 1)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='order_mismatch'").fetchone()[0], 0)

    def test_order_mismatch_and_invalid_id_are_visible(self):
        fixture = json.loads(json.dumps(self.fixture))
        fixture["orders"][0]["discount_total"] = "50.00"
        fixture["orders"][0]["paid_total"] = "950.00"
        ingest(self.db, "email-1", 1000, "Payment confirmed", fixture)
        codes = {row["code"] for row in self.db.execute("SELECT code FROM issues")}
        self.assertEqual(codes, {"order_mismatch"})
        self.assertIn("Shopee coins were not recorded for this order", page(dashboard_data(self.db)))
        with self.assertRaisesRegex(ValueError, "not present"):
            ingest(self.db, "email-2", 2000, "Payment confirmed", self.fixture, "unrelated receipt")

    def test_shopee_coins_reconcile_separately_from_discounts(self):
        fixture = json.loads(json.dumps(self.fixture))
        fixture["orders"][0]["paid_total"] = "1000.00"
        fixture["orders"][0]["coins_used"] = "25.00"
        ingest(self.db, "email-1", 1000, "Payment confirmed", fixture)
        self.assertEqual(self.db.execute("SELECT coins_cents FROM orders WHERE order_id='DEMO-ORDER-0001'").fetchone()[0], 2500)
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='order_mismatch'").fetchone()[0], 0)
        data = dashboard_data(self.db)
        self.assertEqual(data["total_cents"], 207200)
        self.assertIn("Shopee coins ₱25.00", page(data))

    def test_product_protection_is_in_paid_total_once(self):
        fixture = json.loads(json.dumps(self.fixture))
        fixture["orders"][0]["paid_total"] = "1034.00"
        fixture["orders"][0]["protection_subtotal"] = "9.00"
        ingest(self.db, "email-1", 1000, "Payment confirmed", fixture)
        self.assertEqual(self.db.execute("SELECT protection_cents FROM orders WHERE order_id='DEMO-ORDER-0001'").fetchone()[0], 900)
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='order_mismatch'").fetchone()[0], 0)
        data = dashboard_data(self.db)
        self.assertEqual(data["total_cents"], 210600)
        self.assertNotIn("Product Protection", page(data))

    def test_negative_gap_cannot_be_resolved_with_coins(self):
        fixture = json.loads(json.dumps(self.fixture))
        fixture["orders"][0]["paid_total"] = "1034.00"
        ingest(self.db, "email-1", 1000, "Payment confirmed", fixture)
        self.assertIn("Shopee coins cannot explain this difference", page(dashboard_data(self.db)))

    def test_existing_order_database_gets_coins_column(self):
        path = Path(self.temp.name) / "old.sqlite3"
        with sqlite3.connect(path) as old:
            old.execute("""CREATE TABLE orders (
                order_id TEXT PRIMARY KEY, ordered_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                status TEXT NOT NULL, paid_cents INTEGER, shipping_cents INTEGER,
                discount_cents INTEGER, refund_cents INTEGER NOT NULL DEFAULT 0,
                source_gmail_id TEXT NOT NULL)""")
        with connect(path) as upgraded:
            columns = {row[1] for row in upgraded.execute("PRAGMA table_info(orders)")}
            self.assertIn("coins_cents", columns)
            self.assertIn("protection_cents", columns)
            self.assertIn("platform", columns)
            self.assertIn("subtotal_cents", columns)
            self.assertIn("handling_cents", columns)
            self.assertIn("coins_inferred", columns)

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
        message["payload"]["headers"][0]["value"] = "Shopee <notice@orders.shopee.ph>"
        message["payload"]["headers"][1]["value"] = "Your order #241115ANYCH5TR has been delivered"
        self.assertFalse(is_shopee_confirmation(message, "shopee.ph"))
        message["payload"]["headers"][1]["value"] = "Your payment has been confirmed"
        self.assertTrue(is_shopee_confirmation(message, "shopee.ph"))
        message["payload"]["headers"][1]["value"] = "Return/Refund under review: #DEMO-ORDER-0001"
        self.assertFalse(is_shopee_confirmation(message, "shopee.ph"))
        message["payload"]["headers"][1]["value"] = "Review result for request DEMO-ORDER-0001 - Shopee"
        self.assertTrue(is_shopee_confirmation(message, "shopee.ph"))
        self.assertIn('subject:"Review result for request"', SHOPEE_QUERY)
        message["payload"]["headers"][0]["value"] = "TikTok Shop <no-reply@shop.tiktok.com>"
        message["payload"]["headers"][1]["value"] = "Your TikTok Shop order is confirmed"
        self.assertTrue(is_shop_confirmation(message, "no-reply@shop.tiktok.com"))
        message["payload"]["headers"][0]["value"] = "TikTok <no-reply@tiktok.com>"
        self.assertFalse(is_shop_confirmation(message, "no-reply@shop.tiktok.com"))

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

    def test_gemini_temporary_failure_is_retried(self):
        response = {"candidates": [{"content": {"parts": [{"text": '{"orders":[]}'}]}}]}
        error = HTTPError("https://example.invalid", 503, "Unavailable", {}, None)
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
             patch("tracker.core.urlopen", side_effect=[error, io.BytesIO(json.dumps(response).encode())]) as urlopen, \
             patch("tracker.core.time.sleep"):
            self.assertEqual(gemini_extract("synthetic receipt", "Order paid")["orders"], [])
        self.assertEqual(urlopen.call_count, 2)

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

        one_order = {"orders": [self.fixture["orders"][0]]}
        with patch("tracker.core.gmail_service", return_value=Service()), \
             patch("tracker.core.gemini_extract", return_value=one_order), \
             patch("tracker.core.GmailPacer.wait"):
            with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                sync(self.db)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sync_state").fetchone()[0], 0)

    def test_sync_uses_checkpoint_and_retries_failed_message(self):
        receipt = base64.urlsafe_b64encode(b"Order DEMO-ORDER-0001").decode()

        def message(gmail_id):
            return {"id": gmail_id, "internalDate": "1000", "payload": {
                "mimeType": "text/plain", "body": {"data": receipt},
                "headers": [{"name": "From", "value": "Shopee <notice@shopee.ph>"},
                            {"name": "Subject", "value": "Payment confirmed"}]}}

        class Call:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return self.result

        class Service:
            def __init__(self):
                self.searches = []
                self.fetched = []

            def users(self):
                return self

            def messages(self):
                return self

            def list(self, **kwargs):
                self.searches.append(kwargs["q"])
                if "tiktok.com" in kwargs["q"]:
                    ids = []
                else:
                    ids = ["bad", "first"] if len([q for q in self.searches if "shopee.ph" in q]) == 1 else ["new"]
                return Call({"messages": [{"id": gmail_id} for gmail_id in ids]})

            def get(self, **kwargs):
                self.fetched.append(kwargs["id"])
                return Call(message(kwargs["id"]))

        service = Service()
        one_order = {"orders": [self.fixture["orders"][0]]}
        with patch("tracker.core.gmail_service", return_value=service), \
             patch("tracker.core.gemini_extract", side_effect=[one_order, ValueError("temporary parse failure"), one_order, one_order]), \
             patch("tracker.core.GmailPacer.wait"), \
             patch("tracker.core.time.time", side_effect=[100.5, 200.5]):
            self.assertEqual(sync(self.db), (1, 1))
            self.assertEqual(sync(self.db), (2, 0))

        self.assertNotIn("after:", service.searches[0])
        self.assertNotIn("after:", service.searches[1])
        self.assertIn("after:40", service.searches[2])
        self.assertIn("after:40", service.searches[3])
        self.assertEqual(service.fetched, ["first", "bad", "bad", "new"])
        checkpoint = self.db.execute("SELECT last_started_ms, retry_ids FROM sync_state").fetchone()
        self.assertEqual(checkpoint["last_started_ms"], 200500)
        self.assertEqual(json.loads(checkpoint["retry_ids"]), [])

    def test_expanded_shop_search_ignores_old_shopee_checkpoint(self):
        old_config = json.dumps(["owner@example.com", "shopee.ph", SHOPEE_QUERY])
        self.db.execute("INSERT INTO sync_state VALUES (?,?,?)", (old_config, 150000, "[]"))
        self.db.commit()
        body = base64.urlsafe_b64encode(b"TikTok Shop order DEMO-ORDER-0001").decode()

        class Call:
            def __init__(self, result):
                self.result = result

            def execute(self):
                return self.result

        class Service:
            def __init__(self):
                self.searches = []

            def users(self):
                return self

            def messages(self):
                return self

            def list(self, **kwargs):
                self.searches.append(kwargs["q"])
                return Call({"messages": [{"id": "historical-tiktok"}] if "tiktok.com" in kwargs["q"] else []})

            def get(self, **kwargs):
                return Call({"id": kwargs["id"], "internalDate": "1000", "payload": {
                    "mimeType": "text/plain", "body": {"data": body},
                    "headers": [{"name": "From", "value": "TikTok Shop <no-reply@shop.tiktok.com>"},
                                {"name": "Subject", "value": "Your TikTok Shop order is confirmed"}]}})

        service = Service()
        one_order = {"orders": [self.fixture["orders"][0]]}
        env = {"GMAIL_ACCOUNT": "owner@example.com", "SHOPEE_SENDER_DOMAIN": "shopee.ph",
               "SHOPEE_QUERY": SHOPEE_QUERY, "TIKTOK_SENDER_EMAIL": "no-reply@shop.tiktok.com", "TIKTOK_QUERY": TIKTOK_QUERY}
        with patch.dict("os.environ", env), \
             patch("tracker.core.gmail_service", return_value=service), \
             patch("tracker.core.gemini_extract", return_value=one_order) as extract, \
             patch("tracker.core.GmailPacer.wait"), \
             patch("tracker.core.time.time", return_value=200.5):
            self.assertEqual(sync(self.db), (1, 0))
        self.assertEqual(len(service.searches), 2)
        self.assertTrue(all("after:" not in query for query in service.searches))
        self.assertEqual(extract.call_args.kwargs["platform"], "TikTok Shop")
        self.assertEqual(self.db.execute("SELECT count(*) FROM sync_state").fetchone()[0], 1)
        self.assertEqual(dashboard_data(self.db)["orders"][0]["platform"], "TikTok Shop")


if __name__ == "__main__":
    unittest.main()
