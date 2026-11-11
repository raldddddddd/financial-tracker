import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker.__main__ import page
from tracker.core import (ROOT, add_manual_purchase, add_manual_refund, backup_database,
                          connect, correct_order, correct_refund, dashboard_data, dismiss_review,
                          export_csv, ingest, record_error)
from tracker.core import run_sync
from tracker.core import (apply_refund_evidence, archive_record, bind_account, ignore_email,
                          manual_day, record_refund_event, recheck_refunds, resolve_conflict,
                          resolve_refund_duplicate, undo_change)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = connect(self.root / "expenses.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_manual_purchase_refund_correction_export_and_backup(self):
        order_id = add_manual_purchase(self.db, "Corner store", "2026-11-11", "100.00", "Rice", "Groceries")
        refund_id = add_manual_refund(self.db, order_id, "2026-11-12", "20.00")
        correct_order(self.db, order_id, {"paid": "110.00", "status": "paid", "shipping": "0",
                                          "protection": "0", "discount": "0", "coins": "0",
                                          "merchant": "Corner store", "category_0": "Food & Drink"})
        correct_refund(self.db, refund_id, "30.00", order_id)
        data = dashboard_data(self.db)
        self.assertEqual(data["total_cents"], 8000)
        self.assertEqual(data["categories"], {"Food & Drink": 11000})
        self.assertEqual(len(data["issues"]), 1)
        rendered = page(data, csrf_token="test-token")
        self.assertIn("Net by category", rendered)
        self.assertIn("Food &amp; Drink</span><strong>₱110.00", rendered)
        self.assertIn("Unallocated refunds</span><strong>-₱30.00", rendered)
        self.assertIn("Other shop purchase", page(data, csrf_token="test-token"))
        exports = export_csv(self.db, self.root / "export")
        self.assertEqual(len(exports), 3)
        with exports[2].open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(rows[0]["amount_cents"], "3000")
        self.assertEqual(rows[0]["order_id"], order_id)
        with self.assertRaisesRegex(ValueError, "already exist"):
            export_csv(self.db, self.root / "export")
        backup = backup_database(self.db, self.root / "backup.sqlite3")
        with sqlite3.connect(backup) as restored:
            self.assertEqual(restored.execute("SELECT paid_cents FROM orders").fetchone()[0], 11000)
        with self.assertRaisesRegex(ValueError, "already exists"):
            backup_database(self.db, backup)

    def test_later_refund_clears_old_issue_and_correction_survives_sync(self):
        fixture = json.loads((ROOT / "fixtures" / "two_orders.json").read_text())
        paid = fixture["orders"][0]
        order_id = paid["order_id"]
        ingest(self.db, "paid", 1000, "Payment confirmed", {"orders": [paid]})
        no_amount = {"orders": [{"order_id": order_id, "status": "cancelled", "paid_total": "",
                                 "refund_amount": "", "items": []}]}
        ingest(self.db, "cancel", 2000, "Cancelled", no_amount)
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='unknown_refund'").fetchone()[0], 1)
        with_amount = {"orders": [{"order_id": order_id, "status": "refunded", "paid_total": "",
                                   "refund_amount": "25.00", "items": []}]}
        ingest(self.db, "refund", 3000, "Refund", with_amount)
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='unknown_refund'").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT order_id FROM refund_events").fetchone()[0], order_id)
        correct_order(self.db, order_id, {"paid": "1000.00", "status": "refunded", "shipping": "0",
                                          "protection": "0", "discount": "0", "coins": "0",
                                          "category_0": "Groceries"})
        ingest(self.db, "new-paid", 4000, "Payment confirmed", {"orders": [paid]})
        order = dashboard_data(self.db)["orders"][0]
        self.assertEqual(order["paid_cents"], 100000)
        self.assertEqual(order["items"][0]["category"], "Groceries")
        self.assertEqual(order["status"], "paid")
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='manual_conflict'").fetchone()[0], 1)
        conflict = self.db.execute("SELECT gmail_id,target_key,kind,field FROM conflict_values WHERE field='paid_cents'").fetchone()
        self.assertIsNotNone(conflict)
        resolve_conflict(self.db, *conflict)
        self.assertNotIn("paid_cents", [row[0] for row in self.db.execute("SELECT field FROM order_overrides WHERE order_id=?", (order_id,))])
        self.assertEqual(self.db.execute("SELECT paid_cents FROM orders WHERE order_id=?", (order_id,)).fetchone()[0], 102500)

    def test_refund_link_requires_order_from_same_shop(self):
        order_id = add_manual_purchase(self.db, "Corner store", "2026-11-11", "100.00", "Rice", "Groceries")
        fixture = json.loads((ROOT / "fixtures" / "two_orders.json").read_text())
        ingest(self.db, "paid", 1000, "Payment confirmed", {"orders": [fixture["orders"][0]]})
        refund_id = add_manual_refund(self.db, order_id, "2026-11-12", "20.00")
        with self.assertRaisesRegex(ValueError, "same shop"):
            correct_refund(self.db, refund_id, "30.00", fixture["orders"][0]["order_id"])
        self.assertEqual(self.db.execute("SELECT amount_cents FROM refund_events WHERE event_key=?", (refund_id,)).fetchone()[0], 2000)

    def test_request_refund_links_to_explicit_order_id(self):
        fixture = json.loads((ROOT / "fixtures" / "two_orders.json").read_text())
        order_id = fixture["orders"][0]["order_id"]
        ingest(self.db, "paid", 1000, "Payment confirmed", {"orders": [fixture["orders"][0]]})
        result = {"orders": [{"order_id": "REQUEST-1", "status": "partial_refund", "refund_amount": "10.00"}]}
        ingest(self.db, "result", 2000, "Review result for request REQUEST-1", result,
               f"Request REQUEST-1 approved\nOrder ID: {order_id}")
        self.assertEqual(self.db.execute("SELECT order_id FROM refund_events").fetchone()[0], order_id)

    def test_sync_result_and_failure_are_visible(self):
        with patch("tracker.core.sync", return_value=(2, 1)):
            self.assertEqual(run_sync(self.db), (2, 1))
        data = dashboard_data(self.db)
        self.assertEqual(data["sync_run"]["state"], "completed")
        self.assertIn("2 processed, 1 need review", page(data))
        with patch("tracker.core.sync", side_effect=RuntimeError("Gmail unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Gmail unavailable"):
                run_sync(self.db)
        data = dashboard_data(self.db)
        self.assertEqual(data["sync_run"]["state"], "failed")
        self.assertIn("Gmail unavailable", page(data))

    def test_existing_database_gains_new_columns_without_losing_records(self):
        legacy_path = self.root / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as legacy:
            legacy.executescript("""
                CREATE TABLE orders (
                    order_id TEXT PRIMARY KEY, ordered_ms INTEGER, updated_ms INTEGER,
                    status TEXT, paid_cents INTEGER, shipping_cents INTEGER,
                    protection_cents INTEGER, discount_cents INTEGER, coins_cents INTEGER,
                    coins_inferred INTEGER, subtotal_cents INTEGER, handling_cents INTEGER,
                    refund_cents INTEGER, source_gmail_id TEXT, platform TEXT);
                CREATE TABLE refund_events (
                    event_key TEXT PRIMARY KEY, platform TEXT, reference_type TEXT,
                    reference_id TEXT, amount_cents INTEGER, occurred_ms INTEGER,
                    source_gmail_id TEXT);
                INSERT INTO orders VALUES
                    ('OLD-ORDER', 1000, 1000, 'paid', 5000, 0, 0, 0, 0, 0, NULL, NULL, 0, 'old-mail', 'Shopee');
                INSERT INTO refund_events VALUES
                    ('old-refund', 'Shopee', 'order', 'OLD-ORDER', 1000, 2000, 'refund-mail');
            """)
        migrated = connect(legacy_path)
        try:
            self.assertEqual(migrated.execute("SELECT paid_cents,merchant,manual_override FROM orders").fetchone()[:],
                             (5000, "", 0))
            self.assertEqual(migrated.execute("SELECT order_id FROM refund_events").fetchone()[0], "OLD-ORDER")
            self.assertEqual(migrated.execute("SELECT count(*) FROM sync_runs").fetchone()[0], 0)
        finally:
            migrated.close()

    def test_review_items_can_be_marked_checked(self):
        order_id = add_manual_purchase(self.db, "Corner store", "2026-11-11", "100.00", "Rice", "Groceries")
        correct_order(self.db, order_id, {"paid": "110.00", "status": "paid", "shipping": "0",
                                          "protection": "0", "discount": "0", "coins": "0",
                                          "merchant": "Corner store", "category_0": "Groceries"})
        issue = self.db.execute("SELECT gmail_id,order_id,code FROM issues").fetchone()
        dismiss_review(self.db, *issue)
        self.assertEqual(len(dashboard_data(self.db)["issues"]), 0)
        record_error(self.db, "broken-mail", 1000, "Payment", ValueError("Missing order ID"))
        dismiss_review(self.db, "broken-mail")
        self.assertEqual(self.db.execute("SELECT state FROM messages WHERE gmail_id='broken-mail'").fetchone()[0], "error")
        self.assertEqual(len(dashboard_data(self.db)["messages"]), 0)
        record_error(self.db, "broken-mail", 1000, "Payment", ValueError("Still missing order ID"))
        self.assertEqual(len(dashboard_data(self.db)["messages"]), 1)
        ignore_email(self.db, "broken-mail")
        self.assertEqual(self.db.execute("SELECT state FROM messages WHERE gmail_id='broken-mail'").fetchone()[0], "ignored")

    def test_mixed_basket_and_item_refund_allocation(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "150.00", items=[
            {"name": "Rice", "quantity": "1", "amount": "100.00", "category": "Groceries"},
            {"name": "Soap", "quantity": "2", "amount": "50.00", "category": "Home"},
        ])
        with self.assertRaisesRegex(ValueError, "add up"):
            add_manual_purchase(self.db, "Market", "2026-11-11", "150.00", items=[
                {"name": "Rice", "quantity": "1", "amount": "99.00", "category": "Groceries"}])
        refund_id = add_manual_refund(self.db, order_id, "2026-11-12", "30.00", {1: "30.00"})
        data = dashboard_data(self.db)
        self.assertEqual(data["total_cents"], 12000)
        self.assertEqual(data["refunds"][0]["allocations"][0]["category"], "Home")
        self.assertIn("Home</span><strong>₱20.00", page(data))
        with self.assertRaisesRegex(ValueError, "add up"):
            add_manual_refund(self.db, order_id, "2026-11-12", "30.00", {0: "10.00"})
        self.assertEqual(self.db.execute("SELECT count(*) FROM refund_events").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT event_key FROM refund_allocations").fetchone()[0], refund_id)

    def test_duplicate_refund_held_until_resolved_and_decision_persists(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "100.00", "Rice", "Groceries")
        record_refund_event(self.db, "first", "Other", "order", order_id, 2000, 1000, "mail-a", order_id)
        record_refund_event(self.db, "second", "Other", "order", order_id, 2000, 2000, "mail-b", order_id)
        self.assertEqual(dashboard_data(self.db)["refund_total_cents"], 2000)
        self.assertEqual(len(dashboard_data(self.db)["pending_refunds"]), 1)
        resolve_refund_duplicate(self.db, "second", "distinct")
        self.assertEqual(dashboard_data(self.db)["refund_total_cents"], 4000)
        decision_change = self.db.execute("SELECT id FROM changes ORDER BY id DESC LIMIT 1").fetchone()[0]
        undo_change(self.db, decision_change)
        self.assertEqual(dashboard_data(self.db)["refund_total_cents"], 2000)
        resolve_refund_duplicate(self.db, "second", "distinct")
        self.db.close()
        self.db = connect(self.root / "expenses.sqlite3")
        self.assertEqual(len(dashboard_data(self.db)["pending_refunds"]), 0)

    def test_correction_undo_and_archive_restore(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "100.00", "Rice", "Groceries")
        correct_order(self.db, order_id, {"paid": "120.00", "status": "paid", "date": "2026-11-10",
                                          "shipping": "0", "protection": "0", "discount": "0", "coins": "0",
                                          "merchant": "Market", "category_0": "Groceries"})
        change = self.db.execute("SELECT id FROM changes ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(dashboard_data(self.db)["paid_total_cents"], 12000)
        undo_change(self.db, change)
        self.assertEqual(dashboard_data(self.db)["paid_total_cents"], 10000)
        archive_record(self.db, "order", order_id)
        self.assertEqual(dashboard_data(self.db)["paid_total_cents"], 0)
        archive_record(self.db, "order", order_id, restore=True)
        self.assertEqual(dashboard_data(self.db)["paid_total_cents"], 10000)

    def test_account_binding_rejects_other_account_and_ambiguous_legacy(self):
        add_manual_purchase(self.db, "Market", "2026-11-11", "100.00", "Rice", "Groceries")
        with self.assertRaisesRegex(ValueError, "no proven Gmail account"):
            bind_account(self.db, "first@example.com")
        bind_account(self.db, "first@example.com", explicit=True)
        with self.assertRaisesRegex(ValueError, "another Gmail account"):
            bind_account(self.db, "second@example.com")

    def test_mixed_category_combined_refund_stays_unallocated(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "150.00", items=[
            {"name": "Rice", "quantity": "1", "amount": "100.00", "category": "Groceries"},
            {"name": "Soap", "quantity": "1", "amount": "50.00", "category": "Home"}])
        record_refund_event(self.db, "refund", "Other", "order", order_id, 3000, 1000, "mail", order_id)
        self.assertFalse(apply_refund_evidence(self.db, "refund", [
            {"name": "Rice", "amount": ""}, {"name": "Soap", "amount": ""}]))
        self.assertEqual(self.db.execute("SELECT count(*) FROM refund_allocations").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT evidence_state FROM refund_events").fetchone()[0], "ambiguous")

    def test_same_category_combined_refund_allocates_category_only(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "100.00", items=[
            {"name": "Rice", "quantity": "1", "amount": "60.00", "category": "Groceries"},
            {"name": "Eggs", "quantity": "1", "amount": "40.00", "category": "Groceries"}])
        record_refund_event(self.db, "refund", "Other", "order", order_id, 3000, 1000, "mail", order_id)
        self.assertTrue(apply_refund_evidence(self.db, "refund", [
            {"name": "Rice", "amount": ""}, {"name": "Eggs", "amount": ""}]))
        allocation = self.db.execute("SELECT item_position,category,amount_cents FROM refund_allocations").fetchone()
        self.assertEqual(tuple(allocation), (-1, "Groceries", 3000))
        correct_refund(self.db, "refund", "30.00", order_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM refund_allocations").fetchone()[0], 1)

    def test_imported_refund_edit_survives_repeat_notice(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "100.00", "Rice", "Groceries")
        record_refund_event(self.db, "email-refund", "Other", "order", order_id, 2000, 1000, "mail", order_id)
        correct_refund(self.db, "email-refund", "25.00", order_id, "2026-11-12")
        record_refund_event(self.db, "email-refund", "Other", "order", order_id, 2000, 2000, "mail", order_id)
        event = self.db.execute("SELECT amount_cents,occurred_ms FROM refund_events WHERE event_key='email-refund'").fetchone()
        self.assertEqual(event["amount_cents"], 2500)
        self.assertEqual(event["occurred_ms"], manual_day("2026-11-12"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM issues WHERE code='manual_refund_conflict'").fetchone()[0], 1)

    def test_manual_refund_allocation_survives_later_email_evidence(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "100.00", "Rice", "Groceries")
        record_refund_event(self.db, "email-refund", "Other", "order", order_id, 2000, 1000, "mail", order_id)
        correct_refund(self.db, "email-refund", "20.00", order_id, item_amounts={0: "20.00"})
        self.assertTrue(apply_refund_evidence(self.db, "email-refund", [{"name": "Rice", "amount": "20.00"}]))
        row = self.db.execute("SELECT category,amount_cents,source FROM refund_allocations WHERE event_key='email-refund'").fetchone()
        self.assertEqual(tuple(row), ("Groceries", 2000, "manual"))

    def test_historical_recheck_uses_sanitized_mail_and_keeps_total(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "100.00", "Rice", "Groceries")
        record_refund_event(self.db, "email-refund", "Other", "order", order_id, 2000, 1000, "mail", order_id)
        bind_account(self.db, "first@example.com", explicit=True)
        response = {"orders": [{"order_id": order_id, "refund_items": [{"name": "Rice", "amount": "20.00"}]}]}
        message = {"payload": {"headers": [{"name": "Subject", "value": "Refund"}]}}
        with patch.dict("os.environ", {"GMAIL_ACCOUNT": "first@example.com"}), \
             patch("tracker.core.gmail_service"), \
             patch("tracker.core.gmail_execute", return_value=message), \
             patch("tracker.core.mime_text", return_value="Rice refunded\nCustomer email first@example.com") as mime, \
             patch("tracker.core.gemini_extract", return_value=response) as gemini:
            self.assertEqual(recheck_refunds(self.db), (1, 0))
        self.assertIn("[email redacted]", gemini.call_args.args[0])
        self.assertNotIn("first@example.com", gemini.call_args.args[0])
        self.assertEqual(dashboard_data(self.db)["refund_total_cents"], 2000)
        self.assertEqual(self.db.execute("SELECT amount_cents FROM refund_allocations").fetchone()[0], 2000)


if __name__ == "__main__":
    unittest.main()
