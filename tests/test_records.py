import csv
import io
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tracker.__main__ import SyncManager, serve
from tracker.dashboard import page
from tracker.core import ROOT, apply_refund_evidence, ingest, record_error, record_refund_event
from tracker.insights import dashboard_data, select_dashboard
from tracker.records import (add_manual_purchase, add_manual_refund, archive_record,
                             correct_order, correct_refund, dismiss_review, ignore_email,
                             manual_day, resolve_conflict, resolve_refund_duplicate,
                             set_monthly_budget, undo_change)
from tracker.storage import backup_database, bind_account, connect, export_csv
from tracker.sync import recheck_refunds, run_sync


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
        self.assertEqual(select_dashboard(data).total, 8000)
        self.assertEqual(select_dashboard(data).categories, {"Food & Drink": 11000})
        self.assertEqual(len(data["issues"]), 1)
        rendered = page(data, csrf_token="test-token")
        self.assertIn("Net by category", rendered)
        self.assertIn("Food &amp; Drink</span><strong>₱110.00", rendered)
        self.assertIn("Food &amp; Drink</span><strong>₱80.00", rendered)
        self.assertIn('<details class="add-card" id="add"><summary>Add purchase or refund</summary>', rendered)
        self.assertIn("Top products by spend", rendered)
        self.assertIn("Rice</strong>", rendered)
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
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(exports[0].stat().st_mode), 0o600)

    def test_monthly_budget_is_optional_and_does_not_change_purchases(self):
        order_id = add_manual_purchase(self.db, "Online shop", "2026-11-11", "100.00", "Rice", "Groceries")
        set_monthly_budget(self.db, "150.00")
        data = dashboard_data(self.db)
        self.assertEqual(data["monthly_budget_cents"], 15000)
        self.assertIn('value="150.00"', page(data, csrf_token="test-token"))
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            set_monthly_budget(self.db, "0")
        self.assertEqual(dashboard_data(self.db)["monthly_budget_cents"], 15000)
        set_monthly_budget(self.db, "")
        self.assertIsNone(dashboard_data(self.db)["monthly_budget_cents"])
        self.assertEqual(self.db.execute("SELECT paid_cents FROM orders WHERE order_id=?", (order_id,)).fetchone()[0], 10000)

    def test_sync_manager_prevents_overlapping_runs(self):
        with patch("tracker.__main__.subprocess.Popen") as launch:
            manager = SyncManager(self.root / "expenses.sqlite3")
            manager.start()
            launch.return_value.poll.return_value = None
            with self.assertRaisesRegex(ValueError, "already running"):
                manager.start()
            self.assertEqual(launch.call_count, 1)
            launch.return_value.poll.return_value = 0
            manager.start()
            self.assertEqual(launch.call_count, 2)

    def test_sync_manager_starts_immediately_then_waits_for_interval(self):
        manager = SyncManager(self.root / "expenses.sqlite3")
        with patch.object(manager, "start") as start, \
             patch.object(manager.stopped, "wait", side_effect=lambda _seconds: manager.stopped.set()) as wait:
            manager.repeat(30)
        start.assert_called_once_with()
        wait.assert_called_once_with(1800)

    def test_csv_export_quotes_formula_like_email_fields(self):
        add_manual_purchase(self.db, "=HYPERLINK(\"bad\")", "2026-11-11", "10.00", "+unsafe item", "Other")
        export_csv(self.db, self.root / "export")
        with (self.root / "export" / "orders.csv").open(encoding="utf-8-sig", newline="") as source:
            self.assertEqual(next(csv.DictReader(source))["merchant"], "'=HYPERLINK(\"bad\")")
        with (self.root / "export" / "items.csv").open(encoding="utf-8-sig", newline="") as source:
            self.assertEqual(next(csv.DictReader(source))["name"], "'+unsafe item")

    def test_failed_export_removes_partial_files(self):
        class FailingDB:
            calls = 0

            def execute(inner, query):
                inner.calls += 1
                if inner.calls == 2:
                    raise sqlite3.OperationalError("simulated export failure")
                return self.db.execute(query)

        folder = self.root / "partial"
        with self.assertRaisesRegex(sqlite3.OperationalError, "simulated"):
            export_csv(FailingDB(), folder)
        self.assertEqual(list(folder.iterdir()), [])

    def test_custom_database_keeps_parent_permissions_and_secures_file(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        with connect(shared / "custom.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((shared / "custom.sqlite3").stat().st_mode), 0o600)
        self.assertEqual(list(shared.glob("custom.before-schema-*.sqlite3")), [])

    def test_dashboard_reports_database_outage(self):
        class BrokenDB:
            def execute(self, *_args):
                raise sqlite3.OperationalError("disk I/O error")

        handlers = []
        class CapturedServer:
            def __init__(self, _address, handler):
                handlers.append(handler)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def serve_forever(self):
                pass

        with patch("tracker.__main__.HTTPServer", CapturedServer), \
             patch("tracker.__main__.secrets.token_urlsafe", return_value="test-token"):
            serve(BrokenDB(), 0)
        for path in ("/", "/health", "/order-items?order=one"):
            handler = object.__new__(handlers[0])
            handler.path = path
            responses = []
            handler.respond = lambda status, body, content_type: responses.append((status, body, content_type))
            handler.do_GET()
            self.assertEqual(responses[0][0], 503, path)
            self.assertIn(b"Database unavailable", responses[0][1])
        handler = object.__new__(handlers[0])
        handler.path = "/action"
        body = b"op=sync&csrf=test-token"
        handler.headers = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        responses = []
        handler.respond = lambda status, body, content_type: responses.append((status, body, content_type))
        handler.do_POST()
        self.assertEqual(responses[0][0], 503)
        with patch("tracker.__main__.HTTPServer", CapturedServer):
            serve(self.db, 0, self.root / "missing.sqlite3")
        handler = object.__new__(handlers[-1])
        handler.path = "/health"
        responses = []
        handler.respond = lambda status, body, content_type: responses.append((status, body, content_type))
        handler.do_GET()
        self.assertEqual(responses[0][0], 503)
        with patch("tracker.__main__.HTTPServer", CapturedServer):
            serve(self.db, 0, self.root / "expenses.sqlite3")
        handler = object.__new__(handlers[-1])
        handler.path = "/health"
        responses = []
        handler.respond = lambda status, body, content_type: responses.append((status, body, content_type))
        handler.do_GET()
        self.assertEqual(responses[0][0], 200)

    def test_manual_merchant_search_and_sync_control(self):
        add_manual_purchase(self.db, "Corner store", "2026-11-11", "100.00", "Rice", "Groceries")
        data = dashboard_data(self.db)
        self.assertIn("1 matching", page(data, search="Corner"))
        self.assertIn('name="op" value="sync"', page(data, csrf_token="token", can_sync=True))

    def test_demo_page_keeps_actions_and_amounts_clear(self):
        add_manual_purchase(self.db, "Corner store", "2026-11-11", "100.00", "Rice", "Groceries")
        rendered = page(dashboard_data(self.db), csrf_token="token", demo=True)
        self.assertNotIn("Last sync:", rendered)
        self.assertNotIn("Last completed scan:", rendered)
        self.assertLess(rendered.index('id="add"'), rendered.index('id="shop-filter"'))
        self.assertNotIn("Shipping — · discounts —", rendered)
        self.assertNotIn('aria-label="Orders pages"', rendered)
        self.assertNotIn('class="order-status-mobile"', rendered)
        self.assertNotIn("1 categories", rendered)
        self.assertNotIn("Recorded discounts", rendered)
        self.assertNotIn("By shop</h2>", rendered)
        self.assertIn("Sample data", rendered)
        self.assertIn("Period: All time", rendered)
        self.assertNotIn('<svg class="category-pie"', rendered)
        self.assertIn('class="single-trend"', rendered)
        self.assertNotIn("Net by category", rendered)

    def test_sync_action_starts_one_background_process(self):
        handlers = []
        class CapturedServer:
            def __init__(self, _address, handler):
                handlers.append(handler)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def serve_forever(self):
                pass

        with patch("tracker.__main__.HTTPServer", CapturedServer), \
             patch("tracker.__main__.secrets.token_urlsafe", return_value="test-token"), \
             patch("subprocess.Popen") as popen:
            popen.return_value.poll.return_value = None
            serve(self.db, 0, self.root / "expenses.sqlite3")
            for _ in range(2):
                handler = object.__new__(handlers[0])
                handler.path = "/action"
                body = b"op=sync&csrf=test-token"
                handler.headers = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                handler.send_response = lambda *_args: None
                handler.send_header = lambda *_args: None
                handler.end_headers = lambda: None
                handler.respond = lambda *_args: None
                handler.do_POST()
            self.assertEqual(popen.call_count, 1)

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
        with patch("tracker.sync.sync", return_value=(2, 1)):
            self.assertEqual(run_sync(self.db), (2, 1))
        data = dashboard_data(self.db)
        self.assertEqual(data["sync_run"]["state"], "completed")
        self.assertIn("2 processed, 1 need review", page(data))
        with patch("tracker.sync.sync", side_effect=RuntimeError("Gmail unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Gmail unavailable"):
                run_sync(self.db)
        data = dashboard_data(self.db)
        self.assertEqual(data["sync_run"]["state"], "failed")
        self.assertIn("Gmail unavailable", page(data))

    def test_sync_preserves_original_error_if_failure_log_cannot_be_written(self):
        def fail_after_database_loss(db):
            db.execute("DROP TABLE sync_runs")
            db.commit()
            raise RuntimeError("Gmail unavailable")

        with patch("tracker.sync.sync", side_effect=fail_after_database_loss):
            with self.assertRaisesRegex(RuntimeError, "Gmail unavailable"):
                run_sync(self.db)

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
            self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            migrated.close()
        backups = list(self.root.glob("legacy.before-schema-v0-to-v1-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        with sqlite3.connect(backups[0]) as original:
            self.assertEqual(original.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(original.execute("SELECT paid_cents FROM orders").fetchone()[0], 5000)
            self.assertNotIn("merchant", {row[1] for row in original.execute("PRAGMA table_info(orders)")})
        with connect(legacy_path) as reopened:
            self.assertEqual(reopened.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(list(self.root.glob("legacy.before-schema-v0-to-v1-*.sqlite3")), backups)

    def test_newer_schema_is_rejected_without_changes(self):
        path = self.root / "future.sqlite3"
        with sqlite3.connect(path) as future:
            future.execute("PRAGMA user_version=2")
        with self.assertRaisesRegex(RuntimeError, "newer than this app supports"):
            connect(path)
        with sqlite3.connect(path) as future:
            self.assertEqual(future.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(list(self.root.glob("future.before-schema-*.sqlite3")), [])

    def test_failed_upgrade_keeps_pre_upgrade_backup(self):
        path = self.root / "broken-upgrade.sqlite3"
        with sqlite3.connect(path) as old:
            old.execute("CREATE TABLE original (value TEXT)")
            old.execute("INSERT INTO original VALUES ('kept')")

        def fail(_db):
            raise sqlite3.OperationalError("migration failed")

        with patch("tracker.storage.MIGRATIONS", (fail,)):
            with self.assertRaisesRegex(RuntimeError, "original ledger backup"):
                connect(path)
        backups = list(self.root.glob("broken-upgrade.before-schema-v0-to-v1-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as original:
            self.assertEqual(original.execute("SELECT value FROM original").fetchone()[0], "kept")

    def test_legacy_override_upgrade_uses_schema_version(self):
        path = self.root / "old-overrides.sqlite3"
        old = connect(path)
        with old:
            old.execute("""INSERT INTO orders
                (order_id,ordered_ms,updated_ms,status,paid_cents,source_gmail_id,platform,manual_override)
                VALUES ('LEGACY-ORDER',1000,1000,'paid',5000,'old-mail','Shopee',1)""")
            old.execute("INSERT INTO ledger_meta VALUES ('legacy_overrides_migrated','1')")
            old.execute("PRAGMA user_version=0")
        old.close()
        upgraded = connect(path)
        try:
            value = upgraded.execute("""SELECT value_json FROM order_overrides
                WHERE order_id='LEGACY-ORDER' AND field='paid_cents'""").fetchone()
            self.assertEqual(json.loads(value[0]), 5000)
            self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            upgraded.close()

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
        self.assertEqual(select_dashboard(data).total, 12000)
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
        self.assertEqual(select_dashboard(dashboard_data(self.db)).refund_total, 2000)
        self.assertEqual(len(dashboard_data(self.db)["pending_refunds"]), 1)
        resolve_refund_duplicate(self.db, "second", "distinct")
        self.assertEqual(select_dashboard(dashboard_data(self.db)).refund_total, 4000)
        decision_change = self.db.execute("SELECT id FROM changes ORDER BY id DESC LIMIT 1").fetchone()[0]
        undo_change(self.db, decision_change)
        self.assertEqual(select_dashboard(dashboard_data(self.db)).refund_total, 2000)
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
        self.assertEqual(select_dashboard(dashboard_data(self.db)).paid_total, 12000)
        undo_change(self.db, change)
        self.assertEqual(select_dashboard(dashboard_data(self.db)).paid_total, 10000)
        archive_record(self.db, "order", order_id)
        self.assertEqual(select_dashboard(dashboard_data(self.db)).paid_total, 0)
        archive_record(self.db, "order", order_id, restore=True)
        self.assertEqual(select_dashboard(dashboard_data(self.db)).paid_total, 10000)

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

    def test_linked_refund_uses_purchase_category_after_correction(self):
        order_id = add_manual_purchase(self.db, "Artist", "2026-11-11", "47.20", "Toy keychain", "Pet Supplies")
        refund_id = add_manual_refund(self.db, order_id, "2026-11-12", "47.20")
        data = dashboard_data(self.db)
        self.assertEqual(data["refunds"][0]["allocations"][0]["category"], "Pet Supplies")
        self.assertIn("Category: Pet Supplies", page(data))

        correct_order(self.db, order_id, {"paid": "47.20", "status": "cancelled", "merchant": "Artist",
                                          "category_0": "Hobbies"})
        data = dashboard_data(self.db)
        self.assertEqual(data["refunds"][0]["allocations"][0]["category"], "Hobbies")
        self.assertEqual(select_dashboard(data).net_categories.get("Pet Supplies"), None)
        self.assertEqual(select_dashboard(data).net_categories["Hobbies"], 0)
        self.assertIn("Category: Hobbies", page(data))
        self.assertNotIn("Unallocated refunds", page(data))

    def test_item_refund_uses_corrected_item_category(self):
        order_id = add_manual_purchase(self.db, "Market", "2026-11-11", "150.00", items=[
            {"name": "Rice", "quantity": "1", "amount": "100.00", "category": "Groceries"},
            {"name": "Keychain", "quantity": "1", "amount": "50.00", "category": "Pet Supplies"}])
        add_manual_refund(self.db, order_id, "2026-11-12", "20.00", {1: "20.00"})
        correct_order(self.db, order_id, {"paid": "150.00", "status": "paid", "merchant": "Market",
                                          "category_1": "Hobbies"})
        allocation = dashboard_data(self.db)["refunds"][0]["allocations"][0]
        self.assertEqual((allocation["item_position"], allocation["category"], allocation["amount_cents"]),
                         (1, "Hobbies", 2000))

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
             patch("tracker.sync.gmail_service"), \
             patch("tracker.sync.gmail_execute", return_value=message), \
             patch("tracker.sync.mime_text", return_value="Rice refunded\nCustomer email first@example.com") as mime, \
             patch("tracker.sync.gemini_extract", return_value=response) as gemini:
            self.assertEqual(recheck_refunds(self.db), (1, 0))
        self.assertIn("[email redacted]", gemini.call_args.args[0])
        self.assertNotIn("first@example.com", gemini.call_args.args[0])
        self.assertEqual(select_dashboard(dashboard_data(self.db)).refund_total, 2000)
        self.assertEqual(self.db.execute("SELECT amount_cents FROM refund_allocations").fetchone()[0], 2000)


if __name__ == "__main__":
    unittest.main()
