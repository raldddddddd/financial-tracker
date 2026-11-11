import unittest
from datetime import datetime, timezone

from tracker.__main__ import page
from tracker.charts import line_chart
from tracker.insights import filter_period, filter_refunds, order_day, refund_day, resolve_period, trend_series


def milliseconds(timestamp):
    return int(datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc).timestamp() * 1000)


def order(order_id, timestamp, paid, shop="Shopee"):
    ms = milliseconds(timestamp)
    return {"order_id": f"tiktok:{order_id}" if shop == "TikTok Shop" else order_id,
            "ordered_ms": ms, "date": order_day({"ordered_ms": ms}).isoformat(),
            "platform": shop, "status": "paid", "paid_cents": paid,
            "shipping_cents": 0, "discount_cents": 0, "protection_cents": 0,
            "coins_cents": 0, "coins_inferred": 0, "handling_cents": 0,
            "category_allocations": {"Unallocated": paid}, "items": []}


def refund(refund_id, timestamp, amount, shop="Shopee"):
    ms = milliseconds(timestamp)
    return {"event_key": refund_id, "platform": shop, "reference_type": "request",
            "reference_id": refund_id, "amount_cents": amount, "occurred_ms": ms,
            "date": refund_day({"occurred_ms": ms}).isoformat()}


def dashboard(orders, refunds=()):
    paid = sum(item["paid_cents"] for item in orders)
    refunded = sum(item["amount_cents"] for item in refunds)
    return {"orders": orders, "refunds": list(refunds), "paid_total_cents": paid,
            "refund_total_cents": refunded, "total_cents": paid - refunded,
            "categories": {"Unallocated": paid}, "issues": [], "messages": []}


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.december = order("DECEMBER", "2025-12-31T15:59:00", 10000)
        self.january = order("JANUARY", "2025-12-31T16:00:00", 5000, "TikTok Shop")
        self.refund = refund("RETURN-1", "2026-05-01T00:00:00", 2000)
        self.orders = [self.january, self.december]
        self.refunds = [self.refund]

    def test_refund_uses_approval_year_and_does_not_change_categories(self):
        self.assertEqual(order_day(self.december).isoformat(), "2025-12-31")
        self.assertEqual(order_day(self.january).isoformat(), "2026-01-01")
        self.assertEqual(refund_day(self.refund).year, 2026)
        html_2025 = page(dashboard(self.orders, self.refunds), year="2025")
        self.assertIn('<div class="total">₱100.00</div>', html_2025)
        self.assertIn('<h2>Refunds</h2><strong>₱0.00</strong>', html_2025)
        html_2026 = page(dashboard(self.orders, self.refunds), year="2026")
        self.assertIn('<div class="total">₱30.00</div>', html_2026)
        self.assertIn('<h2>Refunds</h2><strong>₱20.00</strong>', html_2026)
        self.assertIn('Unallocated</span><strong>₱50.00</strong>', html_2026)

    def test_custom_range_is_inclusive_and_combines_with_shop(self):
        period = resolve_period(self.orders, start="2026-05-01", end="2026-05-01", refunds=self.refunds)
        self.assertEqual(filter_period(self.orders, period), [])
        self.assertEqual(filter_refunds(self.refunds, period), self.refunds)
        html = page(dashboard(self.orders, self.refunds), shop="shopee", start="2026-05-01", end="2026-05-01")
        self.assertIn('<div class="total">-₱20.00</div>', html)
        self.assertIn('class="line-chart"', html)
        self.assertIn('class="zero-line"', html)
        self.assertIn('href="/?shop=tiktok&amp;start=2026-05-01&amp;end=2026-05-01#shop-filter"', html)

    def test_invalid_and_empty_ranges(self):
        html = page(dashboard(self.orders, self.refunds), start="2026-02-30", end="2026-03-01")
        self.assertIn('role="alert"', html)
        self.assertIn("Enter real dates", html)
        self.assertIn('<div class="total">₱130.00</div>', html)
        empty = page(dashboard(self.orders, self.refunds), start="2027-01-01", end="2027-01-31")
        self.assertIn('<div class="total">₱0.00</div>', empty)
        self.assertIn("No purchases or refunds in this period", empty)

    def test_trend_granularity_and_negative_periods(self):
        all_period = resolve_period(self.orders, refunds=self.refunds)
        kind, annual = trend_series(self.orders, all_period, self.refunds)
        self.assertEqual((kind, annual), ("year", [("2025", 10000), ("2026", 3000)]))
        kind, monthly = trend_series(self.orders, all_period, self.refunds, "month")
        self.assertEqual(kind, "month")
        self.assertEqual(monthly, [("Dec 2025", 10000), ("Jan 2026", 5000),
                                   ("Feb 2026", 0), ("Mar 2026", 0),
                                   ("Apr 2026", 0), ("May 2026", -2000)])
        year = resolve_period(self.orders, year="2026", refunds=self.refunds)
        annual_orders = filter_period(self.orders, year)
        annual_refunds = filter_refunds(self.refunds, year)
        self.assertEqual(len(trend_series(annual_orders, year, annual_refunds)[1]), 12)
        for start, end, expected in (("2026-05-01", "2026-05-02", "day"),
                                     ("2025-12-01", "2026-06-01", "month"),
                                     ("2024-01-01", "2026-06-01", "year")):
            period = resolve_period(self.orders, start=start, end=end, refunds=self.refunds)
            selected_orders = filter_period(self.orders, period)
            selected_refunds = filter_refunds(self.refunds, period)
            kind, series = trend_series(selected_orders, period, selected_refunds)
            self.assertEqual(kind, expected)
            self.assertEqual(sum(amount for _, amount in series),
                             sum(o["paid_cents"] for o in selected_orders) - sum(r["amount_cents"] for r in selected_refunds))

    def test_month_view_filters_refunds_and_shows_daily_trend(self):
        data = dashboard(self.orders, self.refunds)
        may = resolve_period(self.orders, refunds=self.refunds, month="2026-05")
        self.assertEqual((may.start.isoformat(), may.end.isoformat()), ("2026-05-01", "2026-05-31"))
        self.assertEqual(filter_period(self.orders, may), [])
        self.assertEqual(filter_refunds(self.refunds, may), self.refunds)
        kind, daily = trend_series([], may, self.refunds)
        self.assertEqual(kind, "day")
        self.assertEqual(len(daily), 31)
        self.assertEqual(daily[0], ("May 01", -2000))
        self.assertEqual(sum(amount for _, amount in daily), -2000)
        html = page(data, shop="shopee", category="Unallocated", month="2026-05")
        self.assertIn('<div class="total">-₱20.00</div>', html)
        self.assertIn('class="zero-line"', html)
        self.assertIn('name="month" type="month" value="2026-05"', html)
        self.assertIn('href="/?shop=shopee&amp;month=2026-05#overview"', html)
        self.assertIn('href="/?shop=shopee&amp;month=2026-05#orders"', html)
        self.assertIn('href="/?shop=tiktok&amp;month=2026-05&amp;category=Unallocated#shop-filter"', html)
        self.assertEqual(resolve_period(self.orders, refunds=self.refunds, month="2026-13").error,
                         "Choose a valid month.")
        self.assertTrue(resolve_period(self.orders, year="2026", month="2026-05").error)

    def test_trend_group_links_preserve_filters_without_changing_net(self):
        data = dashboard(self.orders, self.refunds)
        monthly = page(data, shop="shopee", category="Unallocated", trend_group="month")
        self.assertIn('aria-label="Net spending by month"', monthly)
        self.assertIn('href="/?shop=shopee&amp;group=month&amp;category=Unallocated#trends" data-trend-group="month" aria-current="page">By month', monthly)
        self.assertIn('href="/?shop=shopee&amp;category=Unallocated#trends" data-trend-group="year">By year', monthly)
        self.assertIn('<div class="total">₱80.00</div>', monthly)
        yearly = page(data, shop="shopee", category="Unallocated")
        self.assertIn('<div class="total">₱80.00</div>', yearly)
        self.assertIn('aria-label="Net spending by year"', yearly)

    def test_trend_hover_shows_exact_amount(self):
        html = line_chart([("Jan 2026", 12345), ("Feb 2026", -67)], "month")
        self.assertIn('aria-label="Jan 2026: ₱123.45"', html)
        self.assertIn('>₱123.45</text>', html)
        self.assertIn('aria-label="Feb 2026: -₱0.67"', html)
        self.assertIn('>-₱0.67</text>', html)

    def test_pagination_keeps_filters(self):
        repeated = [dict(self.january, order_id=f"ORDER-{index:02d}", platform="Shopee") for index in range(21)]
        first = page(dashboard(repeated), shop="shopee", year="2026")
        second = page(dashboard(repeated), 2, "shopee", "2026")
        self.assertIn('href="/?shop=shopee&amp;year=2026&amp;page=2#orders"', first)
        self.assertIn('href="/?shop=shopee&amp;year=2026&amp;page=1#orders"', second)
        self.assertIn("ORDER-20", second)
        self.assertNotIn("ORDER-00</strong>", second)
        category_view = page(dashboard(repeated), shop="shopee", year="2026", category="Unallocated")
        self.assertIn('href="/?shop=shopee&amp;year=2026&amp;category=Unallocated&amp;page=2#orders"', category_view)
        self.assertIn('href="/?shop=shopee&amp;year=2026#overview"', category_view)
        self.assertIn('href="/?shop=shopee&amp;year=2026#orders"', category_view)
        self.assertEqual(category_view.count('>All categories</a>'), 2)
        self.assertNotIn('hero-icon', category_view)
        self.assertIn('name="category" value="Unallocated"', category_view)
        empty_category = page(dashboard(repeated), shop="shopee", year="2026", category="Groceries")
        self.assertIn('No purchases in this category', empty_category)
        self.assertIn('<div class="total">₱1,050.00</div>', empty_category)

    def test_category_reset_keeps_custom_dates_and_totals(self):
        data = dashboard(self.orders, self.refunds)
        selected = page(data, shop="shopee", start="2026-05-01", end="2026-05-01", category="Unallocated")
        all_categories = page(data, shop="shopee", start="2026-05-01", end="2026-05-01")
        reset_url = '/?shop=shopee&amp;start=2026-05-01&amp;end=2026-05-01'
        self.assertIn(f'href="{reset_url}#overview"', selected)
        self.assertIn(f'href="{reset_url}#orders"', selected)
        self.assertIn('<div class="total">-₱20.00</div>', selected)
        self.assertIn('<div class="total">-₱20.00</div>', all_categories)
        self.assertEqual(selected.count('>All categories</a>'), 2)
        self.assertNotIn('>All categories</a>', all_categories)

    def test_order_search_matches_product_or_id_and_preserves_filters(self):
        whiskas = dict(self.december, items=[{"name": "Whiskas Tuna Pouch", "quantity": 1, "line_cents": None, "category": "Unallocated"}])
        other = dict(self.january, items=[{"name": "Cat comb", "quantity": 1, "line_cents": None, "category": "Unallocated"}])
        data = dashboard([whiskas, other], self.refunds)
        by_product = page(data, shop="shopee", category="Unallocated", search="  wHiSkAs  ")
        self.assertIn('value="wHiSkAs"', by_product)
        self.assertIn('DECEMBER</strong>', by_product)
        self.assertNotIn('JANUARY</strong>', by_product)
        self.assertIn('href="/?shop=shopee&amp;category=Unallocated&amp;q=wHiSkAs#orders"', by_product)
        self.assertIn('<div class="total">₱80.00</div>', by_product)
        by_id = page(data, search="jAnUaRy")
        self.assertIn('JANUARY</strong>', by_id)
        self.assertNotIn('DECEMBER</strong>', by_id)
        missing = page(data, search="not a product")
        self.assertIn('No orders match', missing)

    def test_order_search_paginates_matching_orders(self):
        matching = [dict(self.december, order_id=f"MATCH-{index:02d}",
                         items=[{"name": "A&B Pouch", "quantity": 1, "line_cents": None, "category": "Unallocated"}])
                    for index in range(21)]
        data = dashboard([*matching, self.january])
        first = page(data, year="2025", search="A&B")
        second = page(data, 2, year="2025", search="A&B")
        self.assertIn('value="A&amp;B"', first)
        self.assertIn('href="/?year=2025&amp;q=A%26B&amp;page=2#orders"', first)
        self.assertIn('MATCH-20</strong>', second)
        self.assertNotIn('MATCH-00</strong>', second)
        self.assertIn('name="year" value="2025"', first)


if __name__ == "__main__":
    unittest.main()
