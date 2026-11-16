"""Manual ledger entries, corrections, and review decisions."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, time as clock_time

from .core import CATEGORIES, MANILA_TZ, money_cents, php, record_refund_event


def manual_day(value: str) -> int:
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Enter a valid date") from exc
    return int(datetime.combine(day, clock_time.min, MANILA_TZ).timestamp() * 1000)


def required_amount(value: str) -> int:
    amount = money_cents(value)
    if amount is None or amount <= 0:
        raise ValueError("Enter an amount greater than zero")
    return amount


def set_monthly_budget(db: sqlite3.Connection, amount: str) -> None:
    with db:
        if amount.strip():
            db.execute("INSERT OR REPLACE INTO ledger_meta (key,value) VALUES ('monthly_budget_cents',?)",
                       (str(required_amount(amount)),))
        else:
            db.execute("DELETE FROM ledger_meta WHERE key='monthly_budget_cents'")


def record_snapshot(db: sqlite3.Connection, kind: str, key: str) -> dict:
    table, column = ("orders", "order_id") if kind == "order" else ("refund_events", "event_key")
    row = db.execute(f"SELECT * FROM {table} WHERE {column}=?", (key,)).fetchone()
    if not row:
        raise ValueError("Record was not found")
    child_table = "items" if kind == "order" else "refund_allocations"
    children = [dict(x) for x in db.execute(f"SELECT * FROM {child_table} WHERE {column}=? ORDER BY 2", (key,))]
    overrides = [dict(x) for x in db.execute("SELECT * FROM order_overrides WHERE order_id=? ORDER BY field", (key,))] if kind == "order" else [dict(x) for x in db.execute("SELECT * FROM refund_overrides WHERE event_key=? ORDER BY field", (key,))]
    issues = ([dict(x) for x in db.execute("SELECT * FROM issues WHERE order_id=? ORDER BY gmail_id,code", (key,))]
              if kind == "order" else [dict(x) for x in db.execute("""SELECT * FROM issues WHERE gmail_id=?
              AND code IN ('manual_refund_conflict','refund_evidence','refund_amount_conflict','possible_duplicate_refund') ORDER BY order_id,code""",
              (row["source_gmail_id"],))])
    conflicts = [dict(x) for x in db.execute("SELECT * FROM conflict_values WHERE target_key=? AND kind=? ORDER BY gmail_id,field", (key, kind))]
    return {"row": dict(row), "children": children, "overrides": overrides, "issues": issues, "conflicts": conflicts}


def log_change(db: sqlite3.Connection, kind: str, key: str, before: dict, action: str) -> None:
    after = record_snapshot(db, kind, key)
    if before != after:
        db.execute("INSERT INTO changes (changed_ms,kind,target_key,before_json,after_json) VALUES (?,?,?,?,?)",
                   (int(time.time() * 1000), action, key, json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True)))


def undo_change(db: sqlite3.Connection, change_id: int) -> None:
    change = db.execute("SELECT * FROM changes WHERE id=? AND undone_ms IS NULL", (change_id,)).fetchone()
    if not change or change["kind"].split(":", 1)[0] not in ("order", "refund"):
        raise ValueError("Change was not found")
    kind = change["kind"].split(":", 1)[0]
    key = change["target_key"]
    later = db.execute("SELECT 1 FROM changes WHERE target_key=? AND id>? AND undone_ms IS NULL", (key, change_id)).fetchone()
    if later or record_snapshot(db, kind, key) != json.loads(change["after_json"]):
        raise ValueError("A later update changed this record; undo that update first")
    before = json.loads(change["before_json"])
    table, column = ("orders", "order_id") if kind == "order" else ("refund_events", "event_key")
    child_table = "items" if kind == "order" else "refund_allocations"
    with db:
        row = before["row"]
        db.execute(f"UPDATE {table} SET " + ",".join(f"{field}=?" for field in row if field != column) + f" WHERE {column}=?",
                   (*[value for field, value in row.items() if field != column], key))
        db.execute(f"DELETE FROM {child_table} WHERE {column}=?", (key,))
        for child in before["children"]:
            db.execute(f"INSERT INTO {child_table} ({','.join(child)}) VALUES ({','.join('?' for _ in child)})", tuple(child.values()))
        if kind == "order":
            db.execute("DELETE FROM order_overrides WHERE order_id=?", (key,))
            for override in before["overrides"]:
                db.execute("INSERT INTO order_overrides VALUES (?,?,?)", tuple(override.values()))
            db.execute("DELETE FROM issues WHERE order_id=?", (key,))
            for issue in before["issues"]:
                db.execute("INSERT INTO issues VALUES (?,?,?,?)", tuple(issue.values()))
        else:
            db.execute("DELETE FROM refund_overrides WHERE event_key=?", (key,))
            for override in before["overrides"]:
                db.execute("INSERT INTO refund_overrides VALUES (?,?,?)", tuple(override.values()))
            db.execute("DELETE FROM issues WHERE gmail_id=? AND code IN ('manual_refund_conflict','refund_evidence','refund_amount_conflict','possible_duplicate_refund')",
                       (row["source_gmail_id"],))
            for issue in before["issues"]:
                db.execute("INSERT INTO issues VALUES (?,?,?,?)", tuple(issue.values()))
        db.execute("DELETE FROM conflict_values WHERE target_key=? AND kind=?", (key, kind))
        for conflict in before["conflicts"]:
            db.execute("INSERT INTO conflict_values VALUES (?,?,?,?,?)", tuple(conflict.values()))
        db.execute("UPDATE changes SET undone_ms=? WHERE id=?", (int(time.time() * 1000), change_id))


def archive_record(db: sqlite3.Connection, kind: str, key: str, *, restore: bool = False) -> None:
    if kind not in ("order", "refund"):
        raise ValueError("Unknown record type")
    before = record_snapshot(db, kind, key)
    row = before["row"]
    if kind == "order" and row["platform"] != "Other":
        raise ValueError("Only manual purchases can be archived")
    if kind == "refund" and row["reference_type"] != "manual":
        raise ValueError("Only manual refunds can be archived")
    with db:
        table, column = ("orders", "order_id") if kind == "order" else ("refund_events", "event_key")
        db.execute(f"UPDATE {table} SET archived=? WHERE {column}=?", (0 if restore else 1, key))
        log_change(db, kind, key, before, f"{kind}:{'restore' if restore else 'archive'}")


def resolve_conflict(db: sqlite3.Connection, gmail_id: str, key: str, kind: str, field: str) -> None:
    if kind not in ("order", "refund"):
        raise ValueError("Unknown conflict type")
    allowed = (field in {"status", "paid_cents", "shipping_cents", "protection_cents",
                         "discount_cents", "coins_cents", "handling_cents", "merchant", "ordered_ms"}
               or bool(re.fullmatch(r"item:\d+:(name|quantity|unit_cents|line_cents|category)", field))) if kind == "order" else field in {"amount_cents", "occurred_ms", "order_id"}
    if not allowed:
        raise ValueError("Unknown conflict field")
    conflict = db.execute("SELECT incoming_json FROM conflict_values WHERE gmail_id=? AND target_key=? AND kind=? AND field=?",
                          (gmail_id, key, kind, field)).fetchone()
    if not conflict:
        raise ValueError("Conflict was not found")
    value = json.loads(conflict["incoming_json"])
    with db:
        before = record_snapshot(db, kind, key)
        if kind == "order" and field.startswith("item:"):
            _, position, item_field = field.split(":", 2)
            db.execute(f"UPDATE items SET {item_field}=? WHERE order_id=? AND position=?", (value, key, int(position)))
        else:
            table, column = ("orders", "order_id") if kind == "order" else ("refund_events", "event_key")
            db.execute(f"UPDATE {table} SET {field}=? WHERE {column}=?", (value, key))
            if kind == "refund" and field in ("amount_cents", "order_id"):
                db.execute("DELETE FROM refund_allocations WHERE event_key=?", (key,))
                db.execute("UPDATE refund_events SET evidence_state='unallocated' WHERE event_key=?", (key,))
        override_table, column = ("order_overrides", "order_id") if kind == "order" else ("refund_overrides", "event_key")
        db.execute(f"DELETE FROM {override_table} WHERE {column}=? AND field=?", (key, field))
        db.execute("DELETE FROM conflict_values WHERE gmail_id=? AND target_key=? AND kind=? AND field=?",
                   (gmail_id, key, kind, field))
        if not db.execute("SELECT 1 FROM conflict_values WHERE gmail_id=? AND target_key=? AND kind=?", (gmail_id, key, kind)).fetchone():
            if kind == "order":
                db.execute("DELETE FROM issues WHERE gmail_id=? AND order_id=? AND code='manual_conflict'", (gmail_id, key))
            elif not db.execute("SELECT 1 FROM conflict_values WHERE gmail_id=? AND kind='refund'", (gmail_id,)).fetchone():
                db.execute("DELETE FROM issues WHERE gmail_id=? AND code='manual_refund_conflict'", (gmail_id,))
        log_change(db, kind, key, before, f"{kind}:accept_email")


def add_manual_purchase(db: sqlite3.Connection, merchant: str, day: str, amount: str,
                        item_name: str = "", category: str = "", items: list[dict] | None = None) -> str:
    merchant = merchant.strip()[:120]
    cents, ordered_ms = required_amount(amount), manual_day(day)
    items = items if items is not None else [{"name": item_name, "category": category, "amount": amount, "quantity": "1"}]
    rows = []
    for item in items:
        name = str(item.get("name", "")).strip()[:300]
        if not name and not str(item.get("amount", "")).strip():
            continue
        try:
            quantity = int(item.get("quantity", "1"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Enter a valid item quantity") from exc
        item_cents = required_amount(str(item.get("amount", "")))
        item_category = item.get("category", "")
        if not name or quantity < 1 or item_category not in CATEGORIES:
            raise ValueError("Enter a name, quantity, amount, and category for each item")
        rows.append((name, quantity, None, item_cents, item_category))
    if not merchant or not rows or sum(row[3] for row in rows) != cents:
        raise ValueError("Enter a merchant and items whose amounts add up to paid")
    order_id = f"manual:{uuid.uuid4().hex}"
    with db:
        db.execute("""INSERT INTO orders
            (order_id,ordered_ms,updated_ms,status,paid_cents,source_gmail_id,platform,merchant,manual_override)
            VALUES (?,?,?,?,?,?,?,?,1)""",
            (order_id, ordered_ms, ordered_ms, "paid", cents, order_id, "Other", merchant))
        db.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?)",
                       [(order_id, position, *row) for position, row in enumerate(rows)])
    return order_id


def set_refund_allocations(db: sqlite3.Connection, event_key: str, order_id: str | None,
                           amounts: dict[int, str], total_cents: int, source: str) -> None:
    allocations = []
    if amounts:
        if not order_id:
            raise ValueError("Link an order before allocating a refund")
        items = {row["position"]: row for row in db.execute("SELECT * FROM items WHERE order_id=?", (order_id,))}
        for position, raw in amounts.items():
            cents = money_cents(raw) or 0
            if cents:
                if position not in items:
                    raise ValueError("Choose an item in the linked order")
                allocations.append((event_key, position, items[position]["category"], cents, source))
        if sum(row[3] for row in allocations) != total_cents:
            raise ValueError("Item refund amounts must add up to the refund total")
    db.execute("DELETE FROM refund_allocations WHERE event_key=?", (event_key,))
    if allocations:
        db.executemany("INSERT INTO refund_allocations VALUES (?,?,?,?,?)", allocations)
    db.execute("UPDATE refund_events SET evidence_state=? WHERE event_key=?",
               ("allocated" if allocations else "unallocated", event_key))
    if allocations:
        event = db.execute("SELECT source_gmail_id,order_id FROM refund_events WHERE event_key=?", (event_key,)).fetchone()
        db.execute("DELETE FROM issues WHERE gmail_id=? AND order_id=? AND code='refund_evidence'",
                   (event["source_gmail_id"], event["order_id"] or ""))


def add_manual_refund(db: sqlite3.Connection, order_id: str, day: str, amount: str,
                      item_amounts: dict[int, str] | None = None) -> str:
    order = db.execute("SELECT platform FROM orders WHERE order_id=? AND archived=0", (order_id,)).fetchone()
    if not order:
        raise ValueError("Choose a tracked order for this refund")
    cents, occurred_ms = required_amount(amount), manual_day(day)
    event_key = f"manual-refund:{uuid.uuid4().hex}"
    with db:
        record_refund_event(db, event_key, order["platform"], "manual",
                            order_id.removeprefix("tiktok:"), cents, occurred_ms, event_key, order_id)
        set_refund_allocations(db, event_key, order_id, item_amounts or {}, cents, "manual")
    return event_key


def correct_order(db: sqlite3.Connection, order_id: str, values: dict[str, str]) -> None:
    order = db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not order:
        raise ValueError("Order was not found")
    status = values.get("status", "")
    if status not in ("paid", "cancelled", "refunded", "partial_refund"):
        raise ValueError("Choose a valid order status")
    paid = required_amount(values.get("paid", ""))
    ordered_ms = manual_day(values["date"]) if values.get("date") else order["ordered_ms"]
    merchant = values.get("merchant", "").strip()[:120]
    if order["platform"] == "Other" and not merchant:
        raise ValueError("Enter a merchant")
    amounts = {}
    for key in ("shipping", "protection", "discount", "coins", "handling"):
        cents = money_cents(values.get(key, ""))
        amounts[key] = cents
    items = db.execute("SELECT * FROM items WHERE order_id=?", (order_id,)).fetchall()
    item_edits = {}
    for item in items:
        position = item["position"]
        category = values.get(f"category_{position}", item["category"])
        name = values.get(f"name_{position}", item["name"]).strip()[:300]
        try:
            quantity = int(values.get(f"quantity_{position}", str(item["quantity"])))
        except ValueError as exc:
            raise ValueError("Enter a valid item quantity") from exc
        if category not in CATEGORIES or not name or quantity < 1:
            raise ValueError("Enter a name, quantity, and category for every item")
        unit = money_cents(values[f"unit_{position}"]) if f"unit_{position}" in values else item["unit_cents"]
        line = money_cents(values[f"line_{position}"]) if f"line_{position}" in values else item["line_cents"]
        item_edits[position] = (name, quantity, unit, line, category)
    with db:
        before = record_snapshot(db, "order", order_id)
        new_fields = {"status": status, "paid_cents": paid, "shipping_cents": amounts["shipping"],
                      "protection_cents": amounts["protection"], "discount_cents": amounts["discount"],
                      "coins_cents": amounts["coins"], "handling_cents": amounts["handling"],
                      "merchant": merchant, "ordered_ms": ordered_ms}
        if order["platform"] != "Other":
            for field, value in new_fields.items():
                if value != order[field]:
                    db.execute("INSERT OR REPLACE INTO order_overrides VALUES (?,?,?)",
                               (order_id, field, json.dumps(value)))
        db.execute("""UPDATE orders SET ordered_ms=?,status=?,paid_cents=?,shipping_cents=?,protection_cents=?,
            discount_cents=?,coins_cents=?,handling_cents=?,coins_inferred=0,merchant=?,manual_override=1
            WHERE order_id=?""",
            (ordered_ms, status, paid, amounts["shipping"], amounts["protection"], amounts["discount"],
             amounts["coins"], amounts["handling"], merchant, order_id))
        for position, item_values in item_edits.items():
            current_item = next(x for x in items if x["position"] == position)
            if order["platform"] != "Other":
                for field, value in zip(("name", "quantity", "unit_cents", "line_cents", "category"), item_values):
                    if value != current_item[field]:
                        db.execute("INSERT OR REPLACE INTO order_overrides VALUES (?,?,?)",
                                   (order_id, f"item:{position}:{field}", json.dumps(value)))
            db.execute("""UPDATE items SET name=?,quantity=?,unit_cents=?,line_cents=?,category=?
                WHERE order_id=? AND position=?""", (*item_values, order_id, position))
        db.execute("DELETE FROM issues WHERE order_id=? AND code IN ('order_mismatch','unknown_refund')", (order_id,))
        source_id = f"manual-review:{order_id}"
        for code in ("order_mismatch", "unknown_refund"):
            db.execute("DELETE FROM review_checks WHERE kind='issue' AND review_key=?",
                       (f"{source_id}:{order_id}:{code}",))
        if status in ("cancelled", "refunded", "partial_refund") and not db.execute(
                "SELECT 1 FROM refund_events WHERE order_id=?", (order_id,)).fetchone():
            db.execute("INSERT INTO issues VALUES (?,?,?,?)",
                       (source_id, order_id, "unknown_refund", "No refund amount is recorded for this order"))
        if status == "paid":
            expected = None
            if order["platform"] == "TikTok Shop" and order["subtotal_cents"] is not None:
                expected = order["subtotal_cents"] + (amounts["shipping"] or 0) + (amounts["handling"] or 0) - (amounts["discount"] or 0)
            elif item_edits and all(item[3] is not None for item in item_edits.values()):
                expected = sum(item[3] for item in item_edits.values()) + (amounts["shipping"] or 0)
                expected += (amounts["protection"] or 0) - (amounts["discount"] or 0) - (amounts["coins"] or 0) + (amounts["handling"] or 0)
            if expected is not None and expected != paid:
                db.execute("INSERT INTO issues VALUES (?,?,?,?)",
                           (source_id, order_id, "order_mismatch",
                            f"Corrected components total {php(expected)} vs paid {php(paid)}"))
        log_change(db, "order", order_id, before, "order:correct")


def correct_refund(db: sqlite3.Connection, event_key: str, amount: str, order_id: str,
                   day: str = "", item_amounts: dict[int, str] | None = None) -> None:
    refund = db.execute("SELECT platform FROM refund_events WHERE event_key=?", (event_key,)).fetchone()
    if not refund:
        raise ValueError("Refund was not found")
    if order_id:
        order = db.execute("SELECT platform FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not order or order["platform"] != refund["platform"]:
            raise ValueError("Choose an order from the same shop")
    cents = required_amount(amount)
    occurred_ms = manual_day(day) if day else None
    with db:
        before = record_snapshot(db, "refund", event_key)
        if before["row"]["reference_type"] != "manual":
            for field, value in (("amount_cents", cents), ("order_id", order_id or None), ("occurred_ms", occurred_ms)):
                if value is not None and value != before["row"][field]:
                    db.execute("INSERT OR REPLACE INTO refund_overrides VALUES (?,?,?)",
                               (event_key, field, json.dumps(value)))
        db.execute("UPDATE refund_events SET amount_cents=?,order_id=?,occurred_ms=coalesce(?,occurred_ms) WHERE event_key=?",
                   (cents, order_id or None, occurred_ms, event_key))
        if item_amounts:
            set_refund_allocations(db, event_key, order_id or None, item_amounts, cents, "manual")
        elif cents != before["row"]["amount_cents"] or order_id != (before["row"]["order_id"] or ""):
            set_refund_allocations(db, event_key, order_id or None, {}, cents, "manual")
        if order_id:
            db.execute("DELETE FROM issues WHERE order_id=? AND code='unknown_refund'", (order_id,))
        log_change(db, "refund", event_key, before, "refund:correct")


def dismiss_review(db: sqlite3.Connection, gmail_id: str, order_id: str = "", code: str = "") -> None:
    with db:
        if code:
            key = f"{gmail_id}:{order_id}:{code}"
            db.execute("INSERT OR REPLACE INTO review_checks VALUES ('issue',?,?)",
                       (key, int(time.time() * 1000)))
        else:
            db.execute("INSERT OR REPLACE INTO review_checks VALUES ('message',?,?)",
                       (gmail_id, int(time.time() * 1000)))


def ignore_email(db: sqlite3.Connection, gmail_id: str) -> None:
    with db:
        result = db.execute("UPDATE messages SET state='ignored',error=NULL WHERE gmail_id=? AND state='error'", (gmail_id,))
        if not result.rowcount:
            raise ValueError("Failed email was not found")
        db.execute("DELETE FROM review_checks WHERE kind='message' AND review_key=?", (gmail_id,))


def resolve_refund_duplicate(db: sqlite3.Connection, event_key: str, decision: str) -> None:
    event = db.execute("SELECT * FROM refund_events WHERE event_key=? AND state='review'", (event_key,)).fetchone()
    if not event or decision not in ("distinct", "ignore"):
        raise ValueError("Choose a pending refund and a valid decision")
    with db:
        before = record_snapshot(db, "refund", event_key)
        db.execute("UPDATE refund_events SET state=?,duplicate_of=? WHERE event_key=?",
                   ("counted" if decision == "distinct" else "ignored",
                    "confirmed-distinct" if decision == "distinct" else None, event_key))
        db.execute("DELETE FROM issues WHERE gmail_id=? AND order_id=? AND code='possible_duplicate_refund'",
                   (event["source_gmail_id"], event["order_id"] or ""))
        log_change(db, "refund", event_key, before, "refund:resolve_duplicate")
