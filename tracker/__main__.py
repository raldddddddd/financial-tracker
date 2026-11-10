from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .core import connect, dashboard_data, gmail_service, load_env, php, seed_demo, spending_summary, sync
from .insights import available_years, filter_period, resolve_period, trend_series


ORDERS_PER_PAGE = 20
DASHBOARD_CSS = Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8")


def page(data: dict, page_number: int = 1, shop: str = "all", year: str = "", start: str = "", end: str = "") -> str:
    esc = lambda value: html.escape(str(value))
    def adjustments(order: dict) -> str:
        details = f'Shipping {php(order["shipping_cents"])} · discounts {php(order["discount_cents"])}'
        if order["platform"] == "Shopee" and order["coins_cents"]:
            source = " (inferred)" if order["coins_inferred"] else ""
            details += f' · Shopee coins{source} {php(order["coins_cents"])}'
        if order["platform"] == "TikTok Shop" and order["handling_cents"]:
            details += f' · handling fee {php(order["handling_cents"])}'
        return details

    def display_order_id(order: dict) -> str:
        return order["order_id"].split(":", 1)[1] if order["platform"] == "TikTok Shop" else order["order_id"]

    shops = {"all": "All shops", "shopee": "Shopee", "tiktok": "TikTok Shop"}
    shop = shop if shop in shops else "all"
    period = resolve_period(data["orders"], year, start, end)
    shop_orders = data["orders"] if shop == "all" else [order for order in data["orders"] if order["platform"] == shops[shop]]
    orders = filter_period(shop_orders, period)
    total, categories = (data["total_cents"], data["categories"]) if shop == "all" and period.mode == "all" else spending_summary(orders)
    order_count = len(orders)
    page_count = max(1, (order_count + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
    page_number = min(max(1, page_number), page_count)
    first = (page_number - 1) * ORDERS_PER_PAGE
    visible_orders = orders[first:first + ORDERS_PER_PAGE]
    def dashboard_url(shop_key: str = shop, page_index: int | None = None, fragment: str = "") -> str:
        params = []
        if shop_key != "all":
            params.append(("shop", shop_key))
        if period.mode == "year":
            params.append(("year", period.year))
        elif period.mode == "custom":
            params.extend((("start", period.start_value), ("end", period.end_value)))
        if page_index is not None:
            params.append(("page", str(page_index)))
        return "/" + ("?" + urlencode(params) if params else "") + fragment

    def shop_tab(key: str, label: str) -> str:
        href = dashboard_url(key, fragment="#shop-filter")
        active_class = " active" if key == shop else ""
        current = ' aria-current="page"' if key == shop else ""
        return f'<a href="{href}" class="shop-tab{active_class}"{current}>{label}</a>'

    shop_tabs = "".join(shop_tab(key, label) for key, label in shops.items())
    hidden_shop = f'<input type="hidden" name="shop" value="{shop}">' if shop != "all" else ""
    year_options = '<option value="">All time</option>' + "".join(
        f'<option value="{choice}"{" selected" if period.year == str(choice) else ""}>{choice}</option>'
        for choice in available_years(data["orders"])
    )
    period_error = f'<p class="filter-error" role="alert">{esc(period.error)}</p>' if period.error else ""
    all_time_url = "/" + (f"?shop={shop}" if shop != "all" else "") + "#shop-filter"
    clear_dates = f'<a class="clear-dates" href="{all_time_url}">Clear dates</a>' if period.mode != "all" else ""
    filter_controls = f'''<div class="period-filter" aria-label="Spending period">
<form action="/" method="get" class="year-form"><label for="year-select">View year</label>{hidden_shop}<select id="year-select" name="year">{year_options}</select><button type="submit">View</button></form>
<span class="filter-divider" aria-hidden="true"></span>
<form action="/" method="get" class="range-form"><span class="range-label">Custom range</span>{hidden_shop}<label for="start-date">From</label><input id="start-date" name="start" type="date" value="{esc(period.start_value)}"><label for="end-date">To</label><input id="end-date" name="end" type="date" value="{esc(period.end_value)}"><button type="submit">Apply</button></form>{clear_dates}</div>{period_error}'''
    category_rows = "".join(
        f'<div class="barrow"><span>{esc(category)}</span><div class="track"><div class="fill" style="width:{(amount / total * 100) if total else 0:.1f}%"></div></div><strong>{php(amount)}</strong></div>'
        for category, amount in sorted(categories.items(), key=lambda x: -x[1])
    ) or '<p class="muted">No spending yet.</p>'
    order_rows = "".join(
        '<tr>'
        f'<td>{esc(order["date"])}</td><td><span class="shop-tag {"shop-tiktok" if order["platform"] == "TikTok Shop" else "shop-shopee"}">{esc(order["platform"])}</span><strong>{esc(display_order_id(order))}</strong><small>{esc(", ".join(item["name"] + " ×" + str(item["quantity"]) + " · " + item["category"] for item in order["items"]))}</small><small>{adjustments(order)}</small></td>'
        f'<td><span class="pill">{esc(order["status"].replace("_", " "))}</span></td>'
        f'<td class="number strong">{php(order["effective_cents"])}</td>'
        '</tr>' for order in visible_orders
    ) or ('<tr><td colspan="4" class="muted">No orders in this period. Try another year or date range.</td></tr>' if period.mode != "all" else '<tr><td colspan="4" class="muted">No orders yet. Run the sync command or load the demo fixture.</td></tr>')
    refunded_orders = [order for order in orders if order["refund_cents"] > 0]
    refund_total = sum(order["refund_cents"] for order in refunded_orders)
    refund_rows = "".join(
        f'<li><span><span class="shop-tag {"shop-tiktok" if order["platform"] == "TikTok Shop" else "shop-shopee"}">{esc(order["platform"])}</span>{esc(display_order_id(order))}</span><strong>{php(order["refund_cents"])}</strong></li>'
        for order in refunded_orders
    ) or '<li class="muted">No refunds recorded for this selection.</li>'
    unlinked_refunds = data.get("refund_requests", []) if shop != "tiktok" else []
    unlinked_refunds = [request for request in unlinked_refunds if not request["order_id"]]
    unlinked_total = sum(request["refund_cents"] for request in unlinked_refunds)
    unlinked_rows = "".join(
        f'<li><span><span class="shop-tag shop-shopee">Shopee</span>Request {esc(request["request_id"])}</span><strong>{php(request["refund_cents"])}</strong></li>'
        for request in unlinked_refunds
    )
    unlinked_section = (f'<div class="refund-head"><h3>Approved refunds awaiting order link</h3><strong>{php(unlinked_total)}</strong></div>'
                        '<p class="muted">Across all dates: these request IDs do not identify a purchase order, so they are excluded from the selected period and net spending.</p>'
                        f'<ul class="refund-list">{unlinked_rows}</ul>') if unlinked_rows else ''
    page_link = lambda number: dashboard_url(page_index=number, fragment="#orders")
    previous = (f'<a href="{page_link(page_number - 1)}">Previous</a>' if page_number > 1 else '<span aria-disabled="true">Previous</span>')
    next_page = (f'<a href="{page_link(page_number + 1)}">Next</a>' if page_number < page_count else '<span aria-disabled="true">Next</span>')
    pagination = (f'<nav class="pagination" aria-label="Orders pages">{previous}<span>Page {page_number} of {page_count} · Showing {first + 1}–{first + len(visible_orders)} of {order_count}</span>{next_page}</nav>' if order_count else '')
    granularity, trend = trend_series(orders, period)
    if trend:
        maximum = max(amount for _, amount in trend) or 1
        trend_rows = "".join(
            f'<div class="trend-row"><span>{esc(label)}</span><div class="trend-track"><div class="trend-fill" style="width:{amount / maximum * 100:.1f}%"></div></div><strong>{php(amount)}</strong></div>'
            for label, amount in trend
        )
        trend_table = "".join(f'<tr><th scope="row">{esc(label)}</th><td>{php(amount)}</td></tr>' for label, amount in trend)
        trend_content = f'<div class="trend-chart" aria-hidden="true">{trend_rows}</div><table class="sr-only"><caption>Net spending by {granularity}</caption><thead><tr><th>Period</th><th>Net spending</th></tr></thead><tbody>{trend_table}</tbody></table>'
    else:
        trend_content = '<p class="empty-state">No orders in this period. Choose another year or date range to see a trend.</p>'
    trend_card = f'<section class="card trend-card" id="trends"><div class="section-heading"><div><div class="eyebrow">Over time</div><h2>Spending trend</h2></div><span class="section-meta">{esc(period.label)}</span></div><p class="insight-note">Net spending by purchase period. Later refunds adjust the original order period.</p>{trend_content}</section>'
    shop_card = ""
    if shop == "all":
        shop_totals = {name: sum(order["effective_cents"] for order in orders if order["platform"] == name)
                       for name in ("Shopee", "TikTok Shop")}
        shop_maximum = max(shop_totals.values()) or 1
        shop_rows = "".join(
            f'<div class="shop-breakdown-row"><div><span>{name}</span><strong>{php(amount)}</strong></div><div class="track"><div class="fill" style="width:{amount / shop_maximum * 100:.1f}%"></div></div></div>'
            for name, amount in shop_totals.items()
        ) if orders else '<p class="empty-state">No shop spending in this period.</p>'
        shop_card = f'<section class="card shop-card"><div class="section-heading"><div><div class="eyebrow">Across platforms</div><h2>By shop</h2></div></div>{shop_rows}</section>'
    insights_class = "insights-grid" if shop_card else "insights-grid single"
    orders_by_id = {order["order_id"]: order for order in data["orders"]}
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

    issue_rows = "".join(
        f'<li><strong>{esc(x["code"].replace("_", " "))}</strong> · {esc(x["order_id"] or x["gmail_id"])}<br>'
        f'{esc(x["detail"])}{issue_hint(x)}</li>'
        for x in data["issues"]
    )
    unlinked_issue_rows = "".join(
        f'<li><strong>Approved refund needs order link</strong> · request {esc(request["request_id"])}<br>'
        f'{php(request["refund_cents"])} is awaiting the original Shopee purchase order ID.</li>'
        for request in unlinked_refunds
    )
    def failure_hint(error: str) -> str:
        if "order ID" in error or "order IDs" in error or "extracted ID" in error:
            return "If it fails again, check the Order ID in this email."
        return "If it fails again, use the error above and open the email to investigate."

    failures = "".join(
        f'<li><strong>{esc(x["subject"])}</strong> · '
        f'{datetime.fromtimestamp(x["received_ms"] / 1000, timezone.utc):%Y-%m-%d %H:%M} UTC'
        f'<br>Gmail ID: <code>{esc(x["gmail_id"])}</code> · '
        f'<a href="https://mail.google.com/mail/u/0/#all/{esc(x["gmail_id"])}" target="_blank" rel="noopener noreferrer">Open email</a>'
        f'<br>{esc(x["error"])}'
        f'<br><span class="muted">Run <code>python -m tracker sync</code> to retry. {esc(failure_hint(x["error"]))}</span></li>'
        for x in data["messages"] if x["state"] == "error"
    )
    review = (f'<section class="card review-card" id="review"><div class="section-heading"><div><div class="eyebrow">Attention needed</div><h2>Needs review across all shops</h2></div><span class="review-count">{len(data["issues"]) + len(unlinked_refunds) + len([x for x in data["messages"] if x["state"] == "error"])} items</span></div><ul>{issue_rows}{unlinked_issue_rows}{failures}</ul></section>' if issue_rows or unlinked_issue_rows or failures else '')
    review_nav = '<a href="#review">Needs review</a>' if review else ''
    title = "Shop spending" if shop == "all" else f"{shops[shop]} spending"
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'">
<title>{title}</title><style>{DASHBOARD_CSS}</style></head><body><a class="skip-link" href="#main">Skip to content</a><div class="app-shell">
<aside class="sidebar" aria-label="Dashboard navigation"><div class="brand"><span class="brand-mark" aria-hidden="true">S</span><span>Spendwise<small>PERSONAL TRACKER</small></span></div><div class="sidebar-label">WORKSPACE</div><nav class="side-nav" aria-label="Sections"><a class="current" href="#overview">Overview</a><a href="#orders">Orders</a><a href="#refunds">Refunds</a>{review_nav}</nav><div class="sidebar-note"><span class="status-dot"></span>Local dashboard<small>Purchase data stays on this computer</small></div></aside>
<main id="main"><header class="page-header"><div><div class="eyebrow">YOUR SPENDING AT A GLANCE</div><h1>{title}</h1><p class="muted">A clear view of purchases, refunds, and what needs attention.</p></div><span class="header-badge">PERSONAL DASHBOARD</span></header>
<nav class="shop-filter" id="shop-filter" aria-label="Filter dashboard by shop"><span class="shop-filter-label">SHOP</span><div class="shop-tabs">{shop_tabs}</div></nav>
{filter_controls}{review}<div class="grid" id="overview"><section class="card hero-card"><div class="hero-top"><div class="eyebrow">Net spending</div><span class="hero-icon" aria-hidden="true">↗</span></div><div class="total">{php(total)}</div><p class="hero-caption">After refunds and cancellations · {esc(period.label)}</p><div class="hero-rule"></div><div class="hero-bottom"><span>ORDERS TRACKED</span><strong>{order_count}</strong></div></section><section class="card category-card"><div class="section-heading"><div><div class="eyebrow">Where it went</div><h2>By category</h2></div><span class="section-meta">{len(categories)} categories</span></div>{category_rows}</section></div>
<div class="metric-strip"><div class="metric-card"><span>Orders tracked</span><strong>{order_count}</strong><small>Including cancelled and fully refunded</small></div><div class="metric-card"><span>Refunds recorded</span><strong>{php(refund_total)}</strong><small>Already reflected in net spending</small></div></div>
<div class="{insights_class}">{trend_card}{shop_card}</div>
<section class="card orders-card" id="orders"><div class="section-heading"><div><div class="eyebrow">Purchase history</div><h2>Orders</h2></div><span class="section-meta">{order_count} total</span></div><div class="tablewrap"><table><thead><tr><th>Date</th><th>Order & items</th><th>Status</th><th class="number">Total</th></tr></thead><tbody>{order_rows}</tbody></table></div>{pagination}</section>
<section class="card" id="refunds"><div class="refund-head"><h2>Refunds</h2><strong>{php(refund_total)}</strong></div><p class="muted">These refunds are already subtracted from order totals.</p><ul class="refund-list">{refund_rows}</ul>{unlinked_section}</section>
<footer>Order totals show spending after refunds. Categories allocate each order's remaining amount across items by their listed line prices. Data stays in local SQLite; Gemini receives redacted receipt text during sync.</footer></main></div></body></html>'''


def serve(db, port: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path not in ("/", "/health"):
                self.send_error(404)
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
            body = (json.dumps({"ok": True}) if url.path == "/health" else page(dashboard_data(db), page_number, shop, year, start, end)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if url.path == "/health" else "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Dashboard: http://127.0.0.1:{port}")
        server.serve_forever()


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Local shop expense tracker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth", help="Authorize the one Gmail account")
    commands.add_parser("sync", help="Fetch and parse Shopee and TikTok Shop confirmations")
    commands.add_parser("demo", help="Load synthetic sample orders")
    server = commands.add_parser("serve", help="Show the local dashboard")
    server.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "auth":
        gmail_service()
        print("Gmail authorization ready.")
        return
    db = connect()
    if args.command == "sync":
        good, bad = sync(db)
        print(f"Processed {good} message(s); {bad} need review.")
    elif args.command == "demo":
        seed_demo(db)
        print("Loaded two synthetic orders. Run: python -m tracker serve")
    elif args.command == "serve":
        serve(db, args.port)


if __name__ == "__main__":
    main()
