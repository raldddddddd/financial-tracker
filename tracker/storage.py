"""SQLite setup, legacy upgrades, exports, and backups for one local ledger."""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path

from .core import ROOT, php, secure_dir


def connect(path: Path = ROOT / "data" / "expenses.sqlite3") -> sqlite3.Connection:
    secure_dir(path.parent)
    existed = path.exists()
    if not existed:
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            existed = True
    db = sqlite3.connect(path)
    try:
        path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        latest = len(MIGRATIONS)
        if version > latest:
            raise RuntimeError(f"Database schema version {version} is newer than this app supports ({latest})")
        if version < latest:
            backup = None
            if existed:
                backup = path.with_name(f"{path.stem}.before-schema-v{version}-to-v{latest}-{uuid.uuid4().hex}.sqlite3")
                backup_database(db, backup)
            try:
                for target in range(version + 1, latest + 1):
                    MIGRATIONS[target - 1](db)
                    with db:
                        db.execute(f"PRAGMA user_version={target}")
            except Exception as exc:
                detail = f"; original ledger backup: {backup}" if backup else ""
                raise RuntimeError(f"Database upgrade failed{detail}") from exc
        return db
    except Exception:
        db.close()
        raise


def _migrate_to_v1(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS messages (
      gmail_id TEXT PRIMARY KEY, received_ms INTEGER NOT NULL, subject TEXT NOT NULL,
      state TEXT NOT NULL, error TEXT
    );
    CREATE TABLE IF NOT EXISTS orders (
      order_id TEXT PRIMARY KEY, ordered_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
      status TEXT NOT NULL, paid_cents INTEGER, shipping_cents INTEGER, protection_cents INTEGER,
      discount_cents INTEGER, coins_cents INTEGER, coins_inferred INTEGER NOT NULL DEFAULT 0,
      subtotal_cents INTEGER, handling_cents INTEGER,
      refund_cents INTEGER NOT NULL DEFAULT 0,
      source_gmail_id TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'Shopee',
      merchant TEXT NOT NULL DEFAULT '', manual_override INTEGER NOT NULL DEFAULT 0,
      archived INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS items (
      order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
      position INTEGER NOT NULL, name TEXT NOT NULL, quantity INTEGER NOT NULL,
      unit_cents INTEGER, line_cents INTEGER, category TEXT NOT NULL,
      PRIMARY KEY(order_id, position)
    );
    CREATE TABLE IF NOT EXISTS issues (
      gmail_id TEXT NOT NULL, order_id TEXT NOT NULL DEFAULT '',
      code TEXT NOT NULL, detail TEXT NOT NULL,
      PRIMARY KEY(gmail_id, order_id, code)
    );
    CREATE TABLE IF NOT EXISTS refund_requests (
      request_id TEXT PRIMARY KEY, approved_ms INTEGER NOT NULL,
      refund_cents INTEGER NOT NULL, source_gmail_id TEXT NOT NULL,
      order_id TEXT
    );
    CREATE TABLE IF NOT EXISTS refund_events (
      event_key TEXT PRIMARY KEY, platform TEXT NOT NULL,
      reference_type TEXT NOT NULL, reference_id TEXT NOT NULL,
      amount_cents INTEGER NOT NULL, occurred_ms INTEGER NOT NULL,
      source_gmail_id TEXT NOT NULL, order_id TEXT,
      state TEXT NOT NULL DEFAULT 'counted', duplicate_of TEXT,
      evidence_state TEXT NOT NULL DEFAULT 'unchecked', archived INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sync_state (
      config TEXT PRIMARY KEY, last_started_ms INTEGER NOT NULL,
      retry_ids TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sync_runs (
      id INTEGER PRIMARY KEY, started_ms INTEGER NOT NULL, completed_ms INTEGER,
      state TEXT NOT NULL, processed_count INTEGER NOT NULL DEFAULT 0,
      failed_count INTEGER NOT NULL DEFAULT 0, error TEXT
    );
    CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS order_overrides (
      order_id TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL,
      PRIMARY KEY(order_id,field)
    );
    CREATE TABLE IF NOT EXISTS refund_overrides (
      event_key TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL,
      PRIMARY KEY(event_key,field)
    );
    CREATE TABLE IF NOT EXISTS conflict_values (
      gmail_id TEXT NOT NULL, target_key TEXT NOT NULL, kind TEXT NOT NULL,
      field TEXT NOT NULL, incoming_json TEXT NOT NULL,
      PRIMARY KEY(gmail_id,target_key,kind,field)
    );
    CREATE TABLE IF NOT EXISTS changes (
      id INTEGER PRIMARY KEY, changed_ms INTEGER NOT NULL, kind TEXT NOT NULL,
      target_key TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
      undone_ms INTEGER
    );
    CREATE TABLE IF NOT EXISTS review_checks (
      kind TEXT NOT NULL, review_key TEXT NOT NULL, checked_ms INTEGER NOT NULL,
      PRIMARY KEY(kind,review_key)
    );
    CREATE TABLE IF NOT EXISTS refund_allocations (
      event_key TEXT NOT NULL REFERENCES refund_events(event_key) ON DELETE CASCADE,
      item_position INTEGER NOT NULL, category TEXT NOT NULL,
      amount_cents INTEGER NOT NULL, source TEXT NOT NULL,
      PRIMARY KEY(event_key,item_position)
    );
    """)
    if "coins_cents" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN coins_cents INTEGER")
    if "protection_cents" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN protection_cents INTEGER")
    if "platform" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN platform TEXT NOT NULL DEFAULT 'Shopee'")
    if "subtotal_cents" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN subtotal_cents INTEGER")
    if "handling_cents" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN handling_cents INTEGER")
    if "coins_inferred" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN coins_inferred INTEGER NOT NULL DEFAULT 0")
    if "merchant" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN merchant TEXT NOT NULL DEFAULT ''")
    if "manual_override" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0")
    if "archived" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
        db.execute("ALTER TABLE orders ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "order_id" not in {row[1] for row in db.execute("PRAGMA table_info(refund_events)")}:
        db.execute("ALTER TABLE refund_events ADD COLUMN order_id TEXT")
    refund_columns = {row[1] for row in db.execute("PRAGMA table_info(refund_events)")}
    for column, ddl in (("state", "TEXT NOT NULL DEFAULT 'counted'"),
                        ("duplicate_of", "TEXT"),
                        ("evidence_state", "TEXT NOT NULL DEFAULT 'unchecked'"),
                        ("archived", "INTEGER NOT NULL DEFAULT 0")):
        if column not in refund_columns:
            db.execute(f"ALTER TABLE refund_events ADD COLUMN {column} {ddl}")
    repair_pending_shopee_refunds(db)
    migrate_unlinked_shopee_refunds(db)
    migrate_refunds(db)
    with db:
        db.execute("""UPDATE refund_events SET order_id=CASE
            WHEN platform='TikTok Shop' THEN 'tiktok:' || reference_id ELSE reference_id END
            WHERE order_id IS NULL AND reference_type='order'""")
    migrate_legacy_overrides(db)
    flag_duplicate_refunds(db)


MIGRATIONS = (_migrate_to_v1,)


def bind_account(db: sqlite3.Connection, account: str, *, explicit: bool = False) -> None:
    account = account.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", account):
        raise ValueError("Set a valid GMAIL_ACCOUNT in .env")
    bound = db.execute("SELECT value FROM ledger_meta WHERE key='gmail_account'").fetchone()
    if bound:
        if bound["value"] != account:
            raise ValueError("This database belongs to another Gmail account; use a separate database")
        return
    previous_accounts = set()
    for row in db.execute("SELECT config FROM sync_state"):
        try:
            config = json.loads(row["config"])
            if isinstance(config, list) and config and config[0]:
                previous_accounts.add(str(config[0]).strip().lower())
        except (ValueError, TypeError):
            pass
    if previous_accounts and previous_accounts != {account}:
        raise ValueError("Existing sync history belongs to another or multiple Gmail accounts; use a separate database")
    has_records = any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                      for table in ("orders", "messages", "refund_events"))
    if has_records and not previous_accounts and not explicit:
        raise ValueError("This legacy database has no proven Gmail account; run `python -m tracker bind-account` after checking it")
    with db:
        db.execute("INSERT INTO ledger_meta VALUES ('gmail_account',?)", (account,))


def purge_legacy_demo(db: sqlite3.Connection) -> int:
    """Remove only the old fixture's rows from a live ledger after it has been backed up."""
    with db:
        rows = db.execute("SELECT order_id FROM orders WHERE source_gmail_id='synthetic-demo-message'").fetchall()
        for row in rows:
            db.execute("DELETE FROM orders WHERE order_id=?", (row["order_id"],))
        db.execute("DELETE FROM issues WHERE gmail_id='synthetic-demo-message'")
        db.execute("DELETE FROM messages WHERE gmail_id='synthetic-demo-message'")
    return len(rows)


def migrate_legacy_overrides(db: sqlite3.Connection) -> None:
    with db:
        for order in db.execute("""SELECT * FROM orders WHERE manual_override=1 AND platform!='Other'
            AND NOT EXISTS (SELECT 1 FROM order_overrides WHERE order_overrides.order_id=orders.order_id)""").fetchall():
            for field in ("status", "paid_cents", "shipping_cents", "protection_cents",
                          "discount_cents", "coins_cents", "handling_cents", "merchant"):
                db.execute("INSERT INTO order_overrides VALUES (?,?,?)",
                           (order["order_id"], field, json.dumps(order[field])))
            for item in db.execute("SELECT * FROM items WHERE order_id=?", (order["order_id"],)).fetchall():
                for field in ("name", "quantity", "unit_cents", "line_cents", "category"):
                    db.execute("INSERT INTO order_overrides VALUES (?,?,?)",
                               (order["order_id"], f"item:{item['position']}:{field}", json.dumps(item[field])))


def repair_pending_shopee_refunds(db: sqlite3.Connection) -> None:
    """Discard pending Shopee notices previously mistaken for completed refunds."""
    with db:
        pending = db.execute("""SELECT gmail_id FROM messages
            WHERE state='processed' AND lower(subject) LIKE 'return/refund under review:%'""").fetchall()
        for message in pending:
            gmail_id = message["gmail_id"]
            db.execute("""DELETE FROM orders WHERE source_gmail_id=? AND platform='Shopee'
                AND paid_cents IS NULL AND refund_cents=0
                AND NOT EXISTS (SELECT 1 FROM items WHERE items.order_id=orders.order_id)""", (gmail_id,))
            db.execute("DELETE FROM issues WHERE gmail_id=?", (gmail_id,))
            db.execute("UPDATE messages SET state='ignored', error=NULL WHERE gmail_id=?", (gmail_id,))


def migrate_unlinked_shopee_refunds(db: sqlite3.Connection) -> None:
    """Move old review-result request IDs out of the purchase-order table."""
    with db:
        rows = db.execute("""SELECT o.order_id,o.updated_ms,o.refund_cents,o.source_gmail_id
            FROM orders o JOIN messages m ON m.gmail_id=o.source_gmail_id
            WHERE o.platform='Shopee' AND o.paid_cents IS NULL AND o.refund_cents>0
              AND lower(m.subject) LIKE 'review result for request %'
              AND NOT EXISTS (SELECT 1 FROM items i WHERE i.order_id=o.order_id)""").fetchall()
        for row in rows:
            db.execute("""INSERT OR IGNORE INTO refund_requests
                (request_id,approved_ms,refund_cents,source_gmail_id) VALUES (?,?,?,?)""",
                (row["order_id"], row["updated_ms"], row["refund_cents"], row["source_gmail_id"]))
            db.execute("DELETE FROM issues WHERE order_id=? AND code='orphan_event'", (row["order_id"],))
            db.execute("DELETE FROM orders WHERE order_id=?", (row["order_id"],))


def migrate_refunds(db: sqlite3.Connection) -> None:
    """Move legacy refund amounts into the dated ledger exactly once."""
    with db:
        for row in db.execute("SELECT * FROM refund_requests").fetchall():
            db.execute("""INSERT OR IGNORE INTO refund_events
                (event_key,platform,reference_type,reference_id,amount_cents,occurred_ms,source_gmail_id,order_id)
                VALUES (?,?,?,?,?,?,?,?)""",
                       (f"shopee-request:{row['request_id']}", "Shopee", "request",
                        row["request_id"], row["refund_cents"], row["approved_ms"], row["source_gmail_id"], row["order_id"]))
        for row in db.execute("SELECT * FROM orders WHERE refund_cents>0").fetchall():
            message = db.execute("SELECT received_ms FROM messages WHERE gmail_id=?", (row["source_gmail_id"],)).fetchone()
            db.execute("""INSERT OR IGNORE INTO refund_events
                (event_key,platform,reference_type,reference_id,amount_cents,occurred_ms,source_gmail_id,order_id)
                VALUES (?,?,?,?,?,?,?,?)""",
                       (f"order:{row['source_gmail_id']}:{row['order_id']}", row["platform"], "order",
                        row["order_id"].removeprefix("tiktok:"), row["refund_cents"],
                        message["received_ms"] if message else row["updated_ms"], row["source_gmail_id"], row["order_id"]))
        db.execute("UPDATE orders SET refund_cents=0 WHERE refund_cents<>0")
        db.execute("DELETE FROM refund_requests")


def flag_duplicate_refunds(db: sqlite3.Connection) -> None:
    """Hold legacy same-order, same-amount notices without merging their evidence."""
    with db:
        for event in db.execute("""SELECT * FROM refund_events
            WHERE state='counted' AND archived=0 AND duplicate_of IS NULL AND reference_type!='request'
            ORDER BY occurred_ms,event_key""").fetchall():
            if not event["order_id"]:
                continue
            prior = db.execute("""SELECT event_key FROM refund_events WHERE platform=?
                AND order_id=? AND amount_cents=? AND event_key<>? AND state='counted'
                AND archived=0 AND occurred_ms BETWEEN ? AND ?
                AND (occurred_ms<? OR (occurred_ms=? AND event_key<?))
                ORDER BY occurred_ms,event_key LIMIT 1""",
                (event["platform"], event["order_id"], event["amount_cents"], event["event_key"],
                 event["occurred_ms"] - 604800000, event["occurred_ms"],
                 event["occurred_ms"], event["occurred_ms"], event["event_key"])).fetchone()
            if prior:
                db.execute("UPDATE refund_events SET state='review',duplicate_of=? WHERE event_key=?",
                           (prior["event_key"], event["event_key"]))
                db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)",
                           (event["source_gmail_id"], event["order_id"], "possible_duplicate_refund",
                            f"{php(event['amount_cents'])} may repeat refund {prior['event_key']}; held from totals until reviewed"))


def export_csv(db: sqlite3.Connection, folder: Path) -> list[Path]:
    def safe_cell(value):
        return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value

    exports = (
        ("orders.csv", "SELECT * FROM orders ORDER BY ordered_ms,order_id"),
        ("items.csv", "SELECT * FROM items ORDER BY order_id,position"),
        ("refunds.csv", "SELECT * FROM refund_events ORDER BY occurred_ms,event_key"),
    )
    if any((folder / filename).exists() for filename, _ in exports):
        raise ValueError("Export files already exist in this folder")
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        for filename, query in exports:
            cursor = db.execute(query)
            path = folder / filename
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            paths.append(path)
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as output:
                writer = csv.writer(output)
                writer.writerow([column[0] for column in cursor.description])
                writer.writerows([safe_cell(value) for value in row] for row in cursor)
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    return paths


def backup_database(db: sqlite3.Connection, path: Path) -> Path:
    if path.exists():
        raise ValueError("Backup file already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        destination = sqlite3.connect(path)
        try:
            db.backup(destination)
        finally:
            destination.close()
        path.chmod(0o600)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path
