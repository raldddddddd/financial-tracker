"""Read-only period selection and spending trend calculations for the dashboard."""

from __future__ import annotations

import re
from calendar import monthrange
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
    month: str = ""
    start_value: str = ""
    end_value: str = ""
    error: str = ""


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
