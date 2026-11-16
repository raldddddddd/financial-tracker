from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import MANILA_TZ, ROOT, load_env, seed_demo
from .dashboard import DASHBOARD_JS, page
from .insights import dashboard_data
from .records import (add_manual_purchase, add_manual_refund, archive_record,
                      correct_order, correct_refund, dismiss_review, ignore_email,
                      resolve_conflict, resolve_refund_duplicate, set_monthly_budget,
                      undo_change)
from .storage import backup_database, bind_account, connect, export_csv, purge_legacy_demo
from .sync import gmail_service, recheck_refunds, run_sync


# shortcut: one local server owns sync; use a shared lock before multiple server processes.
class SyncManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.process = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()

    def start(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                raise ValueError("Sync is already running")
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", "tracker", "--database", str(self.db_path), "sync"],
                    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                raise ValueError("Could not start sync") from exc

    def repeat(self, minutes: int) -> None:
        while not self.stopped.is_set():
            try:
                self.start()
            except ValueError as exc:
                if str(exc) != "Sync is already running":
                    print(f"Automatic sync: {exc}", file=sys.stderr)
            self.stopped.wait(minutes * 60)


def serve(db, port: int, db_path: Path | None = None, demo: bool = False, auto_sync_minutes: int = 0):
    db_path = db_path.resolve() if db_path is not None else None
    csrf_token = secrets.token_urlsafe(32)
    sync_manager = SyncManager(db_path) if db_path is not None and not demo else None
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def database_unavailable(self):
            if urlsplit(self.path).path == "/health":
                self.respond(503, b'{"ok": false, "error": "Database unavailable"}', "application/json")
            else:
                self.respond(503, b"Database unavailable. Check the database file and restart the tracker.", "text/plain; charset=utf-8")

        def do_GET(self):
            try:
                self.get()
            except sqlite3.Error:
                self.database_unavailable()

        def get(self):
            url = urlsplit(self.path)
            if url.path not in ("/", "/health", "/dashboard.js", "/order-items"):
                self.send_error(404)
                return
            if url.path == "/dashboard.js":
                body = DASHBOARD_JS.encode()
                self.respond(200, body, "text/javascript; charset=utf-8")
                return
            if db_path is not None and not db_path.is_file():
                raise sqlite3.OperationalError("Database file is missing")
            if url.path == "/health":
                db.execute("SELECT count(*) FROM orders").fetchone()
                self.respond(200, b'{"ok": true}', "application/json")
                return
            if url.path == "/order-items":
                order_id = parse_qs(url.query).get("order", [""])[0]
                items = [dict(row) for row in db.execute("""SELECT position,name,category FROM items
                    WHERE order_id=? AND EXISTS (SELECT 1 FROM orders WHERE order_id=? AND archived=0)
                    ORDER BY position""", (order_id, order_id))]
                self.respond(200, json.dumps(items).encode(), "application/json; charset=utf-8")
                return
            raw_page = parse_qs(url.query).get("page", ["1"])[0]
            try:
                page_number = int(raw_page)
            except ValueError:
                page_number = 1
            query = parse_qs(url.query)
            shop = query.get("shop", ["all"])[0]
            year = query.get("year", [""])[0]
            start = query.get("start", [""])[0]
            end = query.get("end", [""])[0]
            category = query.get("category", [""])[0]
            month = query.get("month", [""])[0]
            trend_group = query.get("group", ["year"])[0]
            search = query.get("q", [""])[0]
            notice = "Sync started. Refresh status to see the result." if query.get("sync") == ["started"] else ""
            body = page(dashboard_data(db), page_number, shop, year, start, end, category, month, trend_group, search,
                        csrf_token, notice, can_sync=db_path is not None and not demo, demo=demo).encode()
            self.respond(200, body, "text/html; charset=utf-8")

        def do_POST(self):
            try:
                self.post()
            except sqlite3.Error:
                self.database_unavailable()

        def post(self):
            if self.path != "/action" or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                self.send_error(404)
                return
            if db_path is not None and not db_path.is_file():
                raise sqlite3.OperationalError("Database file is missing")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= 131072:
                self.send_error(413)
                return
            fields = {key: value[0] for key, value in parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}
            if not secrets.compare_digest(fields.get("csrf", ""), csrf_token):
                self.send_error(403)
                return
            op = fields.get("op")
            try:
                if op == "sync":
                    if sync_manager is None:
                        raise ValueError("Sync is unavailable for this dashboard")
                    sync_manager.start()
                elif op == "purchase":
                    positions = sorted({int(key.removeprefix("item_name_")) for key in fields
                                        if key.startswith("item_name_") and key.removeprefix("item_name_").isdigit()})
                    items = [{"name": fields.get(f"item_name_{position}", ""),
                              "quantity": fields.get(f"item_quantity_{position}", "1"),
                              "amount": fields.get(f"item_amount_{position}", ""),
                              "category": fields.get(f"item_category_{position}", "Other")} for position in positions]
                    add_manual_purchase(db, fields.get("merchant", ""), fields.get("date", ""),
                                        fields.get("amount", ""), items=items)
                elif op == "budget":
                    set_monthly_budget(db, fields.get("amount", ""))
                elif op == "add_refund":
                    item_amounts = {int(key.removeprefix("refund_item_")): value for key, value in fields.items()
                                    if key.startswith("refund_item_") and key.removeprefix("refund_item_").isdigit() and value.strip()}
                    add_manual_refund(db, fields.get("order_id", ""), fields.get("date", ""), fields.get("amount", ""), item_amounts)
                elif op == "order":
                    correct_order(db, fields.get("order_id", ""), fields)
                elif op == "refund":
                    item_amounts = {int(key.removeprefix("refund_item_")): value for key, value in fields.items()
                                    if key.startswith("refund_item_") and key.removeprefix("refund_item_").isdigit() and value.strip()}
                    correct_refund(db, fields.get("event_key", ""), fields.get("amount", ""), fields.get("order_id", ""), fields.get("date", ""), item_amounts)
                elif op == "dismiss":
                    dismiss_review(db, fields.get("gmail_id", ""), fields.get("order_id", ""), fields.get("code", ""))
                elif op == "ignore_email":
                    ignore_email(db, fields.get("gmail_id", ""))
                elif op == "resolve_duplicate":
                    resolve_refund_duplicate(db, fields.get("event_key", ""), fields.get("decision", ""))
                elif op == "resolve_conflict":
                    resolve_conflict(db, fields.get("gmail_id", ""), fields.get("key", ""),
                                     fields.get("kind", ""), fields.get("field", ""))
                elif op in ("archive", "restore"):
                    archive_record(db, fields.get("kind", ""), fields.get("key", ""), restore=op == "restore")
                elif op == "undo":
                    undo_change(db, int(fields.get("change_id", "0")))
                else:
                    raise ValueError("Unknown action")
            except ValueError as exc:
                self.respond(400, page(dashboard_data(db), csrf_token=csrf_token, notice=str(exc), form_values=fields,
                                       can_sync=db_path is not None and not demo, demo=demo).encode(), "text/html; charset=utf-8")
                return
            self.send_response(303)
            fragment = ("budget" if op == "budget" else "overview" if op == "purchase" else
                        "refunds" if op in ("add_refund", "refund") else
                        "review" if op in ("dismiss", "ignore_email", "resolve_duplicate", "resolve_conflict") else
                        "history" if op in ("undo", "restore") else "orders")
            self.send_header("Location", "/?sync=started#overview" if op == "sync" else f"/#{fragment}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Dashboard: http://127.0.0.1:{port}")
        if sync_manager and auto_sync_minutes > 0:
            threading.Thread(target=sync_manager.repeat, args=(auto_sync_minutes,), daemon=True).start()
        try:
            server.serve_forever()
        finally:
            if sync_manager:
                sync_manager.stopped.set()


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Local shop expense tracker")
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "expenses.sqlite3",
                        help="Ledger database path; use a separate file for each Gmail account")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth", help="Authorize the one Gmail account")
    commands.add_parser("sync", help="Fetch and parse Shopee and TikTok Shop confirmations")
    commands.add_parser("bind-account", help="Bind this existing ledger to GMAIL_ACCOUNT after checking ownership")
    recheck = commands.add_parser("recheck-refunds", help="Re-read known refund emails for item evidence")
    recheck.add_argument("--limit", type=int, default=100)
    commands.add_parser("demo", help="Load synthetic sample orders into the separate demo ledger")
    export_command = commands.add_parser("export", help="Export orders, items, and refunds to CSV")
    export_command.add_argument("--output", type=Path, default=ROOT / "data" / f"export-{datetime.now(MANILA_TZ):%Y%m%d-%H%M%S}")
    backup_command = commands.add_parser("backup", help="Create a consistent SQLite backup")
    backup_command.add_argument("--output", type=Path, default=ROOT / "data" / f"backup-{datetime.now(MANILA_TZ):%Y%m%d-%H%M%S}.sqlite3")
    server = commands.add_parser("serve", help="Show the local dashboard")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--demo", action="store_true", help="Serve the separate demo ledger")
    server.add_argument("--sync-minutes", type=int, default=30, help="Sync automatically every N minutes (0 disables; demo never syncs)")
    args = parser.parse_args()
    if args.command == "auth":
        gmail_service()
        print("Gmail authorization ready.")
        return
    if args.database.resolve() == (ROOT / "data" / "demo.sqlite3").resolve() and args.command != "demo" and not (args.command == "serve" and args.demo):
        parser.error("The demo database is reserved for sample data; choose another --database path")
    db = connect(ROOT / "data" / "demo.sqlite3") if args.command == "demo" or (args.command == "serve" and args.demo) else connect(args.database)
    if args.command not in ("demo",) and not (args.command == "serve" and args.demo):
        if db.execute("SELECT 1 FROM orders WHERE source_gmail_id='synthetic-demo-message' LIMIT 1").fetchone():
            backup_database(db, args.database.parent / f"before-demo-separation-{datetime.now(MANILA_TZ):%Y%m%d-%H%M%S}.sqlite3")
            purge_legacy_demo(db)
    if args.command == "sync":
        good, bad = run_sync(db)
        print(f"Processed {good} message(s); {bad} need review.")
    elif args.command == "demo":
        seed_demo(db)
        print("Loaded two synthetic orders. Run: python -m tracker serve --demo")
    elif args.command == "bind-account":
        bind_account(db, os.environ.get("GMAIL_ACCOUNT", ""), explicit=True)
        print("Ledger bound to GMAIL_ACCOUNT.")
    elif args.command == "recheck-refunds":
        checked, ambiguous = recheck_refunds(db, args.limit)
        print(f"Rechecked {checked} refund notice(s); {ambiguous} remain unallocated or need review.")
    elif args.command == "export":
        print("Exported: " + ", ".join(str(path) for path in export_csv(db, args.output)))
    elif args.command == "backup":
        print(f"Backup: {backup_database(db, args.output)}")
    elif args.command == "serve":
        if args.sync_minutes < 0:
            parser.error("--sync-minutes must be zero or greater")
        serve(db, args.port, args.database, args.demo, args.sync_minutes)


if __name__ == "__main__":
    try:
        main()
    except sqlite3.Error as exc:
        raise SystemExit(f"Database unavailable: {exc}") from None
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Tracker unavailable: {exc}") from None
