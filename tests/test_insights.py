import unittest
from datetime import datetime, timezone

from tracker.__main__ import page
from tracker.insights import filter_period, order_day, resolve_period, trend_series


def order(order_id, timestamp, amount, *, shop="Shopee", refund=0):
    milliseconds = int(datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return {"order_id": f"tiktok:{order_id}" if shop == "TikTok Shop" else order_id,
            "ordered_ms": milliseconds, "updated_ms": milliseconds + 86_400_000,
            "date": order_day({"ordered_ms": milliseconds}).isoformat(), "platform": shop,
            "status": "partial_refund" if refund else "paid", "paid_cents": amount + refund,
            "shipping_cents": 0, "discount_cents": 0, "protection_cents": 0,
            "coins_cents": 0, "coins_inferred": 0, "handling_cents": 0,
            "refund_cents": refund, "effective_cents": amount, "items": []}


def dashboard(orders):
    return {"orders": orders, "total_cents": sum(item["effective_cents"] for item in orders),
            "categories": {"Unallocated": sum(item["effective_cents"] for item in orders)},
            "issues": [], "messages": [], "refund_requests": []}


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.december = order("DECEMBER", "2025-12-31T15:59:00", 8000, refund=2000)
        self.january = order("JANUARY", "2025-12-31T16:00:00", 5000, shop="TikTok Shop")
        self.orders = [self.january, self.december]

    def test_manila_year_boundary_and_purchase_year_refund(self):
        self.assertEqual(order_day(self.december).isoformat(), "2025-12-31")
        self.assertEqual(order_day(self.january).isoformat(), "2026-01-01")
        selected = filter_period(self.orders, resolve_period(self.orders, year="2025"))
        self.assertEqual([item["order_id"] for item in selected], ["DECEMBER"])
        self.december["updated_ms"] = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
        html = page(dashboard(self.orders), year="2025")
        self.assertIn('<div class="total">₱80.00</div>', html)
        self.assertIn('<h2>Refunds</h2><strong>₱20.00</strong>', html)
        self.assertNotIn("JANUARY</strong>", html)
        self.assertEqual(sum(amount for _, amount in trend_series(selected, resolve_period(self.orders, year="2025"))[1]), 8000)

    def test_custom_range_is_inclusive_and_combines_with_shop(self):
        self.assertEqual([item["order_id"] for item in filter_period(self.orders, resolve_period(self.orders, start="2026-01-01", end="2026-01-01"))], ["tiktok:JANUARY"])
        html = page(dashboard(self.orders), shop="tiktok", start="2026-01-01", end="2026-01-01")
        self.assertIn('<div class="total">₱50.00</div>', html)
        self.assertIn("JANUARY</strong>", html)
        self.assertNotIn("DECEMBER</strong>", html)
        self.assertIn('href="/?shop=shopee&start=2026-01-01&end=2026-01-01#shop-filter"', html)
        self.assertNotIn("<h2>By shop</h2>", html)
        self.assertIn("<h2>By shop</h2>", page(dashboard(self.orders)))

    def test_invalid_and_empty_ranges(self):
        html = page(dashboard(self.orders), start="2026-02-30", end="2026-03-01")
        self.assertIn('role="alert"', html)
        self.assertIn("Enter real dates", html)
        self.assertIn('<div class="total">₱130.00</div>', html)
        self.assertIn("Enter both a start and end date", page(dashboard(self.orders), start="2026-01-01"))
        self.assertIn("The end date must be on or after the start date",
                      page(dashboard(self.orders), start="2026-02-01", end="2026-01-01"))
        empty = page(dashboard(self.orders), start="2027-01-01", end="2027-01-31")
        self.assertIn('<div class="total">₱0.00</div>', empty)
        self.assertIn("No orders in this period", empty)

    def test_trend_granularity_and_totals(self):
        all_period = resolve_period(self.orders)
        kind, annual = trend_series(self.orders, all_period)
        self.assertEqual(kind, "year")
        self.assertEqual(annual, [("2025", 8000), ("2026", 5000)])
        year = resolve_period(self.orders, year="2026")
        self.assertEqual(len(trend_series(filter_period(self.orders, year), year)[1]), 12)
        for start, end, expected in (("2026-01-01", "2026-01-02", "day"),
                                     ("2025-12-01", "2026-02-01", "month"),
                                     ("2024-01-01", "2026-01-01", "year")):
            period = resolve_period(self.orders, start=start, end=end)
            selected = filter_period(self.orders, period)
            kind, series = trend_series(selected, period)
            self.assertEqual(kind, expected)
            self.assertEqual(sum(amount for _, amount in series), sum(item["effective_cents"] for item in selected))

    def test_pagination_keeps_all_filters(self):
        repeated = [dict(self.january, order_id=f"ORDER-{index:02d}", platform="Shopee") for index in range(21)]
        first = page(dashboard(repeated), shop="shopee", year="2026")
        second = page(dashboard(repeated), 2, "shopee", "2026")
        self.assertIn('href="/?shop=shopee&year=2026&page=2#orders"', first)
        self.assertIn('href="/?shop=shopee&year=2026&page=1#orders"', second)
        custom = page(dashboard(repeated), shop="shopee", start="2026-01-01", end="2026-01-31")
        self.assertIn('href="/?shop=shopee&start=2026-01-01&end=2026-01-31&page=2#orders"', custom)
        self.assertIn("ORDER-20", second)
        self.assertNotIn("ORDER-00</strong>", second)


if __name__ == "__main__":
    unittest.main()
