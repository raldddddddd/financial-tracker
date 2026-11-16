"""Read-only period selection and spending trend calculations for the dashboard."""

from __future__ import annotations

import re
import sqlite3
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .core import CATEGORIES, MANILA_TZ

ORDERS_PER_PAGE = 20
SHOPS = {"all": "All shops", "shopee": "Shopee", "tiktok": "TikTok Shop", "other": "Other"}


@dataclass(frozen=True)
class PeriodSelection:
    mode: str = "all"
    label: str = "All time"
    start: date | None = None
    end: date | None = None
    year: str = ""
    month: str = ""
    start_value: str = ""
    end_value: str = ""
    error: str = ""


@dataclass(frozen=True)
class DashboardSelection:
    shop: str
    category: str
    trend_group: str
    search: str
    period: PeriodSelection
    orders: list[dict]
    refunds: list[dict]
    visible_orders: list[dict]
    categories: dict[str, int]
    net_categories: dict[str, int]
    products: list[tuple[str, str, int, int]]
    shop_totals: dict[str, int]
    paid_total: int
    refund_total: int
    total: int
    discounts_total: int
    current_spend: int
    previous_spend: int
    category_total: int
    detail_count: int
    page_count: int
    page_number: int
    first_index: int
    this_month: date


def order_day(order: dict) -> date:
    return datetime.fromtimestamp(order["ordered_ms"] / 1000, MANILA_TZ).date()


def refund_day(refund: dict) -> date:
    return datetime.fromtimestamp(refund["occurred_ms"] / 1000, MANILA_TZ).date()


def available_years(orders: list[dict], refunds: list[dict] = ()) -> list[int]:
    return sorted({datetime.now(MANILA_TZ).year, *(order_day(order).year for order in orders),
                   *(refund_day(refund).year for refund in refunds)}, reverse=True)


def resolve_period(orders: list[dict], year: str = "", start: str = "", end: str = "",
                   refunds: list[dict] = (), month: str = "") -> PeriodSelection:
    if sum(bool(value) for value in (year, month, start or end)) > 1:
        return PeriodSelection(error="Choose a year, a month, or a custom date range.")
    if month:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                raise ValueError
            first = date.fromisoformat(f"{month}-01")
        except ValueError:
            return PeriodSelection(error="Choose a valid month.")
        last = date(first.year, first.month, monthrange(first.year, first.month)[1])
        return PeriodSelection(mode="month", label=first.strftime("%b %Y"),
                               start=first, end=last, month=month)
    if start or end:
        values = {"start_value": start if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) else "",
                  "end_value": end if re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) else ""}
        if not start or not end:
            return PeriodSelection(error="Enter both a start and end date.", **values)
        try:
            if not values["start_value"] or not values["end_value"]:
                raise ValueError
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            return PeriodSelection(error="Enter real dates in YYYY-MM-DD format.", **values)
        if first > last:
            return PeriodSelection(error="The end date must be on or after the start date.", **values)
        if (last - first).days > 25 * 366:
            return PeriodSelection(error="Choose a date range of 25 years or less.", **values)
        return PeriodSelection(mode="custom", label=f"{first:%b %d, %Y} – {last:%b %d, %Y}",
                               start=first, end=last, **values)
    if year:
        if not re.fullmatch(r"\d{4}", year) or int(year) not in available_years(orders, refunds):
            return PeriodSelection(error="Choose a year from the list.")
        first, last = date(int(year), 1, 1), date(int(year), 12, 31)
        return PeriodSelection(mode="year", label=year, start=first, end=last, year=year)
    return PeriodSelection()


def filter_period(orders: list[dict], period: PeriodSelection) -> list[dict]:
    if period.mode == "all":
        return orders
    return [order for order in orders if period.start <= order_day(order) <= period.end]


def filter_refunds(refunds: list[dict], period: PeriodSelection) -> list[dict]:
    if period.mode == "all":
        return refunds
    return [refund for refund in refunds if period.start <= refund_day(refund) <= period.end]


def top_products(orders: list[dict], limit: int = 5) -> list[tuple[str, str, int, int]]:
    products: dict[tuple[str, str], list] = {}
    for order in orders:
        items = order["items"]
        paid = order["paid_cents"] or 0
        if not paid or not items or any(item["line_cents"] is None for item in items):
            continue
        item_total = sum(item["line_cents"] for item in items)
        if item_total <= 0:
            continue
        assigned = 0
        for index, item in enumerate(items):
            amount = paid - assigned if index == len(items) - 1 else paid * item["line_cents"] // item_total
            assigned += amount
            key = (item["name"].strip().casefold(), item["category"])
            product = products.setdefault(key, [item["name"], item["category"], 0, 0])
            product[2] += amount
            product[3] += item["quantity"]
    return [tuple(product) for product in sorted(products.values(), key=lambda product: -product[2])[:limit]]


def month_net(orders: list[dict], refunds: list[dict], month: date) -> int:
    def same_month(day: date) -> bool:
        return day.year == month.year and day.month == month.month

    return (sum(order["paid_cents"] or 0 for order in orders
                if same_month(order_day(order)))
            - sum(refund["amount_cents"] for refund in refunds
                  if same_month(refund_day(refund))))


def trend_series(orders: list[dict], period: PeriodSelection,
                 refunds: list[dict] = (), group_by: str = "year") -> tuple[str, list[tuple[str, int]]]:
    """Return net spending by purchase date and refund date, including empty periods."""
    if not orders and not refunds:
        return "", []
    if period.mode == "all":
        days_seen = [order_day(order) for order in orders] + [refund_day(refund) for refund in refunds]
        if group_by == "month":
            units = []
            year, month = min(days_seen).year, min(days_seen).month
            while (year, month) <= (max(days_seen).year, max(days_seen).month):
                units.append(((year, month), date(year, month, 1).strftime("%b %Y")))
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            granularity = "month"
        else:
            years = range(min(day.year for day in days_seen), max(day.year for day in days_seen) + 1)
            units = [(year, str(year)) for year in years]
            granularity = "year"
    elif period.mode == "year":
        units = [((int(period.year), month), date(int(period.year), month, 1).strftime("%b"))
                 for month in range(1, 13)]
        granularity = "month"
    elif period.mode == "month" or (period.end - period.start).days <= 30:
        days = (period.end - period.start).days + 1
        units = [(period.start + timedelta(days=index),
                  (period.start + timedelta(days=index)).strftime("%b %d" if period.mode == "month" else "%b %d, %Y"))
                 for index in range(days)]
        granularity = "day"
    elif (period.end.year - period.start.year) * 12 + period.end.month - period.start.month < 24:
        units = []
        year, month = period.start.year, period.start.month
        while (year, month) <= (period.end.year, period.end.month):
            units.append(((year, month), date(year, month, 1).strftime("%b %Y")))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        granularity = "month"
    else:
        units = [(year, str(year)) for year in range(period.start.year, period.end.year + 1)]
        granularity = "year"
    totals = {key: 0 for key, _ in units}
    for order in orders:
        day = order_day(order)
        key = day if granularity == "day" else (day.year, day.month) if granularity == "month" else day.year
        totals[key] += order["paid_cents"] or 0
    for refund in refunds:
        day = refund_day(refund)
        key = day if granularity == "day" else (day.year, day.month) if granularity == "month" else day.year
        totals[key] -= refund["amount_cents"]
    return granularity, [(label, totals[key]) for key, label in units]


def order_category_allocations(order: dict) -> dict[str, int]:
    """Allocate the paid amount to item categories without applying refunds."""
    paid = order["paid_cents"] or 0
    if not paid:
        return {}
    weighted = [(item["category"], item["line_cents"])
                for item in order["items"] if item["line_cents"] is not None and item["line_cents"] > 0]
    denominator = sum(value for _, value in weighted)
    if not denominator:
        return {"Unallocated": paid}
    categories: dict[str, int] = {}
    assigned = 0
    for index, (category, value) in enumerate(weighted):
        allocation = paid - assigned if index == len(weighted) - 1 else paid * value // denominator
        categories[category] = categories.get(category, 0) + allocation
        assigned += allocation
    return categories


def spending_summary(orders: list[dict]) -> tuple[int, dict[str, int]]:
    categories: dict[str, int] = {}
    total = 0
    for order in orders:
        total += order["paid_cents"] or 0
        for category, amount in order["category_allocations"].items():
            categories[category] = categories.get(category, 0) + amount
    return total, categories


def dashboard_data(db: sqlite3.Connection) -> dict:
    orders = [dict(x) for x in db.execute("SELECT * FROM orders WHERE archived=0 ORDER BY ordered_ms DESC")]
    items_by_order: dict[str, list[dict]] = {}
    for item in db.execute("SELECT * FROM items ORDER BY order_id,position"):
        items_by_order.setdefault(item["order_id"], []).append(dict(item))
    issues = [dict(x) for x in db.execute("""SELECT i.* FROM issues i LEFT JOIN review_checks c
        ON c.kind='issue' AND c.review_key=i.gmail_id || ':' || i.order_id || ':' || i.code
        LEFT JOIN orders o ON o.order_id=i.order_id
        WHERE c.review_key IS NULL AND i.code!='possible_duplicate_refund' AND coalesce(o.archived,0)=0
        ORDER BY CASE WHEN i.code IN ('manual_conflict','manual_refund_conflict','refund_amount_conflict','unknown_refund','refund_evidence') THEN 0 ELSE 1 END,i.gmail_id""")]
    refunds = [dict(x) for x in db.execute("""SELECT r.* FROM refund_events r
        LEFT JOIN orders o ON o.order_id=r.order_id
        WHERE r.state='counted' AND r.archived=0 AND coalesce(o.archived,0)=0
        ORDER BY r.occurred_ms DESC,r.event_key""")]
    allocations_by_refund: dict[str, list[dict]] = {}
    for allocation in db.execute("SELECT * FROM refund_allocations ORDER BY event_key,item_position"):
        allocations_by_refund.setdefault(allocation["event_key"], []).append(dict(allocation))
    for order in orders:
        order["date"] = datetime.fromtimestamp(order["ordered_ms"] / 1000, MANILA_TZ).strftime("%Y-%m-%d")
        order["items"] = items_by_order.get(order["order_id"], [])
        order["category_allocations"] = order_category_allocations(order)
    for refund in refunds:
        refund["date"] = datetime.fromtimestamp(refund["occurred_ms"] / 1000, MANILA_TZ).strftime("%Y-%m-%d")
        items = {item["position"]: item for item in items_by_order.get(refund["order_id"], [])}
        categories = {item["category"] for item in items.values()}
        if len(categories) == 1:
            refund["allocations"] = [{"item_position": -1, "category": next(iter(categories)),
                                      "amount_cents": refund["amount_cents"], "source": "order"}]
        else:
            refund["allocations"] = [dict(allocation, category=items[allocation["item_position"]]["category"])
                                     for allocation in allocations_by_refund.get(refund["event_key"], [])
                                     if allocation["item_position"] in items]
    latest_sync = db.execute("SELECT last_started_ms FROM sync_state ORDER BY last_started_ms DESC LIMIT 1").fetchone()
    latest_run = db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    budget = db.execute("SELECT value FROM ledger_meta WHERE key='monthly_budget_cents'").fetchone()
    return {"orders": orders, "issues": issues, "refunds": refunds,
            "monthly_budget_cents": int(budget[0]) if budget else None,
            "last_sync_ms": latest_sync[0] if latest_sync else None,
            "sync_run": dict(latest_run) if latest_run else None,
            "messages": [dict(x) for x in db.execute("""SELECT m.gmail_id,m.received_ms,m.subject,m.state,m.error
                FROM messages m LEFT JOIN review_checks c ON c.kind='message' AND c.review_key=m.gmail_id
                WHERE m.state='error' AND c.review_key IS NULL ORDER BY m.received_ms DESC""")],
            "pending_refunds": [dict(x) for x in db.execute("SELECT * FROM refund_events WHERE state='review' AND archived=0 ORDER BY occurred_ms DESC")],
            "conflicts": [dict(x) for x in db.execute("SELECT * FROM conflict_values ORDER BY gmail_id,target_key,field")],
            "archived_orders": [dict(x) for x in db.execute("SELECT order_id,merchant FROM orders WHERE archived=1 AND platform='Other'")],
            "archived_refunds": [dict(x) for x in db.execute("SELECT event_key,reference_id FROM refund_events WHERE archived=1 AND reference_type='manual'")],
            "changes": [dict(x) for x in db.execute("SELECT id,changed_ms,kind,target_key FROM changes WHERE undone_ms IS NULL ORDER BY id DESC LIMIT 20")]}


def select_dashboard(data: dict, page_number: int = 1, shop: str = "all", year: str = "",
                     start: str = "", end: str = "", category: str = "", month: str = "",
                     trend_group: str = "year", search: str = "") -> DashboardSelection:
    shop = shop if shop in SHOPS else "all"
    category = category if category in (*CATEGORIES, "Unallocated") else ""
    trend_group = "month" if trend_group == "month" else "year"
    search = search.strip()[:200]
    needle = search.casefold()
    period = resolve_period(data["orders"], year, start, end, data["refunds"], month)
    shop_orders = data["orders"] if shop == "all" else [order for order in data["orders"] if order["platform"] == SHOPS[shop]]
    shop_refunds = data["refunds"] if shop == "all" else [refund for refund in data["refunds"] if refund["platform"] == SHOPS[shop]]
    orders = filter_period(shop_orders, period)
    refunds = filter_refunds(shop_refunds, period)
    paid_total, categories = spending_summary(orders)
    refund_total = sum(refund["amount_cents"] for refund in refunds)
    discounts_total = sum(order["discount_cents"] or 0 for order in orders if order["paid_cents"])
    products = top_products(orders)
    this_month = datetime.now(MANILA_TZ).date().replace(day=1)
    current_spend = month_net(data["orders"], data["refunds"], this_month)
    previous_spend = month_net(data["orders"], data["refunds"], this_month - timedelta(days=1))
    net_categories = dict(categories)
    for refund in refunds:
        allocated = 0
        for allocation in refund.get("allocations", []):
            net_categories[allocation["category"]] = net_categories.get(allocation["category"], 0) - allocation["amount_cents"]
            allocated += allocation["amount_cents"]
        if allocated < refund["amount_cents"]:
            net_categories["Unallocated refunds"] = net_categories.get("Unallocated refunds", 0) - (refund["amount_cents"] - allocated)
    detail_orders = [order for order in orders if order["category_allocations"].get(category, 0) > 0] if category else orders
    if needle:
        detail_orders = [order for order in detail_orders if needle in order["order_id"].casefold() or
                         needle in order.get("merchant", "").casefold() or
                         any(needle in item["name"].casefold() for item in order["items"]
                             if not category or item["category"] == category)]
    detail_count = len(detail_orders)
    category_total = sum(order["category_allocations"].get(category, 0) for order in detail_orders) if category else 0
    page_count = max(1, (detail_count + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
    page_number = min(max(1, page_number), page_count)
    first = (page_number - 1) * ORDERS_PER_PAGE
    active_shops = {order["platform"] for order in orders} | {refund["platform"] for refund in refunds}
    shop_totals = ({name: sum(order["paid_cents"] or 0 for order in orders if order["platform"] == name)
                    - sum(refund["amount_cents"] for refund in refunds if refund["platform"] == name)
                    for name in ("Shopee", "TikTok Shop", "Other") if name in active_shops}
                   if shop == "all" and len(active_shops) > 1 else {})
    return DashboardSelection(
        shop=shop, category=category, trend_group=trend_group, search=search,
        period=period, orders=orders, refunds=refunds,
        visible_orders=detail_orders[first:first + ORDERS_PER_PAGE],
        categories=categories, net_categories=net_categories, products=products,
        shop_totals=shop_totals, paid_total=paid_total, refund_total=refund_total,
        total=paid_total - refund_total, discounts_total=discounts_total,
        current_spend=current_spend, previous_spend=previous_spend,
        category_total=category_total, detail_count=detail_count,
        page_count=page_count, page_number=page_number, first_index=first,
        this_month=this_month)
