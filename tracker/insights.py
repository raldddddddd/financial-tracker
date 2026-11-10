"""Read-only period selection and spending trend calculations for the dashboard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .core import MANILA_TZ


@dataclass(frozen=True)
class PeriodSelection:
    mode: str = "all"
    label: str = "All time"
    start: date | None = None
    end: date | None = None
    year: str = ""
    start_value: str = ""
    end_value: str = ""
    error: str = ""


def order_day(order: dict) -> date:
    return datetime.fromtimestamp(order["ordered_ms"] / 1000, MANILA_TZ).date()


def available_years(orders: list[dict]) -> list[int]:
    return sorted({datetime.now(MANILA_TZ).year, *(order_day(order).year for order in orders)}, reverse=True)


def resolve_period(orders: list[dict], year: str = "", start: str = "", end: str = "") -> PeriodSelection:
    if year and (start or end):
        return PeriodSelection(error="Choose either a year or a custom date range.")
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
        if not re.fullmatch(r"\d{4}", year) or int(year) not in available_years(orders):
            return PeriodSelection(error="Choose a year from the list.")
        first, last = date(int(year), 1, 1), date(int(year), 12, 31)
        return PeriodSelection(mode="year", label=year, start=first, end=last, year=year)
    return PeriodSelection()


def filter_period(orders: list[dict], period: PeriodSelection) -> list[dict]:
    if period.mode == "all":
        return orders
    return [order for order in orders if period.start <= order_day(order) <= period.end]


def trend_series(orders: list[dict], period: PeriodSelection) -> tuple[str, list[tuple[str, int]]]:
    """Return a complete calendar series with each order's current net in its purchase period."""
    if not orders:
        return "", []
    if period.mode == "all":
        years = range(min(order_day(order).year for order in orders),
                      max(order_day(order).year for order in orders) + 1)
        units = [(year, str(year)) for year in years]
        granularity = "year"
    elif period.mode == "year":
        units = [((int(period.year), month), date(int(period.year), month, 1).strftime("%b"))
                 for month in range(1, 13)]
        granularity = "month"
    elif (period.end - period.start).days <= 30:
        days = (period.end - period.start).days + 1
        units = [(period.start + timedelta(days=index),
                  (period.start + timedelta(days=index)).strftime("%b %d, %Y")) for index in range(days)]
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
        totals[key] += order["effective_cents"]
    return granularity, [(label, totals[key]) for key, label in units]
