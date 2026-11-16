from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .charts import category_pie, line_chart
from .core import CATEGORIES, MANILA_TZ, php
from .insights import SHOPS, available_years, select_dashboard, trend_series

DASHBOARD_CSS = Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8")
DASHBOARD_JS = Path(__file__).with_name("dashboard.js").read_text(encoding="utf-8")


def page(data: dict, page_number: int = 1, shop: str = "all", year: str = "", start: str = "", end: str = "",
         category: str = "", month: str = "", trend_group: str = "year", search: str = "",
         csrf_token: str = "", notice: str = "", form_values: dict[str, str] | None = None,
         can_sync: bool = False, demo: bool = False) -> str:
    esc = lambda value: html.escape(str(value))
    form_values = form_values or {}
    def adjustments(order: dict) -> str:
        details = []
        if order["shipping_cents"]:
            details.append(f'Shipping {php(order["shipping_cents"])}')
        if order["discount_cents"]:
            details.append(f'discounts {php(order["discount_cents"])}')
        if order["platform"] == "Shopee" and order["coins_cents"]:
            source = " (inferred)" if order["coins_inferred"] else ""
            details.append(f'Shopee coins{source} {php(order["coins_cents"])}')
        if order["platform"] == "TikTok Shop" and order["handling_cents"]:
            details.append(f'handling fee {php(order["handling_cents"])}')
        return " · ".join(details)

    def display_order_id(order: dict) -> str:
        if order["platform"] == "Other":
            return order["merchant"] or order["order_id"]
        return order["order_id"].split(":", 1)[1] if order["platform"] == "TikTok Shop" else order["order_id"]

    selection = select_dashboard(data, page_number, shop, year, start, end, category, month, trend_group, search)
    shops = SHOPS
    shop, category, trend_group, search = selection.shop, selection.category, selection.trend_group, selection.search
    period, orders, refunds = selection.period, selection.orders, selection.refunds
    paid_total, refund_total, total = selection.paid_total, selection.refund_total, selection.total
    categories, net_categories = selection.categories, selection.net_categories
    products, discounts_total = selection.products, selection.discounts_total
    this_month, current_spend, previous_spend = selection.this_month, selection.current_spend, selection.previous_spend
    category_total, detail_count = selection.category_total, selection.detail_count
    page_count, page_number, visible_orders = selection.page_count, selection.page_number, selection.visible_orders
    first = selection.first_index
    shop_totals = selection.shop_totals
    order_count = len(orders)
    budget = data.get("monthly_budget_cents")
    all_orders_by_id = {order["order_id"]: order for order in data["orders"]}
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
    filter_controls = f'''<details class="period-filter"{" open" if period.error else ""}>
<summary>Period: {esc(period.label)}</summary><div class="period-options">
<form action="/#shop-filter" method="get" class="year-form"><label for="year-select">Year</label>{hidden_shop}{hidden_category}{hidden_group}{hidden_search}<select id="year-select" name="year">{year_options}</select><button type="submit">Show year</button></form>
<form action="/#shop-filter" method="get" class="month-form"><label for="month-select">Month</label>{hidden_shop}{hidden_category}{hidden_group}{hidden_search}<input id="month-select" name="month" type="month" value="{esc(period.month)}"><button type="submit">Show month</button></form>
<span class="filter-divider" aria-hidden="true"></span>
<form action="/#shop-filter" method="get" class="range-form"><span class="range-label">Custom range</span>{hidden_shop}{hidden_category}{hidden_group}{hidden_search}<label for="start-date">From</label><input id="start-date" name="start" type="date" value="{esc(period.start_value)}"><label for="end-date">To</label><input id="end-date" name="end" type="date" value="{esc(period.end_value)}"><button type="submit">Apply dates</button></form>{clear_dates}</div></details>{period_error}'''
    if len(categories) == 1:
        name, amount = next(iter(categories.items()))
        category_content = (f'<a class="single-category" href="{esc(dashboard_url(category_name=name, fragment="#orders"))}">'
                            f'<span>{esc(name)}</span><strong>{php(amount)}</strong><span aria-hidden="true">→</span></a>')
    else:
        pie, legend = category_pie(categories, category, lambda name: dashboard_url(category_name=name, fragment="#orders"))
        category_content = (f'<div class="category-visual">{pie}<div class="category-legend">{legend}</div></div>'
                            if pie else '<p class="muted">No paid purchases in this period.</p>')
    net_category_rows = "".join(f'<li><span>{esc(name)}</span><strong>{php(amount)}</strong></li>'
                                for name, amount in sorted(net_categories.items()) if amount)
    net_category_content = (f'<details class="net-category"><summary>Net by category</summary><p>Linked refunds follow their purchase category when clear; mixed-category refunds need item details.</p><ul>{net_category_rows}</ul></details>'
                            if net_category_rows and refunds else "")
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
        detail = adjustments(order)
        detail_html = f'<small>{detail}</small>' if detail else ''
        mobile_status = (f'<small class="order-status-mobile">Status: {esc(order["status"].replace("_", " "))}</small>'
                         if order["status"] != "paid" else '')
        return ('<tr>'
                f'<td>{esc(order["date"])}</td><td><span class="shop-tag {shop_class}">{esc(order["platform"])}</span><strong>{esc(display_order_id(order))}</strong>{merchant}<small>{esc(order_items(order))}</small>{detail_html}{corrected}'
                f'{mobile_status}'
                f'{("<small>Full order paid " + php(order["paid_cents"]) + "</small>") if category else ""}{edit_order(order)}'
                + (f'<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="archive"><input type="hidden" name="kind" value="order"><input type="hidden" name="key" value="{esc(order["order_id"])}"><button type="submit">Archive mistaken entry</button></form>' if csrf_token and order["platform"] == "Other" else '') + '</td>'
                f'<td><span class="pill">{esc(order["status"].replace("_", " "))}</span></td>'
                f'<td class="number strong">{php(order["category_allocations"].get(category, 0) if category else order["paid_cents"])}</td></tr>')
    order_rows = "".join(render_order(order) for order in visible_orders) or ('<tr><td colspan="4" class="muted">No orders match this search in the selected shop, period, and category.</td></tr>' if search else '<tr><td colspan="4" class="muted">No purchases in this category for the selected shop and period.</td></tr>' if category else '<tr><td colspan="4" class="muted">No orders in this period. Try another year or date range.</td></tr>' if period.mode != "all" else '<tr><td colspan="4" class="muted">No purchases yet. Add a purchase manually or sync to import one.</td></tr>')
    def render_refund(refund: dict) -> str:
        link = f'<small>Order {esc(refund["order_id"])}</small>' if refund.get("order_id") else '<small>Order not linked</small>'
        category_names = sorted({allocation["category"] for allocation in refund.get("allocations", [])})
        category_label = "Category: " + ", ".join(category_names) if category_names else "Category unallocated"
        edit_values = form_values if form_values.get("op") == "refund" and form_values.get("event_key") == refund["event_key"] else {}
        existing_amounts = ({key.removeprefix("refund_item_"): value for key, value in edit_values.items() if key.startswith("refund_item_")}
                            if edit_values else {str(a["item_position"]): amount_value(a["amount_cents"]) for a in refund.get("allocations", []) if a["item_position"] >= 0})
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
                f'<small>{esc(category_label)}</small>{edit}'
                + (f'<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="archive"><input type="hidden" name="kind" value="refund"><input type="hidden" name="key" value="{esc(refund["event_key"])}"><button type="submit">Archive mistaken refund</button></form>' if csrf_token and refund["reference_type"] == "manual" else '') + '</div>'
                f'<strong>{php(refund["amount_cents"])}</strong></li>')
    refund_rows = "".join(render_refund(refund) for refund in refunds) or '<li class="muted">No refunds recorded for this selection.</li>'
    page_link = lambda number: esc(dashboard_url(page_index=number, fragment="#orders"))
    previous = (f'<a href="{page_link(page_number - 1)}">Previous</a>' if page_number > 1 else '<span aria-disabled="true">Previous</span>')
    next_page = (f'<a href="{page_link(page_number + 1)}">Next</a>' if page_number < page_count else '<span aria-disabled="true">Next</span>')
    pagination = (f'<nav class="pagination" aria-label="Orders pages">{previous}<span>Page {page_number} of {page_count} · Showing {first + 1}–{first + len(visible_orders)} of {detail_count}</span>{next_page}</nav>' if page_count > 1 else '')
    search_clear = f'<a href="{esc(dashboard_url(search_text="", fragment="#orders"))}">Clear search</a>' if search else ''
    search_form = (f'<form action="/#orders" method="get" class="order-search">{hidden_shop}{hidden_period}{hidden_group}{hidden_category}'
                   f'<label for="order-search">Search orders</label><input id="order-search" name="q" type="search" '
                   f'placeholder="Merchant, product, or order ID" value="{esc(search)}" maxlength="200">'
                   f'<button type="submit">Search</button>{search_clear}</form>')
    def trend_chart(group: str) -> str:
        granularity, trend = trend_series(orders, period, refunds, group)
        if period.mode == "all" and len(trend) == 1:
            label, amount = trend[0]
            return (f'<div class="single-trend"><span>{esc(label)}</span><strong>{php(amount)}</strong></div>'
                    '<p class="single-trend-note">A trend will appear when more periods have spending.</p>')
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
    if shop_totals:
        shop_maximum = max(abs(amount) for amount in shop_totals.values()) or 1
        shop_rows = "".join(
            f'<div class="shop-breakdown-row"><div><span>{name}</span><strong>{php(amount)}</strong></div><div class="track"><div class="fill{" negative" if amount < 0 else ""}" style="width:{abs(amount) / shop_maximum * 100:.1f}%"></div></div></div>'
            for name, amount in shop_totals.items()
        )
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
        failed_suffix = f' · {len(data["messages"])} failed email(s)' if data["messages"] else ''
        sync_status = f'<p class="sync-status">Last sync: {sync_label}{failed_suffix}</p>'
    data_through = (datetime.fromtimestamp(data["last_sync_ms"] / 1000, MANILA_TZ).strftime("%b %d, %Y %I:%M %p")
                    if data.get("last_sync_ms") else "Never synced")
    sync_controls = (f'<form method="post" action="/action" class="sync-form">{csrf_field}'
                     '<input type="hidden" name="op" value="sync"><button type="submit">Sync now</button></form>'
                     '<a href="/">Refresh status</a>') if can_sync else ""
    review_link = f'<a href="#review">{review_count} to review</a>' if review_count else ""
    last_scan = f'<span>Last completed scan: {data_through}</span>' if data.get("last_sync_ms") else ''
    sync_bar = f'<div class="sync-bar">{sync_status}{last_scan}{review_link}{sync_controls}</div>' if not demo else ''
    product_rows = "".join(
        f'<li><div><strong>{esc(name)}</strong><small>{esc(category)} · {quantity} item{"s" if quantity != 1 else ""}</small></div><span>{php(amount)}</span></li>'
        for name, category, amount, quantity in products)
    product_card = (f'<section class="card product-card" id="products"><div class="section-heading"><h2>Top products by spend</h2><span class="section-meta">{esc(period.label)}</span></div>'
                    f'<ol class="product-list">{product_rows}</ol></section>' if product_rows else
                    '<section class="card product-card" id="products"><div class="section-heading"><h2>Top products by spend</h2></div>'
                    '<p class="empty-state">Itemized paid purchases will appear here after a sync or manual entry.</p></section>')
    if budget:
        used_percent = min(100, max(0, round(current_spend / budget * 100)))
        budget_status = (f'<div class="budget-progress{" over" if current_spend > budget else ""}" role="progressbar" aria-label="Monthly budget used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{used_percent}"><span style="width:{used_percent}%"></span></div>'
                         f'<p>{php(abs(budget - current_spend))} {"over budget" if current_spend > budget else "remaining"}</p>')
    else:
        budget_status = ''
    budget_value = f'{budget / 100:.2f}' if budget else ''
    budget_form = (f'<form method="post" action="/action" class="budget-form">{csrf_field}<input type="hidden" name="op" value="budget">'
                   f'<label for="monthly-budget">Monthly limit (₱)</label><input id="monthly-budget" name="amount" type="number" min="0.01" step="0.01" placeholder="No limit set" value="{budget_value}">'
                   f'<button type="submit">Save budget</button></form><small>Clear the field and save to remove the limit.</small>') if csrf_token else ""
    change = current_spend - previous_spend
    compare = (f'{php(abs(change))} more than last month' if change > 0 else
               f'{php(abs(change))} less than last month' if change < 0 else
               'The same as last month')
    comparison = f'<p class="month-compare">{compare}.</p>' if previous_spend else ''
    month_card = (f'<section class="card month-card" id="budget"><div class="section-heading"><h2>Monthly budget</h2><span class="section-meta">All shops · {this_month:%B %Y}</span></div>'
                  f'<div class="month-summary"><div><small>Net spent</small><strong>{php(current_spend)}</strong></div><div><small>Monthly limit</small><strong>{php(budget) if budget else "Not set"}</strong></div></div>{budget_status}'
                  f'{comparison}{budget_form}</section>')
    discounts_metric = (f'<div class="metric-card"><span>Recorded discounts</span><strong>{php(discounts_total)}</strong><small>From available receipts</small></div>'
                        if discounts_total else '')
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
    add_section = (f'<details class="add-card" id="add"{" open" if purchase_values or manual_refund_values else ""}><summary>Add purchase or refund</summary><div class="manual-content"><p class="form-note">For purchases paid online only; exclude cash and cash on delivery.</p>'
                   f'<div class="add-forms"><form method="post" action="/action" id="purchase-form">{csrf_field}'
                   '<input type="hidden" name="op" value="purchase"><h3>Purchase</h3>'
                   f'<label>Merchant<input name="merchant" maxlength="120" value="{esc(purchase_values.get("merchant", ""))}" required></label>'
                   f'<label>Date<input name="date" type="date" value="{esc(purchase_values.get("date", datetime.now(MANILA_TZ).date().isoformat()))}" required></label>'
                   f'<label>Paid (₱)<input name="amount" type="number" min="0.01" step="0.01" value="{esc(purchase_values.get("amount", ""))}" required></label>'
                   f'<div class="basket-items" data-basket-items>{"".join(purchase_item(i) for i in range(item_count))}</div>'
                   '<button type="button" data-add-item>Add another item</button>'
                   '<button type="submit">Add purchase</button></form>'
                   f'<form method="post" action="/action">{csrf_field}'
                   '<input type="hidden" name="op" value="add_refund"><h3>Refund</h3>'
                   f'<label>Order ID<input name="order_id" list="tracked-orders" value="{esc(manual_refund_values.get("order_id", ""))}" data-refund-order required></label>'
                   f'<label>Date<input name="date" type="date" value="{esc(manual_refund_values.get("date", datetime.now(MANILA_TZ).date().isoformat()))}" required></label>'
                   f'<label>Refund (₱)<input name="amount" type="number" min="0.01" step="0.01" value="{esc(manual_refund_values.get("amount", ""))}" required></label>'
                   f'<div class="refund-item-fields" data-refund-items data-existing="{esc(json.dumps({key.removeprefix("refund_item_"): value for key, value in manual_refund_values.items() if key.startswith("refund_item_")}))}"></div>'
                   '<button type="submit">Add refund</button></form></div>'
                   f'<datalist id="tracked-orders">{order_choices}</datalist></div></details>') if csrf_token else ""
    history_rows = "".join(f'<li>{esc(change["kind"].replace(":", " "))} · {esc(change["target_key"])} <form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="undo"><input type="hidden" name="change_id" value="{change["id"]}"><button type="submit">Undo</button></form></li>' for change in data.get("changes", []))
    archived_rows = "".join(f'<li>{esc(row.get("merchant") or row.get("reference_id") or row.get("order_id"))}<form method="post" action="/action" class="inline-action">{csrf_field}<input type="hidden" name="op" value="restore"><input type="hidden" name="kind" value="{kind}"><input type="hidden" name="key" value="{esc(row.get("order_id") or row.get("event_key"))}"><button type="submit">Restore</button></form></li>' for kind, rows in (("order", data.get("archived_orders", [])), ("refund", data.get("archived_refunds", []))) for row in rows)
    history = f'<section class="card history-card" id="history"><h2>Corrections and archived records</h2><ul>{history_rows}{archived_rows or ""}</ul></section>' if csrf_token and (history_rows or archived_rows) else ""
    title = "Spending overview" if shop == "all" else f"{shops[shop]} spending"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'">
<title>{title} · Spendwise</title><style>{DASHBOARD_CSS}</style></head><body><a class="skip-link" href="#main">Skip to content</a><div class="app-shell">
<aside class="sidebar" aria-label="Dashboard navigation"><div class="brand">spendwise<span class="brand-dot">.</span></div><nav class="side-nav" aria-label="Sections"><a href="#overview">Overview</a><a href="#budget">Budget</a><a href="#products">Products</a><a href="#orders">Purchases</a><a href="#refunds">Refunds</a>{review_nav}</nav></aside>
<main id="main"><div class="page-top"><header class="page-header"><h1>{title}</h1>{'<p class="page-context">Sample data</p>' if demo else ''}</header>{add_section}</div>{sync_bar}{notice_html}
<nav class="shop-filter" id="shop-filter" aria-label="Filter dashboard by shop"><div class="shop-tabs">{shop_tabs}</div></nav>
{filter_controls}{review}<div class="grid" id="overview"><section class="card hero-card"><div class="hero-top"><div class="eyebrow">Net spending</div></div><div class="total">{php(total)}</div><p class="hero-caption">{esc(period.label)}</p><div class="hero-rule"></div><div class="hero-bottom"><span>ORDERS TRACKED</span><strong>{order_count}</strong></div></section><section class="card category-card"><div class="section-heading"><h2>Purchase spending by category</h2><div class="category-heading-actions">{category_heading_clear}</div></div>{category_content}{net_category_content}</section></div>
<div class="metric-strip"><div class="metric-card"><span>Purchases paid</span><strong>{php(paid_total)}</strong></div><div class="metric-card"><span>Refunds recorded</span><strong>{php(refund_total)}</strong></div>{discounts_metric}</div>
<div class="{insights_class}">{trend_card}{shop_card}</div>
<div class="detail-grid">{month_card}{product_card}</div>
<section class="card orders-card" id="orders"><div class="section-heading"><div><h2>{esc(category) + " purchases" if category else "Purchases"}</h2>{f'<p class="insight-note">Category paid: {php(category_total)} · Matching orders: {detail_count}</p>' if category else ''}</div><span class="section-meta" aria-live="polite">{detail_count} {"matching" if search else "total"}</span></div>{category_clear}{search_form}<p class="table-hint">Scroll table horizontally for status and amount.</p><div class="tablewrap" tabindex="0" role="region" aria-label="Purchases table"><table><thead><tr><th>Date</th><th>Order & items</th><th>Status</th><th class="number">{"Category paid" if category else "Paid"}</th></tr></thead><tbody>{order_rows}</tbody></table></div>{pagination}</section>
<section class="card" id="refunds"><div class="refund-head"><h2>Refunds</h2><strong>{php(refund_total)}</strong></div><ul class="refund-list">{refund_rows}</ul></section>
{history}
</main></div><script src="/dashboard.js" defer></script></body></html>'''
