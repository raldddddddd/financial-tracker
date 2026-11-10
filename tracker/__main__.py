from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import connect, dashboard_data, gmail_service, load_env, php, seed_demo, sync


def page(data: dict) -> str:
    esc = lambda value: html.escape(str(value))
    total = data["total_cents"]
    category_rows = "".join(
        f'<div class="barrow"><span>{esc(category)}</span><div class="track"><div class="fill" style="width:{(amount / total * 100) if total else 0:.1f}%"></div></div><strong>{php(amount)}</strong></div>'
        for category, amount in sorted(data["categories"].items(), key=lambda x: -x[1])
    ) or '<p class="muted">No spending yet.</p>'
    order_rows = "".join(
        '<tr>'
        f'<td>{esc(order["date"])}</td><td><strong>{esc(order["order_id"])}</strong><small>{esc(", ".join(item["name"] + " ×" + str(item["quantity"]) + " · " + item["category"] for item in order["items"]))}</small><small>Shipping {php(order["shipping_cents"])} · discounts {php(order["discount_cents"])}</small></td>'
        f'<td><span class="pill">{esc(order["status"].replace("_", " "))}</span></td>'
        f'<td class="number">{php(order["paid_cents"])}</td><td class="number">{php(order["refund_cents"])}</td><td class="number strong">{php(order["effective_cents"])}</td>'
        '</tr>' for order in data["orders"]
    ) or '<tr><td colspan="6" class="muted">No orders yet. Run the sync command or load the demo fixture.</td></tr>'
    issue_rows = "".join(f'<li><strong>{esc(x["code"].replace("_", " "))}</strong> · {esc(x["order_id"] or x["gmail_id"])}<br>{esc(x["detail"])}</li>' for x in data["issues"])
    failures = "".join(f'<li>{esc(x["subject"])}: {esc(x["error"])}</li>' for x in data["messages"] if x["state"] == "error")
    review = (f'<section class="card"><h2>Needs review</h2><ul>{issue_rows}{failures}</ul></section>' if issue_rows or failures else '')
    payments = "".join(f'<tr><td>{esc(x["subject"])}</td><td class="number">{php(x["combined_cents"])}</td><td class="number">{php(x["order_sum_cents"])}</td><td class="number">{php(x["discrepancy_cents"])}</td></tr>' for x in data["messages"] if x["combined_cents"] is not None)
    payment_card = (f'<section class="card"><h2>Combined payments</h2><div class="tablewrap"><table><thead><tr><th>Email</th><th class="number">Combined</th><th class="number">Orders</th><th class="number">Difference</th></tr></thead><tbody>{payments}</tbody></table></div></section>' if payments else '')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Shopee spending</title><style>
:root{{--ink:#162b29;--muted:#64746f;--paper:#f5f7f2;--line:#dce5dc;--accent:#197c60;--pale:#d8ede2}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.45 system-ui,-apple-system,sans-serif}} main{{max-width:1120px;margin:auto;padding:42px 24px 80px}}
header{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:30px}} h1{{font-size:32px;letter-spacing:-.035em;margin:0}} h2{{font-size:18px;margin:0 0 20px}} p{{margin:8px 0}} .muted,small{{color:var(--muted)}} .eyebrow{{color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.13em;text-transform:uppercase}}
.grid{{display:grid;grid-template-columns:1fr 1.5fr;gap:18px;margin-bottom:18px}} .card{{background:#fff;border:1px solid var(--line);border-radius:18px;padding:25px;box-shadow:0 8px 24px #15372b08;margin-bottom:18px}} .total{{font-size:42px;font-weight:800;letter-spacing:-.05em;margin:14px 0 3px}} .barrow{{display:grid;grid-template-columns:120px 1fr 95px;align-items:center;gap:12px;font-size:13px;margin:13px 0}} .barrow strong{{text-align:right}} .track{{height:10px;border-radius:8px;background:#edf2ec;overflow:hidden}} .fill{{height:100%;background:var(--accent);border-radius:8px}}
.tablewrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;min-width:700px}} th{{text-align:left;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.09em;border-bottom:1px solid var(--line);padding:12px 8px}} td{{padding:17px 8px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}} td small{{display:block;max-width:340px;margin-top:4px}} .number{{text-align:right;white-space:nowrap}} .strong{{font-weight:800}} .pill{{display:inline-block;background:var(--pale);color:#176548;padding:4px 9px;border-radius:99px;font-size:12px;text-transform:capitalize}} ul{{padding-left:22px}} li{{margin:12px 0}} footer{{margin-top:18px;font-size:13px;color:var(--muted)}} @media(max-width:750px){{.grid{{grid-template-columns:1fr}} .barrow{{grid-template-columns:100px 1fr 88px}} main{{padding:26px 14px}}}}
</style></head><body><main><header><div><div class="eyebrow">Personal expense tracker</div><h1>Shopee spending</h1><p class="muted">Paid orders, refunds and cancellations from confirmation emails</p></div></header>
<div class="grid"><section class="card"><div class="eyebrow">Net spending</div><div class="total">{php(total)}</div><p class="muted">{len(data["orders"])} unique order(s)</p></section><section class="card"><h2>By category</h2>{category_rows}</section></div>
{review}<section class="card"><h2>Orders</h2><div class="tablewrap"><table><thead><tr><th>Date</th><th>Order & items</th><th>Status</th><th class="number">Paid</th><th class="number">Refund</th><th class="number">Net</th></tr></thead><tbody>{order_rows}</tbody></table></div></section>{payment_card}
<footer>Categories allocate each order's net paid amount across items by their listed line prices. Discrepancies appear above for review. Data stays in local SQLite; Gemini receives redacted receipt text during sync.</footer></main></body></html>'''


def serve(db, port: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/health"):
                self.send_error(404)
                return
            body = (json.dumps({"ok": True}) if self.path == "/health" else page(dashboard_data(db))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if self.path == "/health" else "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Dashboard: http://127.0.0.1:{port}")
        server.serve_forever()


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Local Shopee expense tracker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth", help="Authorize the one Gmail account")
    commands.add_parser("sync", help="Fetch and parse Shopee confirmations")
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
