from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.header import decode_header, make_header
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANILA_TZ = timezone(timedelta(hours=8), "Asia/Manila")
CATEGORIES = ["Pet Supplies", "Groceries", "Food & Drink", "Home", "Beauty & Health", "Fashion", "Electronics", "Transport", "Hobbies", "Other"]


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def secure_dir(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed or path in (ROOT / "data", ROOT / ".secrets"):
        path.chmod(0o700)


def money_cents(value, *, allow_blank: bool = True) -> int | None:
    if value is None or str(value).strip() == "":
        return None if allow_blank else 0
    cleaned = re.sub(r"[₱,\s]", "", str(value))
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Invalid money amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("Money amounts must be finite and nonnegative")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def tiktok_package_summary(text: str) -> dict[str, int] | None:
    """Read the explicit financial summary, whose item prices may already include coupons."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    starts = [index for index, line in enumerate(lines) if line.lower() == "subtotal"]
    if len(starts) != 1:
        return None
    start = starts[0]
    end = next((index for index in range(start + 1, min(start + 40, len(lines)))
                if re.fullmatch(r"total\s*\(\d+\s+items?\)", lines[index], re.IGNORECASE)), None)
    if end is None or end + 1 >= len(lines):
        return None

    def signed_amount(line: str) -> int | None:
        match = re.fullmatch(r"(-)?\s*₱\s*([\d,]+(?:\.\d{2})?)", line)
        if not match:
            return None
        cents = money_cents(match.group(2))
        return -cents if match.group(1) else cents

    subtotal = signed_amount(lines[start + 1])
    total = signed_amount(lines[end + 1])
    if subtotal is None or total is None:
        return None
    shipping = fees = discounts = 0
    for index in range(start + 2, end - 1):
        amount = signed_amount(lines[index + 1])
        if amount is None:
            continue
        if amount < 0:
            discounts -= amount
        elif lines[index].lower() == "shipping":
            shipping += amount
        else:
            fees += amount
    if subtotal + shipping + fees - discounts != total:
        return None
    return {"subtotal": subtotal, "shipping": shipping, "fees": fees,
            "discounts": discounts, "total": total}


def tiktok_refund_total(text: str) -> int | None:
    """Use the refund summary total, which can differ from the gross product refund."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    starts = [index for index, line in enumerate(lines) if line.lower() == "refund summary"]
    if len(starts) != 1:
        return None
    start = starts[0]
    for index in range(start + 1, min(start + 40, len(lines) - 1)):
        if re.fullmatch(r"total\s*\(\d+\s+items?\)", lines[index], re.IGNORECASE):
            match = re.fullmatch(r"₱\s*([\d,]+(?:\.\d{2})?)", lines[index + 1])
            return money_cents(match.group(1)) if match else None
    return None


def shopee_order_summary(text: str, order_id: str) -> dict[str, int | None] | None:
    """Read one order's stated amounts from a Shopee payment email."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for start, line in enumerate(lines):
        if not line.lower().startswith("order id:"):
            continue
        candidate = line.split(":", 1)[1].strip() or (lines[start + 1] if start + 1 < len(lines) else "")
        if candidate.lstrip("#").strip() != order_id:
            continue
        end = next((index for index in range(start + 1, len(lines)) if lines[index].lower().startswith("order id:")), len(lines))
        values: dict[str, int | None] = {"subtotal": None, "shipping": None, "protection": 0,
                                         "discounts": 0, "total": None, "coins": None}
        block = lines[start:end]
        for index, entry in enumerate(block):
            match = re.fullmatch(r"([^:]+):(?:\s*₱\s*([\d,]+(?:\.\d{2})?))?", entry)
            if not match:
                continue
            raw_amount = match.group(2)
            if raw_amount is None and index + 1 < len(block):
                following = re.fullmatch(r"₱\s*([\d,]+(?:\.\d{2})?)", block[index + 1])
                raw_amount = following.group(1) if following else None
            if raw_amount is None:
                continue
            label, amount = match.group(1).lower(), money_cents(raw_amount)
            if label == "subtotal":
                values["subtotal"] = amount
            elif label == "shipping fee":
                values["shipping"] = amount
            elif label == "total payment":
                values["total"] = amount
            elif label == "product protection subtotal":
                values["protection"] = amount
            elif "coin" in label:
                values["coins"] = amount
            elif "discount" in label:
                values["discounts"] += amount
        return values if all(values[key] is not None for key in ("subtotal", "shipping", "total")) else None
    return None


def php(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"-₱{abs(cents) / 100:,.2f}" if cents < 0 else f"₱{cents / 100:,.2f}"


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "td"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "div", "tr", "li", "td"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = TextHTML()
    parser.feed(value)
    return "".join(parser.parts)


def sanitize_receipt(text: str) -> str:
    """Remove likely recipient data before the external model call; never store raw mail."""
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email redacted]", text)
    text = re.sub(r"(?<!\w)(?:\+?63|0)9\d{9}(?!\w)", "[phone redacted]", text)
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    result = []
    skipping = False
    for line in lines:
        if not line:
            skipping = False
            continue
        low = line.lower()
        if re.search(r"\b(recipient|receiver|deliver(?:y| to)|shipping address|ship to|billing address|customer address|contact number|phone number)\b", low):
            skipping = True
            continue
        if skipping:
            if re.search(r"\b(order|item|product|quantity|qty|seller|subtotal|discount|voucher|shipping fee|total|payment|refund|cancel)\b", low):
                skipping = False
            else:
                continue
        if re.search(r"\b(street|barangay|brgy\.?|subdivision|building|unit no\.?|postal code|zip code)\b", low):
            continue
        result.append(line)
    return "\n".join(result)[:30000]


def decode_b64(value: str) -> str:
    data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return data.decode("utf-8", errors="replace")


def mime_text(service, payload: dict, execute=None) -> str:
    parts: dict[str, list[str]] = {"text/plain": [], "text/html": []}

    def walk(part):
        mime = part.get("mimeType", "").lower()
        body = part.get("body", {})
        if mime in parts:
            data = body.get("data")
            if not data and body.get("attachmentId"):
                # Gmail sometimes stores even MIME text bodies as attachments.
                request = service.users().messages().attachments().get(
                    userId="me", messageId=payload["id"], id=body["attachmentId"]
                )
                data = (execute(request, 20) if execute else request.execute()).get("data")
            if data:
                parts[mime].append(decode_b64(data))
        for child in part.get("parts", []):
            walk(child)

    walk(payload["payload"])
    if parts["text/html"]:
        return html_to_text("\n".join(parts["text/html"]))
    return "\n".join(parts["text/plain"])


def header(message: dict, name: str) -> str:
    value = next((h.get("value", "") for h in message.get("payload", {}).get("headers", []) if h.get("name", "").lower() == name.lower()), "")
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeError, LookupError):
        return value


def is_shopee_confirmation(message: dict, domain: str) -> bool:
    return is_shop_confirmation(message, domain)


def is_shop_confirmation(message: dict, domain: str) -> bool:
    sender = parseaddr(header(message, "From"))[1].lower()
    host = sender.rsplit("@", 1)[-1]
    subject = header(message, "Subject").lower()
    if not (sender == domain if "@" in domain else host == domain or host.endswith("." + domain)):
        return False
    if nontransaction_notice(subject) or (domain == "shopee.ph" and shopee_pending_refund(subject)):
        return False
    return bool(re.search(r"\b(order|payment|paid|refund|cancel\w*)\b", subject)
                or (domain == "shopee.ph" and shopee_review_result(subject)))


def shopee_pending_refund(subject: str) -> bool:
    return bool(re.search(r"\breturn/refund under review\s*:", subject, re.IGNORECASE))


def shopee_review_result(subject: str) -> bool:
    return bool(re.search(r"\breview result for request\b", subject, re.IGNORECASE))


def nontransaction_notice(subject: str) -> bool:
    subject = subject.lower()
    fulfillment = re.search(r"\b(delivered|received|shipped|shipping update|out for delivery)\b", subject)
    transaction = re.search(r"\b(payment|paid|refund|cancel\w*)\b", subject)
    return bool(fulfillment and not transaction)


def record_refund_event(db: sqlite3.Connection, event_key: str, platform: str,
                        reference_type: str, reference_id: str, amount_cents: int,
                        occurred_ms: int, gmail_id: str, order_id: str | None = None) -> None:
    existing = db.execute("SELECT * FROM refund_events WHERE event_key=?", (event_key,)).fetchone()
    if existing and reference_type == "request" and existing["amount_cents"] != amount_cents:
        db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)",
                   (gmail_id, order_id or "", "refund_amount_conflict",
                    f"Request {reference_id}: saved {php(existing['amount_cents'])}, later notice {php(amount_cents)}; saved amount retained"))
        return
    candidate = None if existing or reference_type == "request" else db.execute("""
        SELECT event_key FROM refund_events WHERE platform=? AND order_id=? AND amount_cents=?
          AND event_key<>? AND state='counted' AND archived=0
          AND abs(occurred_ms-?)<=604800000 ORDER BY occurred_ms LIMIT 1""",
        (platform, order_id, amount_cents, event_key, occurred_ms)).fetchone() if order_id else None
    db.execute("""INSERT INTO refund_events
        (event_key,platform,reference_type,reference_id,amount_cents,occurred_ms,source_gmail_id,order_id)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(event_key) DO UPDATE SET platform=excluded.platform,
          reference_type=excluded.reference_type, reference_id=excluded.reference_id,
          amount_cents=excluded.amount_cents, occurred_ms=excluded.occurred_ms,
          source_gmail_id=excluded.source_gmail_id, order_id=excluded.order_id""",
        (event_key, platform, reference_type, reference_id, amount_cents, occurred_ms, gmail_id, order_id))
    if existing:
        incoming = {"amount_cents": amount_cents, "occurred_ms": occurred_ms, "order_id": order_id}
        conflicts = []
        for override in db.execute("SELECT field,value_json FROM refund_overrides WHERE event_key=?", (event_key,)).fetchall():
            field, saved = override["field"], json.loads(override["value_json"])
            if incoming.get(field) is not None and incoming[field] != saved:
                conflicts.append(f"{field}: email {incoming[field]!r}, correction {saved!r}")
                db.execute("INSERT OR REPLACE INTO conflict_values VALUES (?,?,?,?,?)",
                           (gmail_id, event_key, "refund", field, json.dumps(incoming[field])))
            db.execute(f"UPDATE refund_events SET {field}=? WHERE event_key=?", (saved, event_key))
        if conflicts:
            db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)",
                       (gmail_id, order_id or "", "manual_refund_conflict", "; ".join(conflicts)[:500]))
    if candidate:
        db.execute("UPDATE refund_events SET state='review',duplicate_of=? WHERE event_key=?",
                   (candidate["event_key"], event_key))
        db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)",
                   (gmail_id, order_id, "possible_duplicate_refund",
                    f"{php(amount_cents)} may repeat refund {candidate['event_key']}; held from totals until reviewed"))


def apply_refund_evidence(db: sqlite3.Connection, event_key: str, refund_items: list[dict]) -> bool:
    event = db.execute("SELECT * FROM refund_events WHERE event_key=?", (event_key,)).fetchone()
    if not event:
        raise ValueError("Refund was not found")
    if db.execute("SELECT 1 FROM refund_allocations WHERE event_key=? AND source='manual' LIMIT 1", (event_key,)).fetchone():
        if refund_items:
            db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)",
                       (event["source_gmail_id"], event["order_id"] or "", "manual_refund_conflict",
                        "Later email item evidence exists; manually assigned refund categories were retained"))
        return True
    purchase_items = [dict(row) for row in db.execute("SELECT * FROM items WHERE order_id=?", (event["order_id"],))]
    allocations = []
    ambiguous = False
    for evidence in refund_items:
        name = re.sub(r"\s+", " ", str(evidence.get("name", ""))).strip().casefold()
        matches = [item for item in purchase_items if re.sub(r"\s+", " ", item["name"]).strip().casefold() == name]
        if not name or len(matches) != 1:
            ambiguous = True
            break
        raw_amount = evidence.get("amount", "")
        amount = money_cents(raw_amount) if str(raw_amount).strip() else None
        allocations.append((matches[0]["position"], matches[0]["category"], amount))
    if allocations and not ambiguous and all(amount is not None for _, _, amount in allocations):
        amounts = {position: amount for position, _, amount in allocations}
        ambiguous = len(amounts) != len(allocations) or sum(amounts.values()) != event["amount_cents"]
    elif allocations and not ambiguous:
        categories = {category for _, category, _ in allocations}
        if len(categories) == 1 and all(amount is None for _, _, amount in allocations):
            allocations = [(-1, allocations[0][1], event["amount_cents"])]
        else:
            ambiguous = True
    with db:
        db.execute("DELETE FROM refund_allocations WHERE event_key=?", (event_key,))
        if allocations and not ambiguous:
            db.executemany("INSERT INTO refund_allocations VALUES (?,?,?,?,?)",
                           [(event_key, position, category, amount, "email") for position, category, amount in allocations])
        state = "ambiguous" if ambiguous else "allocated" if allocations else "unallocated"
        db.execute("UPDATE refund_events SET evidence_state=? WHERE event_key=?", (state, event_key))
        db.execute("DELETE FROM issues WHERE gmail_id=? AND order_id=? AND code='refund_evidence'",
                   (event["source_gmail_id"], event["order_id"] or ""))
        if ambiguous:
            db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)",
                       (event["source_gmail_id"], event["order_id"] or "", "refund_evidence",
                        "Returned item amounts could not be tied to one category; category refund remains unallocated"))
    return bool(allocations and not ambiguous)


def receipt_order_ids(text: str) -> list[str]:
    """Find IDs explicitly labelled in a receipt, preserving their order."""
    return list(dict.fromkeys(re.findall(
        r"(?im)^\s*Order ID:\s*(?:\n\s*)?#?([A-Za-z0-9-]{5,50})\b", text
    )))


def shopee_approved_refund(subject: str, text: str) -> dict | None:
    """Read the approved amount from Shopee's review-result notice."""
    if not shopee_review_result(subject):
        return None
    ids = re.findall(r"(?i)\breview result for request\s*#?([A-Za-z0-9-]{5,50})\b", subject)
    amounts = re.findall(r"(?i)\brefund worth\s*₱\s*([\d,]+(?:\.\d{1,2})?)\s+has been approved\b", text)
    if len(ids) != 1 or len(amounts) != 1 or ids[0].lower() not in text.lower():
        return None
    amount = money_cents(amounts[0])
    if not amount:
        return None
    return {"orders": [{"order_id": ids[0], "status": "partial_refund",
                        "paid_total": "", "refund_amount": amounts[0], "items": []}]}


def ingest_shopee_refund_request(db: sqlite3.Connection, gmail_id: str, received_ms: int,
                                 subject: str, parsed: dict, source_text: str) -> str:
    match = re.search(r"(?i)\breview result for request\s*#?([A-Za-z0-9-]{5,50})\b", subject)
    if (not match or not isinstance(parsed, dict) or not isinstance(parsed.get("orders"), list)
            or not all(isinstance(order, dict) for order in parsed["orders"])):
        raise ValueError("Shopee review result has no valid refund request")
    request_id = match.group(1)
    matches = [order for order in parsed["orders"]
               if str(order.get("order_id", "")).strip().lstrip("#").lower() == request_id.lower()]
    if not matches and len(parsed["orders"]) == 1 and not str(parsed["orders"][0].get("order_id", "")).strip():
        matches = parsed["orders"]
    if len(matches) != 1 or (source_text and request_id.lower() not in source_text.lower()):
        raise ValueError("Shopee review result does not identify one matching refund request")
    if source_text and not re.search(r"(?i)\b(approved|decided in your favor)\b", source_text):
        raise ValueError("Shopee review result does not explicitly approve a refund")
    amount = money_cents(matches[0].get("refund_amount"))
    if not amount:
        raise ValueError("Shopee review result has no explicit refund amount; nothing was saved")
    with db:
        db.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,NULL)",
                   (gmail_id, received_ms, subject[:250], "processed"))
        db.execute("DELETE FROM issues WHERE gmail_id=?", (gmail_id,))
        db.execute("DELETE FROM conflict_values WHERE gmail_id=?", (gmail_id,))
        db.execute("DELETE FROM review_checks WHERE kind='issue' AND substr(review_key,1,length(?)+1)=? || ':'",
                   (gmail_id, gmail_id))
        linked_ids = receipt_order_ids(source_text)
        linked_order = linked_ids[0] if len(linked_ids) == 1 else None
        record_refund_event(db, f"shopee-request:{request_id}", "Shopee", "request",
                            request_id, amount, received_ms, gmail_id, linked_order)
        apply_refund_evidence(db, f"shopee-request:{request_id}", matches[0].get("refund_items", []))
        if linked_order:
            db.execute("DELETE FROM issues WHERE order_id=? AND code='unknown_refund'", (linked_order,))
    return "processed 1 refund request"


def ingest(db: sqlite3.Connection, gmail_id: str, received_ms: int, subject: str, parsed: dict, source_text: str = "", platform: str = "Shopee") -> str:
    if platform not in ("Shopee", "TikTok Shop"):
        raise ValueError("Unknown shop platform")
    if platform == "Shopee" and shopee_pending_refund(subject):
        record_ignored(db, gmail_id, received_ms, subject)
        return "ignored pending refund review"
    if platform == "Shopee" and shopee_review_result(subject):
        return ingest_shopee_refund_request(db, gmail_id, received_ms, subject, parsed, source_text)
    existing = db.execute("SELECT state FROM messages WHERE gmail_id=?", (gmail_id,)).fetchone()
    if existing and existing["state"] == "processed":
        return "already processed"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("orders"), list):
        raise ValueError("Structured response has no orders array")
    orders = parsed["orders"]
    if not all(isinstance(order, dict) for order in orders):
        raise ValueError("Structured response has an invalid order")
    summary = tiktok_package_summary(source_text) if platform == "TikTok Shop" and len(orders) == 1 else None
    receipt_ids = receipt_order_ids(source_text)
    parsed_ids = [str(order.get("order_id", "")).strip().lstrip("#") for order in orders]
    valid_ids = {oid for oid in parsed_ids if re.fullmatch(r"[A-Za-z0-9-]{5,50}", oid)}
    missing_positions = [index for index, oid in enumerate(parsed_ids) if not re.fullmatch(r"[A-Za-z0-9-]{5,50}", oid)]
    unmatched_receipt_ids = [oid for oid in receipt_ids if oid not in valid_ids]
    # An explicit receipt ID can repair one missing model ID when the mapping
    # is unambiguous. Never guess which order owns an ID in a multi-order email.
    if len(missing_positions) == len(unmatched_receipt_ids) == 1 and len(receipt_ids) == len(orders):
        parsed_ids[missing_positions[0]] = unmatched_receipt_ids[0]
    seen: set[str] = set()
    validated = []
    unverified = []
    for index, order in enumerate(orders):
        if (not isinstance(order.get("items", []), list) or
                not all(isinstance(item, dict) for item in order.get("items", [])) or
                not isinstance(order.get("refund_items", []), list) or
                not all(isinstance(item, dict) for item in order.get("refund_items", []))):
            raise ValueError("Structured response has invalid item details")
        oid = parsed_ids[index]
        if not oid or not re.fullmatch(r"[A-Za-z0-9-]{5,50}", oid):
            found = ", ".join(receipt_ids) if receipt_ids else "none"
            raise ValueError(f"Order {index + 1} of {len(orders)} has no valid extracted ID; receipt IDs: {found}; nothing was saved")
        if oid in seen:
            raise ValueError("Duplicate order ID within one email; nothing was saved")
        if source_text and oid.lower() not in (source_text + " " + subject).lower():
            raise ValueError("Extracted order ID was not present in the email; nothing was saved")
        seen.add(oid)
        order_key = oid if platform == "Shopee" else f"tiktok:{oid}"
        status = order.get("status")
        if status not in ("placed", "paid", "cancelled", "refunded", "partial_refund"):
            raise ValueError("Invalid order status")
        method = order.get("payment_method", "online")  # Older direct-import fixtures predate payment classification.
        if method not in ("online", "cash", "unknown"):
            raise ValueError("Invalid payment method")
        if len(orders) == 1 and re.search(r"(?im)^\s*payment\s+method\s*[:\-]?\s*(?:cod|cash(?:\s+on\s+delivery)?)(?:\s*\(cod\))?\s*$", source_text):
            method = "cash"
        if status == "placed" or method == "cash":
            continue
        if method == "unknown" and (status == "paid" or not db.execute("SELECT 1 FROM orders WHERE order_id=?", (order_key,)).fetchone()):
            unverified.append(order_key)
            continue
        paid = money_cents(order.get("paid_total"))
        if status == "paid" and paid is None:
            raise ValueError("Paid order has no per-order paid total; nothing was saved")
        refund = money_cents(order.get("refund_amount")) or 0
        if platform == "TikTok Shop" and status in ("cancelled", "refunded", "partial_refund") and source_text:
            receipt_refund = tiktok_refund_total(source_text)
            if receipt_refund is not None:
                refund = receipt_refund
        rows = []
        for item in order.get("items", []):
            name = re.sub(r"\s+", " ", str(item.get("name", ""))).strip()[:300]
            qty = item.get("quantity")
            if not name or not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                raise ValueError("Invalid item name or quantity")
            category = item.get("category", "Other")
            if category not in CATEGORIES:
                category = "Other"
            rows.append((name, qty, money_cents(item.get("unit_price")), money_cents(item.get("line_total")), category))
        protection = money_cents(order.get("protection_subtotal")) if platform == "Shopee" else None
        coins = money_cents(order.get("coins_used")) if platform == "Shopee" else None
        coins_inferred = 0
        shipping = money_cents(order.get("shipping_fee"))
        discount = money_cents(order.get("discount_total"))
        subtotal = handling = None
        if platform == "Shopee" and status == "paid" and source_text and rows and all(row[3] is not None for row in rows):
            receipt = shopee_order_summary(source_text, oid)
            item_total = sum(row[3] for row in rows)
            if (receipt and receipt["subtotal"] == item_total and receipt["shipping"] == (shipping or 0)
                    and receipt["protection"] == (protection or 0) and receipt["discounts"] == (discount or 0)
                    and receipt["total"] == paid):
                gap = item_total + (shipping or 0) + (protection or 0) - (discount or 0) - paid
                if gap > 0 and receipt["coins"] is None:
                    coins, coins_inferred = gap, 1
                elif receipt["coins"] is not None:
                    coins = receipt["coins"]
        if summary and status == "paid":
            if paid != summary["total"]:
                raise ValueError("TikTok paid amount differs from the package summary; nothing was saved")
            shipping, discount = summary["shipping"], summary["discounts"]
            subtotal, handling = summary["subtotal"], summary["fees"]
        validated.append((order_key, status, paid, shipping, protection, discount, coins, coins_inferred, subtotal, handling, refund, rows, order.get("refund_items", [])))
    with db:
        db.execute("INSERT OR REPLACE INTO messages (gmail_id,received_ms,subject,state,error) VALUES (?,?,?,?,NULL)",
                   (gmail_id, received_ms, subject[:250], "processed"))
        db.execute("DELETE FROM issues WHERE gmail_id=?", (gmail_id,))
        db.execute("DELETE FROM conflict_values WHERE gmail_id=?", (gmail_id,))
        db.execute("DELETE FROM review_checks WHERE kind='issue' AND substr(review_key,1,length(?)+1)=? || ':'",
                   (gmail_id, gmail_id))
        if not orders:
            db.execute("INSERT INTO issues VALUES (?,?,?,?)", (gmail_id, "", "no_orders", "No orders found in confirmation"))
        for oid in unverified:
            db.execute("INSERT INTO issues VALUES (?,?,?,?)",
                       (gmail_id, oid, "payment_unverified", "Payment method was not confirmed online; check the source email before adding this purchase"))
        for oid, status, paid, shipping, protection, discount, coins, coins_inferred, subtotal, handling, refund, rows, refund_items in validated:
            if refund:
                record_refund_event(db, f"order:{gmail_id}:{oid}", platform, "order",
                                    oid.removeprefix("tiktok:"), refund, received_ms, gmail_id, oid)
                db.execute("DELETE FROM issues WHERE order_id=? AND code='unknown_refund'", (oid,))
            old = db.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()
            if old and old["updated_ms"] > received_ms:
                continue
            if not old and status != "paid" and paid is None:
                if not refund:
                    db.execute("INSERT INTO issues VALUES (?,?,?,?)", (gmail_id, oid, "unknown_refund", "No paid order or explicit refund amount was found"))
                continue
            if old:
                db.execute("""UPDATE orders SET updated_ms=?, status=?, paid_cents=?, shipping_cents=?,
                  protection_cents=?, discount_cents=?, coins_cents=?, coins_inferred=?, subtotal_cents=?, handling_cents=?,
                  source_gmail_id=? WHERE order_id=?""",
                  (received_ms, status, paid if paid is not None else old["paid_cents"],
                   shipping if shipping is not None else old["shipping_cents"],
                   protection if protection is not None else old["protection_cents"],
                   discount if discount is not None else old["discount_cents"],
                   coins if coins is not None else old["coins_cents"],
                   coins_inferred if status == "paid" else old["coins_inferred"],
                   subtotal if subtotal is not None else old["subtotal_cents"],
                   handling if handling is not None else old["handling_cents"], gmail_id, oid))
            else:
                db.execute("""INSERT INTO orders
                    (order_id,ordered_ms,updated_ms,status,paid_cents,shipping_cents,protection_cents,discount_cents,coins_cents,coins_inferred,subtotal_cents,handling_cents,refund_cents,source_gmail_id,platform)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (oid, received_ms, received_ms, status, paid, shipping, protection, discount, coins, coins_inferred, subtotal, handling, 0, gmail_id, platform))
            if rows:
                prior_items = {x["position"]: dict(x) for x in db.execute("SELECT * FROM items WHERE order_id=?", (oid,))}
                db.execute("DELETE FROM items WHERE order_id=?", (oid,))
                db.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?)",
                    [(oid, i, *row) for i, row in enumerate(rows)])
            if old and old["manual_override"]:
                incoming = db.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()
                conflicts = []
                for override in db.execute("SELECT field,value_json FROM order_overrides WHERE order_id=?", (oid,)).fetchall():
                    field, saved = override["field"], json.loads(override["value_json"])
                    if field.startswith("item:"):
                        _, position_text, item_field = field.split(":", 2)
                        position = int(position_text)
                        current_item = db.execute("SELECT * FROM items WHERE order_id=? AND position=?", (oid, position)).fetchone()
                        if current_item is None and rows and position in prior_items:
                            old_item = prior_items[position]
                            db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?)",
                                       (oid, position, old_item["name"], old_item["quantity"], old_item["unit_cents"], old_item["line_cents"], old_item["category"]))
                            current_item = db.execute("SELECT * FROM items WHERE order_id=? AND position=?", (oid, position)).fetchone()
                        if current_item is not None:
                            if rows and current_item[item_field] != saved:
                                conflicts.append(f"item {position + 1} {item_field}: email {current_item[item_field]!r}, correction {saved!r}")
                                db.execute("INSERT OR REPLACE INTO conflict_values VALUES (?,?,?,?,?)",
                                           (gmail_id, oid, "order", field, json.dumps(current_item[item_field])))
                            db.execute(f"UPDATE items SET {item_field}=? WHERE order_id=? AND position=?", (saved, oid, position))
                    elif field in incoming.keys():
                        if incoming[field] != saved and (field == "status" or incoming[field] is not None):
                            conflicts.append(f"{field}: email {incoming[field]!r}, correction {saved!r}")
                            db.execute("INSERT OR REPLACE INTO conflict_values VALUES (?,?,?,?,?)",
                                       (gmail_id, oid, "order", field, json.dumps(incoming[field])))
                        db.execute(f"UPDATE orders SET {field}=? WHERE order_id=?", (saved, oid))
                if conflicts:
                    db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)",
                               (gmail_id, oid, "manual_conflict", "; ".join(conflicts)[:500]))
            if status == "paid" and rows:
                db.execute("DELETE FROM issues WHERE order_id=? AND code='order_mismatch'", (oid,))
            current = db.execute("SELECT paid_cents,shipping_cents,protection_cents,discount_cents,coins_cents,subtotal_cents,handling_cents,status FROM orders WHERE order_id=?", (oid,)).fetchone()
            line_sum = sum(x[3] for x in rows if x[3] is not None)
            expected = None
            if current["paid_cents"] is not None and current["status"] == "paid" and platform == "TikTok Shop" and current["subtotal_cents"] is not None:
                expected = current["subtotal_cents"] + (current["shipping_cents"] or 0) + (current["handling_cents"] or 0) - (current["discount_cents"] or 0)
                detail = f"Subtotal + shipping + fees - discounts {php(expected)} vs paid {php(current['paid_cents'])}"
            elif rows and all(x[3] is not None for x in rows) and current["paid_cents"] is not None and current["status"] == "paid" and platform == "Shopee":
                expected = line_sum + (current["shipping_cents"] or 0) + (current["protection_cents"] or 0) - (current["discount_cents"] or 0) - (current["coins_cents"] or 0)
                detail = f"Items + shipping + protection - discounts - redeemed credits {php(expected)} vs paid {php(current['paid_cents'])}"
            if expected is not None:
                if expected != current["paid_cents"]:
                    db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)", (gmail_id, oid, "order_mismatch", detail))
            if status in ("partial_refund", "refunded", "cancelled") and not refund:
                db.execute("INSERT OR REPLACE INTO issues VALUES (?,?,?,?)", (gmail_id, oid, "unknown_refund", "Refund amount was not stated"))
            if refund:
                apply_refund_evidence(db, f"order:{gmail_id}:{oid}", refund_items)
    return f"processed {len(validated)} order(s)"


def record_error(db: sqlite3.Connection, gmail_id: str, received_ms: int, subject: str, error: Exception) -> None:
    with db:
        db.execute("DELETE FROM review_checks WHERE kind='message' AND review_key=?", (gmail_id,))
        db.execute("INSERT OR REPLACE INTO messages (gmail_id,received_ms,subject,state,error) VALUES (?,?,?,?,?)",
                   (gmail_id, received_ms, subject[:250], "error", str(error)[:250]))


def record_ignored(db: sqlite3.Connection, gmail_id: str, received_ms: int, subject: str) -> None:
    with db:
        db.execute("INSERT OR REPLACE INTO messages (gmail_id,received_ms,subject,state,error) VALUES (?,?,?,?,NULL)",
                   (gmail_id, received_ms, subject[:250], "ignored"))


def seed_demo(db: sqlite3.Connection) -> None:
    fixture = json.loads((ROOT / "fixtures" / "two_orders.json").read_text(encoding="utf-8"))
    ingest(db, "synthetic-demo-message", 1794310000000, "Shopee payment confirmation (synthetic)", fixture)
