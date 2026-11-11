from __future__ import annotations

import argparse
import html
import json
import os
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .charts import category_pie, line_chart
from .core import CATEGORIES, MANILA_TZ, ROOT, add_manual_purchase, add_manual_refund, archive_record, backup_database, bind_account, connect, correct_order, correct_refund, dashboard_data, dismiss_review, export_csv, gmail_service, ignore_email, load_env, php, purge_legacy_demo, recheck_refunds, resolve_conflict, resolve_refund_duplicate, run_sync, seed_demo, spending_summary, undo_change
from .insights import available_years, filter_period, filter_refunds, resolve_period, trend_series


ORDERS_PER_PAGE = 20
DASHBOARD_CSS = Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8")
DASHBOARD_JS = Path(__file__).with_name("dashboard.js").read_text(encoding="utf-8")


def page(data: dict, page_number: int = 1, shop: str = "all", year: str = "", start: str = "", end: str = "",
         category: str = "", month: str = "", trend_group: str = "year", search: str = "",
         csrf_token: str = "", notice: str = "", form_values: dict[str, str] | None = None) -> str:
    esc = lambda value: html.escape(str(value))
    form_values = form_values or {}
    def adjustments(order: dict) -> str:
        details = f'Shipping {php(order["shipping_cents"])} · discounts {php(order["discount_cents"])}'
        if order["platform"] == "Shopee" and order["coins_cents"]:
            source = " (inferred)" if order["coins_inferred"] else ""
            details += f' · Shopee coins{source} {php(order["coins_cents"])}'
        if order["platform"] == "TikTok Shop" and order["handling_cents"]:
            details += f' · handling fee {php(order["handling_cents"])}'
        return details

    def display_order_id(order: dict) -> str:
        if order["platform"] == "Other":
            return order["merchant"] or order["order_id"]
        return order["order_id"].split(":", 1)[1] if order["platform"] == "TikTok Shop" else order["order_id"]

    shops = {"all": "All shops", "shopee": "Shopee", "tiktok": "TikTok Shop", "other": "Other"}
    shop = shop if shop in shops else "all"
    category = category if category in (*CATEGORIES, "Unallocated") else ""
    trend_group = "month" if trend_group == "month" else "year"
    search = search.strip()[:200]
    needle = search.casefold()
    period = resolve_period(data["orders"], year, start, end, data["refunds"], month)
    shop_orders = data["orders"] if shop == "all" else [order for order in data["orders"] if order["platform"] == shops[shop]]
    shop_refunds = data["refunds"] if shop == "all" else [refund for refund in data["refunds"] if refund["platform"] == shops[shop]]
    orders = filter_period(shop_orders, period)
    refunds = filter_refunds(shop_refunds, period)
    paid_total, categories = spending_summary(orders)
    refund_total = sum(refund["amount_cents"] for refund in refunds)
    total = paid_total - refund_total
    all_orders_by_id = {order["order_id"]: order for order in data["orders"]}
    net_categories = dict(categories)
    for refund in refunds:
        allocated = 0
        for allocation in refund.get("allocations", []):
            net_categories[allocation["category"]] = net_categories.get(allocation["category"], 0) - allocation["amount_cents"]
            allocated += allocation["amount_cents"]
        if allocated < refund["amount_cents"]:
            net_categories["Unallocated refunds"] = net_categories.get("Unallocated refunds", 0) - (refund["amount_cents"] - allocated)
    order_count = len(orders)
    detail_orders = [order for order in orders if order["category_allocations"].get(category, 0) > 0] if category else orders
    if needle:
        detail_orders = [order for order in detail_orders if needle in order["order_id"].casefold() or
                         any(needle in item["name"].casefold() for item in order["items"]
                             if not category or item["category"] == category)]
    detail_count = len(detail_orders)
    category_total = sum(order["category_allocations"].get(category, 0) for order in detail_orders) if category else 0
    page_count = max(1, (detail_count + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
    page_number = min(max(1, page_number), page_count)
    first = (page_number - 1) * ORDERS_PER_PAGE
    visible_orders = detail_orders[first:first + ORDERS_PER_PAGE]
    def dashboard_url(shop_key: str = shop, page_index: int | None = None,
                      fragment: str = "", category_name: str = category,
                      group_name: str = trend_group, search_text: str = search) -> str:
        params = []
        if shop_key != "all":
            params.append(("shop", shop_key))
        if period.mode == "year":
            params.append(("year", period.year))
        elif period.mode == "month":
            params.append(("month", period.month))
        elif period.mode == "custom":
            params.extend((("start", period.start_value), ("end", period.end_value)))
        if group_name == "month":
            params.append(("group", "month"))
        if category_name:
            params.append(("category", category_name))
        if search_text:
            params.append(("q", search_text))
        if page_index is not None:
            params.append(("page", str(page_index)))
        return "/" + ("?" + urlencode(params) if params else "") + fragment

    def shop_tab(key: str, label: str) -> str:
        href = esc(dashboard_url(key, fragment="#shop-filter"))
        active_class = " active" if key == shop else ""
        current = ' aria-current="page"' if key == shop else ""
        return f'<a href="{href}" class="shop-tab{active_class}"{current}>{label}</a>'

    shop_tabs = "".join(shop_tab(key, label) for key, label in shops.items())
    hidden_shop = f'<input type="hidden" name="shop" value="{shop}">' if shop != "all" else ""
    hidden_category = f'<input type="hidden" name="category" value="{esc(category)}">' if category else ""
    hidden_group = '<input type="hidden" name="group" value="month">' if trend_group == "month" else ""
    hidden_search = f'<input type="hidden" name="q" value="{esc(search)}">' if search else ""
    hidden_period = (f'<input type="hidden" name="year" value="{period.year}">' if period.mode == "year" else
                     f'<input type="hidden" name="month" value="{period.month}">' if period.mode == "month" else
                     f'<input type="hidden" name="start" value="{period.start_value}">'
                     f'<input type="hidden" name="end" value="{period.end_value}">' if period.mode == "custom" else "")
    year_options = '<option value="">All time</option>' + "".join(
        f'<option value="{choice}"{" selected" if period.year == str(choice) else ""}>{choice}</option>'
        for choice in available_years(data["orders"], data["refunds"])
    )
    period_error = f'<p class="filter-error" role="alert">{esc(period.error)}</p>' if period.error else ""
    all_time_params = [("shop", shop)] if shop != "all" else []
    if category:
        all_time_params.append(("category", category))
    if trend_group == "month":
        all_time_params.append(("group", "month"))
    if search:
        all_time_params.append(("q", search))
    all_time_url = "/" + ("?" + urlencode(all_time_params) if all_time_params else "") + "#shop-filter"
    clear_dates = f'<a class="clear-dates" href="{esc(all_time_url)}">Clear dates</a>' if period.mode != "all" else ""
    filter_controls = f'''<div class="period-filter" aria-label="Spending period">
<form action="/" method="get" class="year-form"><label for="year-select">View year</label>{hidden_shop}{hidden_category}{hidden_group}{hidden_search}<select id="year-select" name="year">{year_options}</select><button type="submit">View</button></form>
<form action="/" method="get" class="month-form"><label for="month-select">View month</label>{hidden_shop}{hidden_category}{hidden_group}{hidden_search}<input id="month-select" name="month" type="month" value="{esc(period.month)}"><button type="submit">View</button></form>
<span class="filter-divider" aria-hidden="true"></span>
<form action="/" method="get" class="range-form"><span class="range-label">Custom range</span>{hidden_shop}{hidden_category}{hidden_group}{hidden_search}<label for="start-date">From</label><input id="start-date" name="start" type="date" value="{esc(period.start_value)}"><label for="end-date">To</label><input id="end-date" name="end" type="date" value="{esc(period.end_value)}"><button type="submit">Apply</button></form>{clear_dates}</div>{period_error}'''
    pie, legend = category_pie(categories, category, lambda name: dashboard_url(category_name=name, fragment="#orders"))
    category_content = (f'<div class="category-visual">{pie}<div class="category-legend">{legend}</div></div>'
                        if pie else '<p class="muted">No paid purchases in this period.</p>')
    net_category_rows = "".join(f'<li><span>{esc(name)}</span><strong>{php(amount)}</strong></li>'
                                for name, amount in sorted(net_categories.items()) if amount)
    net_category_content = (f'<details class="net-category"><summary>Net by category</summary><p>Only refunds supported by item evidence reduce a category. The rest stay unallocated.</p><ul>{net_category_rows}</ul></details>'
                            if net_category_rows else "")
    category_heading_clear = (f'<a class="category-reset" href="{esc(dashboard_url(category_name="", fragment="#overview"))}">All categories</a>'
                              if category else '')
    category_clear = (f'<a class="category-reset" href="{esc(dashboard_url(category_name="", fragment="#orders"))}">All categories</a>'
                      if category else '')
    csrf_field = f'<input type="hidden" name="csrf" value="{esc(csrf_token)}">'
    def amount_value(cents: int | None) -> str:
        return "" if cents is None else f"{cents // 100}.{cents % 100:02d}"
    def money_field(name: str, label: str, cents: int | None, minimum: str = "0", required: bool = True, raw_value: str | None = None) -> str:
        return (f'<label>{label}<input name="{name}" type="number" min="{minimum}" step="0.01" '
                f'value="{esc(raw_value) if raw_value is not None else amount_value(cents)}"{" required" if required else ""}></label>')
    category_options = lambda selected: "".join(
        f'<option value="{esc(name)}"{" selected" if selected == name else ""}>{esc(name)}</option>'
        for name in CATEGORIES)
    def order_items(order: dict) -> str:
        matches = [item for item in order["items"] if not category or item["category"] == category]
        return ", ".join(item["name"] + " ×" + str(item["quantity"]) + " · " + item["category"] for item in matches) or "No item detail"
    def edit_order(order: dict) -> str:
        if not csrf_token:
            return ""
        edit_values = form_values if form_values.get("op") == "order" and form_values.get("order_id") == order["order_id"] else {}
        def item_fields(item: dict) -> str:
            position = item["position"]
            return (f'<label>Item<input name="name_{position}" value="{esc(edit_values.get(f"name_{position}", item["name"]))}" maxlength="300" required></label>'
                    f'<label>Quantity<input name="quantity_{position}" type="number" min="1" step="1" value="{esc(edit_values.get(f"quantity_{position}", item["quantity"]))}" required></label>'
                    + money_field(f"unit_{position}", "Unit price (₱)", item["unit_cents"], required=False, raw_value=edit_values.get(f"unit_{position}"))
                    + money_field(f"line_{position}", "Item total (₱)", item["line_cents"], required=False, raw_value=edit_values.get(f"line_{position}"))
                    + f'<label>Category<select name="category_{position}">{category_options(edit_values.get(f"category_{position}", item["category"]))}</select></label>')
        fields = "".join(item_fields(item) for item in order["items"])
        status_options = "".join(f'<option value="{state}"{" selected" if state == edit_values.get("status", order["status"]) else ""}>{state.replace("_", " ").title()}</option>'
                                 for state in ("paid", "cancelled", "refunded", "partial_refund"))
        merchant_field = (f'<label>Merchant<input name="merchant" maxlength="120" value="{esc(edit_values.get("merchant", order["merchant"]))}" required></label>'
                          if order["platform"] == "Other" else "")
        return (f'<details class="edit-record"{" open" if edit_values else ""}><summary>Edit order</summary><form method="post" action="/action">{csrf_field}'
                f'<input type="hidden" name="op" value="order"><input type="hidden" name="order_id" value="{esc(order["order_id"])}">'
                f'<label>Date<input name="date" type="date" value="{esc(edit_values.get("date", order["date"]))}" required></label>'
                f'{money_field("paid", "Paid (₱)", order["paid_cents"], "0.01", raw_value=edit_values.get("paid"))}'
                f'<label>Status<select name="status">{status_options}</select></label>'
                f'{money_field("shipping", "Shipping (₱)", order["shipping_cents"], required=False, raw_value=edit_values.get("shipping"))}'
                f'{money_field("protection", "Protection (₱)", order["protection_cents"], required=False, raw_value=edit_values.get("protection"))}'
                f'{money_field("discount", "Discounts (₱)", order["discount_cents"], required=False, raw_value=edit_values.get("discount"))}'
                f'{money_field("coins", "Coins (₱)", order["coins_cents"], required=False, raw_value=edit_values.get("coins"))}'
                f'{money_field("handling", "Handling (₱)", order["handling_cents"], required=False, raw_value=edit_values.get("handling"))}'
                f'{merchant_field}'
                f'{fields}<button type="submit">Save correction</button></form></details>')
    def render_order(order: dict) -> str:
        shop_class = ("shop-tiktok" if order["platform"] == "TikTok Shop" else
                      "shop-other" if order["platform"] == "Other" else "shop-shopee")
        merchant = f'<small>Manual reference {esc(order["order_id"][-8:])}</small>' if order["platform"] == "Other" else ""
        corrected = '<small>Manually corrected</small>' if order.get("manual_override") and order["platform"] != "Other" else ""
        return ('<tr>'
                f'<td>{esc(order["date"])}</td><td><span class="shop-tag {shop_class}">{esc(order["platform"])}</span><strong>{esc(display_order_id(order))}</strong>{merchant}<small>{esc(order_items(order))}</small><small>{adjustments(order)}</small>{corrected}'
                f'{("<small>Full order paid " + php(order["paid_cents"]) + "</small>") if category else ""}{edit_order(order)}'
                + (f'<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="archive"><input type="hidden" name="kind" value="order"><input type="hidden" name="key" value="{esc(order["order_id"])}"><button type="submit">Archive mistaken entry</button></form>' if csrf_token and order["platform"] == "Other" else '') + '</td>'
                f'<td><span class="pill">{esc(order["status"].replace("_", " "))}</span></td>'
                f'<td class="number strong">{php(order["category_allocations"].get(category, 0) if category else order["paid_cents"])}</td></tr>')
    order_rows = "".join(render_order(order) for order in visible_orders) or ('<tr><td colspan="4" class="muted">No orders match this search in the selected shop, period, and category.</td></tr>' if search else '<tr><td colspan="4" class="muted">No purchases in this category for the selected shop and period.</td></tr>' if category else '<tr><td colspan="4" class="muted">No orders in this period. Try another year or date range.</td></tr>' if period.mode != "all" else '<tr><td colspan="4" class="muted">No orders yet. Run the sync command or load the demo fixture.</td></tr>')
    def render_refund(refund: dict) -> str:
        link = f'<small>Order {esc(refund["order_id"])}</small>' if refund.get("order_id") else '<small>Order not linked</small>'
        edit_values = form_values if form_values.get("op") == "refund" and form_values.get("event_key") == refund["event_key"] else {}
        existing_amounts = ({key.removeprefix("refund_item_"): value for key, value in edit_values.items() if key.startswith("refund_item_")}
                            if edit_values else {str(a["item_position"]): amount_value(a["amount_cents"]) for a in refund.get("allocations", [])})
        edit = (f'<details class="edit-record"{" open" if edit_values else ""}><summary>Edit refund</summary><form method="post" action="/action">{csrf_field}'
                f'<input type="hidden" name="op" value="refund"><input type="hidden" name="event_key" value="{esc(refund["event_key"])}">'
                f'<label>Date<input name="date" type="date" value="{esc(edit_values.get("date", refund["date"]))}" required></label>'
                f'{money_field("amount", "Refund (₱)", refund["amount_cents"], "0.01", raw_value=edit_values.get("amount"))}'
                f'<label>Order ID, if known<input name="order_id" list="tracked-orders" value="{esc(edit_values.get("order_id", refund.get("order_id") or ""))}" data-refund-order></label>'
                f'<div class="refund-item-fields" data-refund-items data-existing="{esc(json.dumps(existing_amounts))}"></div>'
                '<button type="submit">Save refund</button></form></details>') if csrf_token else ""
        shop_class = ("shop-tiktok" if refund["platform"] == "TikTok Shop" else
                      "shop-other" if refund["platform"] == "Other" else "shop-shopee")
        return (f'<li><div class="refund-info"><span class="shop-tag {shop_class}">{esc(refund["platform"])}</span>'
                f'{esc(refund["reference_type"].title())} {esc(refund["reference_id"])}<small>{esc(refund["date"])}</small>{link}'
                f'<small>{"Item evidence allocated" if refund.get("allocations") else "Category unallocated"}</small>{edit}'
                + (f'<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="archive"><input type="hidden" name="kind" value="refund"><input type="hidden" name="key" value="{esc(refund["event_key"])}"><button type="submit">Archive mistaken refund</button></form>' if csrf_token and refund["reference_type"] == "manual" else '') + '</div>'
                f'<strong>{php(refund["amount_cents"])}</strong></li>')
    refund_rows = "".join(render_refund(refund) for refund in refunds) or '<li class="muted">No refunds recorded for this selection.</li>'
    page_link = lambda number: esc(dashboard_url(page_index=number, fragment="#orders"))
    previous = (f'<a href="{page_link(page_number - 1)}">Previous</a>' if page_number > 1 else '<span aria-disabled="true">Previous</span>')
    next_page = (f'<a href="{page_link(page_number + 1)}">Next</a>' if page_number < page_count else '<span aria-disabled="true">Next</span>')
    pagination = (f'<nav class="pagination" aria-label="Orders pages">{previous}<span>Page {page_number} of {page_count} · Showing {first + 1}–{first + len(visible_orders)} of {detail_count}</span>{next_page}</nav>' if detail_count else '')
    search_clear = f'<a href="{esc(dashboard_url(search_text="", fragment="#orders"))}">Clear search</a>' if search else ''
    search_form = (f'<form action="/" method="get" class="order-search">{hidden_shop}{hidden_period}{hidden_group}{hidden_category}'
                   f'<label for="order-search">Search orders</label><input id="order-search" name="q" type="search" '
                   f'placeholder="Product or order ID" value="{esc(search)}" maxlength="200">'
                   f'<button type="submit">Search</button>{search_clear}</form>')
    def trend_chart(group: str) -> str:
        granularity, trend = trend_series(orders, period, refunds, group)
        return (line_chart(trend, granularity) if trend else
                '<p class="empty-state">No purchases or refunds in this period. Choose another year or date range to see a trend.</p>')

    if period.mode == "all":
        trend_content = "".join(
            f'<div data-trend-view="{group}"{" hidden" if group != trend_group else ""}>{trend_chart(group)}</div>'
            for group in ("year", "month")
        )
    else:
        trend_content = trend_chart(trend_group)
    month_current = ' aria-current="page"' if trend_group == "month" else ''
    year_current = ' aria-current="page"' if trend_group == "year" else ''
    trend_tabs = (f'<nav class="trend-tabs" aria-label="Trend grouping">'
                  f'<a href="{esc(dashboard_url(group_name="month", fragment="#trends"))}" data-trend-group="month"{month_current}>By month</a>'
                  f'<a href="{esc(dashboard_url(group_name="year", fragment="#trends"))}" data-trend-group="year"{year_current}>By year</a></nav>') if period.mode == "all" else ''
    trend_card = f'<section class="card trend-card" id="trends"><div class="section-heading"><h2>Spending trend</h2><div class="trend-heading-actions"><span class="section-meta">{esc(period.label)}</span>{trend_tabs}</div></div>{trend_content}</section>'
    shop_card = ""
    if shop == "all":
        shop_totals = {name: sum(order["paid_cents"] or 0 for order in orders if order["platform"] == name)
                       - sum(refund["amount_cents"] for refund in refunds if refund["platform"] == name)
                       for name in ("Shopee", "TikTok Shop", "Other")}
        shop_maximum = max(abs(amount) for amount in shop_totals.values()) or 1
        shop_rows = "".join(
            f'<div class="shop-breakdown-row"><div><span>{name}</span><strong>{php(amount)}</strong></div><div class="track"><div class="fill{" negative" if amount < 0 else ""}" style="width:{abs(amount) / shop_maximum * 100:.1f}%"></div></div></div>'
            for name, amount in shop_totals.items()
        ) if orders or refunds else '<p class="empty-state">No shop spending in this period.</p>'
        shop_card = f'<section class="card shop-card"><div class="section-heading"><h2>By shop</h2></div>{shop_rows}</section>'
    insights_class = "insights-grid" if shop_card else "insights-grid single"
    orders_by_id = all_orders_by_id
    def issue_hint(issue: dict) -> str:
        if issue["code"] != "order_mismatch":
            return ""
        order = orders_by_id.get(issue["order_id"])
        if not order or order["platform"] != "Shopee" or order["coins_cents"] not in (None, 0):
            return ""
        item_sum = sum(item["line_cents"] or 0 for item in order["items"])
        expected = item_sum + (order["shipping_cents"] or 0) + (order["protection_cents"] or 0) - (order["discount_cents"] or 0)
        if expected > (order["paid_cents"] or 0):
            return "<br>Shopee coins were not recorded for this order."
        return "<br>Paid exceeds the calculated amount; Shopee coins cannot explain this difference."

    def issue_row(issue: dict) -> str:
        edit_link = (f' <a href="/?q={esc(issue["order_id"])}#orders">Find order</a>'
                     if issue["order_id"] in orders_by_id else "")
        source_link = (f' <a href="https://mail.google.com/mail/u/0/#all/{esc(issue["gmail_id"])}" target="_blank" rel="noopener noreferrer">Open source email</a>'
                       if not issue["gmail_id"].startswith("manual-") else "")
        dismiss = (f'<form method="post" action="/action" class="inline-action">{csrf_field}'
                   f'<input type="hidden" name="op" value="dismiss"><input type="hidden" name="gmail_id" value="{esc(issue["gmail_id"])}">'
                   f'<input type="hidden" name="order_id" value="{esc(issue["order_id"])}">'
                   f'<input type="hidden" name="code" value="{esc(issue["code"])}"><button type="submit">Mark checked</button></form>') if csrf_token else ""
        kind = "order" if issue["code"] == "manual_conflict" else "refund" if issue["code"] == "manual_refund_conflict" else ""
        conflicts = [conflict for conflict in data.get("conflicts", []) if conflict["gmail_id"] == issue["gmail_id"]
                     and conflict["kind"] == kind and (kind == "refund" or conflict["target_key"] == issue["order_id"])] if kind else []
        resolve = "".join(f'<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="resolve_conflict"><input type="hidden" name="gmail_id" value="{esc(conflict["gmail_id"])}"><input type="hidden" name="key" value="{esc(conflict["target_key"])}"><input type="hidden" name="kind" value="{kind}"><input type="hidden" name="field" value="{esc(conflict["field"])}"><button type="submit">Use email {esc(conflict["field"].replace("_", " "))}: {esc(json.loads(conflict["incoming_json"]))}</button></form>' for conflict in conflicts) if csrf_token else ""
        return (f'<li><strong>{esc(issue["code"].replace("_", " "))}</strong> · {esc(issue["order_id"] or issue["gmail_id"])}<br>'
                f'{esc(issue["detail"])}{issue_hint(issue)}{edit_link}{source_link}{resolve}{dismiss}</li>')
    issue_rows = "".join(issue_row(issue) for issue in data["issues"])
    def failure_hint(error: str) -> str:
        if "order ID" in error or "order IDs" in error or "extracted ID" in error:
            return "If it fails again, check the Order ID in this email."
        return "If it fails again, use the error above and open the email to investigate."

    def failure_row(x: dict) -> str:
        dismiss = (f'<form method="post" action="/action" class="inline-action">{csrf_field}'
                   f'<input type="hidden" name="op" value="dismiss"><input type="hidden" name="gmail_id" value="{esc(x["gmail_id"])}">'
                   '<button type="submit">Mark checked</button></form>') if csrf_token else ""
        return (
        f'<li><strong>{esc(x["subject"])}</strong> · '
        f'{datetime.fromtimestamp(x["received_ms"] / 1000, timezone.utc):%Y-%m-%d %H:%M} UTC'
        f'<br>Gmail ID: <code>{esc(x["gmail_id"])}</code> · '
        f'<a href="https://mail.google.com/mail/u/0/#all/{esc(x["gmail_id"])}" target="_blank" rel="noopener noreferrer">Open email</a>'
        f'<br>{esc(x["error"])}'
        f'<br><span class="muted">Run <code>python -m tracker sync</code> to retry. {esc(failure_hint(x["error"]))}</span>{dismiss}'
        + (f'<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="ignore_email"><input type="hidden" name="gmail_id" value="{esc(x["gmail_id"])}"><button type="submit">Ignore email</button></form>' if csrf_token else '') + '</li>')
    failures = "".join(failure_row(x) for x in data["messages"] if x["state"] == "error")
    def duplicate_row(refund: dict) -> str:
        actions = "".join(f'<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="resolve_duplicate"><input type="hidden" name="event_key" value="{esc(refund["event_key"])}"><input type="hidden" name="decision" value="{decision}"><button type="submit">{label}</button></form>' for decision, label in (("distinct", "Count as separate"), ("ignore", "Ignore duplicate"))) if csrf_token else ""
        return f'<li><strong>Possible duplicate refund</strong> · {esc(refund["order_id"] or refund["reference_id"])}<br>{php(refund["amount_cents"])} held from totals; similar to {esc(refund["duplicate_of"] or "another refund")}<br>Source Gmail ID: <code>{esc(refund["source_gmail_id"])}</code>{actions}</li>'
    pending_rows = "".join(duplicate_row(refund) for refund in data.get("pending_refunds", []))
    review_count = len(data["issues"]) + len(data["messages"]) + len(data.get("pending_refunds", []))
    review = (f'<section class="card review-card" id="review"><div class="section-heading"><h2>Needs review across all shops</h2><span class="review-count">{review_count} items</span></div><ul>{pending_rows}{issue_rows}{failures}</ul></section>' if review_count else '')
    review_nav = '<a href="#review">Needs review</a>' if review else ''
    sync_run = data.get("sync_run")
    sync_time = (sync_run["completed_ms"] or sync_run["started_ms"]) if sync_run else data.get("last_sync_ms")
    sync_label = (datetime.fromtimestamp(sync_time / 1000, MANILA_TZ).strftime("%b %d, %Y %I:%M %p")
                  if sync_time else "Never synced")
    if sync_run:
        sync_detail = (f'{sync_run["processed_count"]} processed, {sync_run["failed_count"]} need review'
                       if sync_run["state"] == "completed" else
                       f'failed: {esc(sync_run["error"] or "Unknown error")}' if sync_run["state"] == "failed" else "running or interrupted")
        sync_status = f'<p class="sync-status">Last sync: {sync_label} · {sync_detail}</p>'
    else:
        sync_status = f'<p class="sync-status">Last sync: {sync_label} · {len(data["messages"])} failed email(s)</p>'
    notice_html = f'<p class="filter-error" role="status">{esc(notice)}</p>' if notice else ""
    order_choices = "".join(f'<option value="{esc(order["order_id"])}">{esc(order.get("merchant") or order["platform"])}</option>'
                            for order in data["orders"])
    purchase_values = form_values if form_values.get("op") == "purchase" else {}
    manual_refund_values = form_values if form_values.get("op") == "add_refund" else {}
    def purchase_item(position: int) -> str:
        name = esc(purchase_values.get(f"item_name_{position}", ""))
        amount = esc(purchase_values.get(f"item_amount_{position}", ""))
        quantity = esc(purchase_values.get(f"item_quantity_{position}", "1"))
        selected = purchase_values.get(f"item_category_{position}", "Other")
        return (f'<div class="basket-item" data-basket-item><strong>Item {position + 1}</strong>'
                f'<label>Name<input name="item_name_{position}" maxlength="300" value="{name}"{" required" if position == 0 else ""}></label>'
                f'<label>Quantity<input name="item_quantity_{position}" type="number" min="1" step="1" value="{quantity}"></label>'
                f'<label>Item total (₱)<input name="item_amount_{position}" type="number" min="0.01" step="0.01" value="{amount}"{" required" if position == 0 else ""}></label>'
                f'<label>Category<select name="item_category_{position}">{category_options(selected)}</select></label></div>')
    item_positions = sorted({int(key.removeprefix("item_name_")) for key in purchase_values if key.startswith("item_name_") and key.removeprefix("item_name_").isdigit()})
    item_count = max(1, max(item_positions, default=0) + 1)
    add_section = (f'<section class="card add-card" id="add"><h2>Add a record</h2>'
                   f'<div class="add-forms"><form method="post" action="/action" id="purchase-form">{csrf_field}'
                   '<input type="hidden" name="op" value="purchase"><h3>Other shop purchase</h3>'
                   f'<label>Merchant<input name="merchant" maxlength="120" value="{esc(purchase_values.get("merchant", ""))}" required></label>'
                   f'<label>Date<input name="date" type="date" value="{esc(purchase_values.get("date", datetime.now(MANILA_TZ).date().isoformat()))}" required></label>'
                   f'<label>Paid (₱)<input name="amount" type="number" min="0.01" step="0.01" value="{esc(purchase_values.get("amount", ""))}" required></label>'
                   f'<div class="basket-items" data-basket-items>{"".join(purchase_item(i) for i in range(item_count))}</div>'
                   '<button type="button" data-add-item>Add another item</button>'
                   '<button type="submit">Add purchase</button></form>'
                   f'<form method="post" action="/action">{csrf_field}'
                   '<input type="hidden" name="op" value="add_refund"><h3>Manual refund</h3>'
                   f'<label>Order ID<input name="order_id" list="tracked-orders" value="{esc(manual_refund_values.get("order_id", ""))}" data-refund-order required></label>'
                   f'<label>Date<input name="date" type="date" value="{esc(manual_refund_values.get("date", datetime.now(MANILA_TZ).date().isoformat()))}" required></label>'
                   f'<label>Refund (₱)<input name="amount" type="number" min="0.01" step="0.01" value="{esc(manual_refund_values.get("amount", ""))}" required></label>'
                   f'<div class="refund-item-fields" data-refund-items data-existing="{esc(json.dumps({key.removeprefix("refund_item_"): value for key, value in manual_refund_values.items() if key.startswith("refund_item_")}))}"></div>'
                   '<button type="submit">Add refund</button></form></div>'
                   f'<datalist id="tracked-orders">{order_choices}</datalist></section>') if csrf_token else ""
    history_rows = "".join(f'<li>{esc(change["kind"].replace(":", " "))} · {esc(change["target_key"])} <form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="undo"><input type="hidden" name="change_id" value="{change["id"]}"><button type="submit">Undo</button></form></li>' for change in data.get("changes", []))
    archived_rows = "".join(f'<li>{esc(row.get("merchant") or row.get("reference_id") or row.get("order_id"))}<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="restore"><input type="hidden" name="kind" value="{kind}"><input type="hidden" name="key" value="{esc(row.get("order_id") or row.get("event_key"))}"><button type="submit">Restore</button></form></li>' for kind, rows in (("order", data.get("archived_orders", [])), ("refund", data.get("archived_refunds", []))) for row in rows)
    history = f'<section class="card history-card" id="history"><h2>Corrections and archived records</h2><ul>{history_rows}{archived_rows or ""}</ul></section>' if csrf_token and (history_rows or archived_rows) else ""
    title = "Shop spending" if shop == "all" else f"{shops[shop]} spending"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'">
<title>{title}</title><style>{DASHBOARD_CSS}</style></head><body><a class="skip-link" href="#main">Skip to content</a><div class="app-shell">
<aside class="sidebar" aria-label="Dashboard navigation"><div class="brand"><span class="brand-mark" aria-hidden="true">S</span><span>Spendwise</span></div><nav class="side-nav" aria-label="Sections"><a class="current" href="#overview">Overview</a><a href="#orders">Orders</a><a href="#refunds">Refunds</a>{review_nav}{'<a href="#add">Add record</a>' if csrf_token else ''}</nav><div class="sidebar-note"><span class="status-dot"></span>Local dashboard</div></aside>
<main id="main"><header class="page-header"><h1>{title}</h1></header>{sync_status}{notice_html}
<nav class="shop-filter" id="shop-filter" aria-label="Filter dashboard by shop"><span class="shop-filter-label">SHOP</span><div class="shop-tabs">{shop_tabs}</div></nav>
{filter_controls}{review}<div class="grid" id="overview"><section class="card hero-card"><div class="hero-top"><div class="eyebrow">Net spending</div></div><div class="total">{php(total)}</div><p class="hero-caption">{esc(period.label)}</p><div class="hero-rule"></div><div class="hero-bottom"><span>ORDERS TRACKED</span><strong>{order_count}</strong></div></section><section class="card category-card"><div class="section-heading"><h2>Purchase spending by category</h2><div class="category-heading-actions"><span class="section-meta">{len(categories)} categories</span>{category_heading_clear}</div></div>{category_content}{net_category_content}</section></div>
<div class="metric-strip"><div class="metric-card"><span>Purchases paid</span><strong>{php(paid_total)}</strong></div><div class="metric-card"><span>Refunds recorded</span><strong>{php(refund_total)}</strong></div></div>
<div class="{insights_class}">{trend_card}{shop_card}</div>
<section class="card orders-card" id="orders"><div class="section-heading"><div><h2>{esc(category) + " purchases" if category else "Orders"}</h2>{f'<p class="insight-note">Category paid: {php(category_total)} · Matching orders: {detail_count}</p>' if category else ''}</div><span class="section-meta">{detail_count} {"matching" if search else "total"}</span></div>{category_clear}{search_form}<div class="tablewrap"><table><thead><tr><th>Date</th><th>Order & items</th><th>Status</th><th class="number">{"Category paid" if category else "Paid"}</th></tr></thead><tbody>{order_rows}</tbody></table></div>{pagination}</section>
<section class="card" id="refunds"><div class="refund-head"><h2>Refunds</h2><strong>{php(refund_total)}</strong></div><ul class="refund-list">{refund_rows}</ul></section>
{add_section}{history}
</main></div><script src="/dashboard.js" defer></script></body></html>'''


def serve(db, port: int):
    csrf_token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path not in ("/", "/health", "/dashboard.js", "/order-items"):
                self.send_error(404)
                return
            if url.path == "/dashboard.js":
                body = DASHBOARD_JS.encode()
                self.respond(200, body, "text/javascript; charset=utf-8")
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
            body = (json.dumps({"ok": True}) if url.path == "/health" else page(dashboard_data(db), page_number, shop, year, start, end, category, month, trend_group, search, csrf_token)).encode()
            self.respond(200, body, "application/json" if url.path == "/health" else "text/html; charset=utf-8")

        def do_POST(self):
            if self.path != "/action" or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                self.send_error(404)
                return
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
                if op == "purchase":
                    positions = sorted({int(key.removeprefix("item_name_")) for key in fields
                                        if key.startswith("item_name_") and key.removeprefix("item_name_").isdigit()})
                    items = [{"name": fields.get(f"item_name_{position}", ""),
                              "quantity": fields.get(f"item_quantity_{position}", "1"),
                              "amount": fields.get(f"item_amount_{position}", ""),
                              "category": fields.get(f"item_category_{position}", "Other")} for position in positions]
                    add_manual_purchase(db, fields.get("merchant", ""), fields.get("date", ""),
                                        fields.get("amount", ""), items=items)
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
                self.respond(400, page(dashboard_data(db), csrf_token=csrf_token, notice=str(exc), form_values=fields).encode(), "text/html; charset=utf-8")
                return
            self.send_response(303)
            self.send_header("Location", "/#" + ("add" if op in ("purchase", "add_refund") else "refunds" if op == "refund" else "review" if op in ("dismiss", "ignore_email", "resolve_duplicate", "resolve_conflict") else "history" if op in ("undo", "restore") else "orders"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Dashboard: http://127.0.0.1:{port}")
        server.serve_forever()


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
        serve(db, args.port)


if __name__ == "__main__":
    main()
