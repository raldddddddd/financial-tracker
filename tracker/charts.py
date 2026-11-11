"""Accessible, dependency-free SVG charts for the local dashboard."""

from __future__ import annotations

import html
import math

from .core import php


PALETTE = ("#3f8069", "#e59462", "#7976b0", "#d6b451", "#5d9bbb",
           "#bd7792", "#78a25a", "#aa8367", "#638d90", "#a4a8aa")


def line_chart(series: list[tuple[str, int]], granularity: str) -> str:
    if not series:
        return ""
    values = [amount for _, amount in series]
    low, high = min(0, *values), max(0, *values)
    if low == high:
        low, high = -100, 100
    left, right, top, bottom = 92, 716, 22, 220
    x = lambda index: (left + (right - left) * index / (len(series) - 1)) if len(series) > 1 else (left + right) / 2
    y = lambda amount: bottom - (amount - low) * (bottom - top) / (high - low)
    points = " ".join(f"{x(index):.1f},{y(amount):.1f}" for index, (_, amount) in enumerate(series))
    ticks = sorted({low, 0, high})
    grid = "".join(
        f'<line class="{"zero-line" if value == 0 else "grid-line"}" x1="{left}" y1="{y(value):.1f}" x2="{right}" y2="{y(value):.1f}"/>'
        f'<text class="axis-label" x="{left - 9}" y="{y(value) + 4:.1f}" text-anchor="end">{html.escape(php(value))}</text>'
        for value in ticks
    )
    step = max(1, math.ceil(len(series) / 7))
    labels = "".join(
        f'<text class="axis-label" x="{x(index):.1f}" y="246" text-anchor="middle">{html.escape(label)}</text>'
        for index, (label, _) in enumerate(series) if index % step == 0 or index == len(series) - 1
    )
    dots = ""
    for index, (label, amount) in enumerate(series):
        cx, cy = x(index), y(amount)
        tip_x = max(0, min(610, cx - 70))
        tip_y = cy + 14 if cy < 70 else cy - 48
        label_text, amount_text = html.escape(label), html.escape(php(amount))
        dots += (
            f'<g class="trend-marker" tabindex="0" aria-label="{label_text}: {amount_text}">'
            f'<circle class="trend-hit-area" cx="{cx:.1f}" cy="{cy:.1f}" r="14"/>'
            f'<circle class="trend-point" cx="{cx:.1f}" cy="{cy:.1f}" r="4.5"/>'
            f'<g class="trend-tooltip"><rect x="{tip_x:.1f}" y="{tip_y:.1f}" width="140" height="42" rx="6"/>'
            f'<text x="{tip_x + 9:.1f}" y="{tip_y + 16:.1f}">{label_text}</text>'
            f'<text class="trend-tooltip-amount" x="{tip_x + 9:.1f}" y="{tip_y + 33:.1f}">{amount_text}</text></g></g>'
        )
    description = "; ".join(f"{label}: {php(amount)}" for label, amount in series)
    line = f'<polyline class="trend-line" points="{points}"/>' if len(series) > 1 else ""
    return (f'<svg class="line-chart" viewBox="0 0 750 265" role="img" '
            f'aria-label="Net spending by {html.escape(granularity)}">'
            f'<title>Net spending by {html.escape(granularity)}</title>'
            f'<desc>{html.escape(description)}</desc>{grid}{line}{dots}{labels}</svg>')


def category_pie(categories: dict[str, int], selected: str, url_for_category) -> tuple[str, str]:
    entries = [(name, amount) for name, amount in sorted(categories.items(), key=lambda pair: -pair[1]) if amount > 0]
    total = sum(amount for _, amount in entries)
    if not total:
        return "", ""
    cx = cy = 110
    radius = 95
    angle = -math.pi / 2
    slices = []
    legend = []
    for index, (name, amount) in enumerate(entries):
        sweep = amount / total * 2 * math.pi
        color = PALETTE[index % len(PALETTE)]
        if len(entries) == 1:
            shape = f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="{color}"/>'
        else:
            end = angle + sweep
            x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
            x2, y2 = cx + radius * math.cos(end), cy + radius * math.sin(end)
            shape = (f'<path d="M {cx} {cy} L {x1:.3f} {y1:.3f} A {radius} {radius} 0 '
                     f'{1 if sweep > math.pi else 0} 1 {x2:.3f} {y2:.3f} Z" fill="{color}"/>')
            angle = end
        href = html.escape(url_for_category(name), quote=True)
        label = html.escape(name)
        percent = amount / total * 100
        active = " selected" if name == selected else ""
        slices.append(f'<a href="{href}" aria-label="View {label}: {html.escape(php(amount))}, {percent:.1f}%">'
                      f'{shape}<title>{label}: {html.escape(php(amount))} ({percent:.1f}%)</title></a>')
        legend.append(f'<a class="category-legend-row{active}" href="{href}">'
                      f'<span class="category-swatch" style="background:{color}"></span>'
                      f'<span class="category-name">{label}</span><strong>{html.escape(php(amount))}</strong>'
                      f'<small>{percent:.1f}%</small></a>')
    svg = ('<svg class="category-pie" viewBox="0 0 220 220" role="group" '
           'aria-label="Purchase spending by category">'
           '<title>Purchase spending by category</title>' + "".join(slices) + '</svg>')
    return svg, "".join(legend)
