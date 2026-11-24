"""The HealthLens evidence format.

Every insight on the dashboard and every sentence the LLM writes must point to one or more
evidence items (E1, E2, ...). An evidence item is a plain, deterministic statement computed
from the workbook, plus the method used and the sheet it came from.

Dates are written as tokens like [[2026-03-18]] so the same statement can be rendered two ways:
  * for the dashboard:  'Mar 18, 2026'
  * for the LLM:        'month 7 of the record'  (relative time, no calendar dates leave the app)
"""
from __future__ import annotations

import re

import pandas as pd

_TOKEN = re.compile(r'\[\[(\d{4}-\d{2}-\d{2})\]\]')


def D(date) -> str:
    """Wrap a date so it can be rendered as an absolute or relative date later."""
    return f'[[{pd.Timestamp(date).strftime("%Y-%m-%d")}]]'


def nice_date(value) -> str:
    d = pd.Timestamp(value)
    return f'{d:%b} {d.day}, {d.year}'  # portable (no %-d, which fails on Windows)


def fmt(value, digits=1) -> str:
    if value is None:
        return 'n/a'
    value = float(value)
    if abs(value) >= 1000:
        return f'{value:,.0f}'
    if float(value).is_integer() and digits <= 1:
        return f'{value:.0f}'
    return f'{value:.{digits}f}'


class EvidenceRegistry:
    def __init__(self, start: pd.Timestamp | None):
        self.start = pd.Timestamp(start) if start is not None else None
        self.items: list[dict] = []

    def add(self, kind: str, text: str, *, metric: str = '', method: str = '', source: str = '',
            values: dict | None = None, scenario_tags=()) -> str:
        eid = f'E{len(self.items) + 1}'
        self.items.append({
            'id': eid, 'kind': kind, 'metric': metric, 'text_raw': text,
            'method': method, 'source': source, 'values': values or {},
            'tags': list(scenario_tags),
        })
        return eid

    def render_ui(self, text: str) -> str:
        return _TOKEN.sub(lambda m: nice_date(m.group(1)), text)

    def render_llm(self, text: str) -> str:
        if self.start is None:
            return _TOKEN.sub('an unrecorded date', text)

        def rel(m):
            d = pd.Timestamp(m.group(1))
            months = (d.year - self.start.year) * 12 + (d.month - self.start.month)
            if months < 0:
                return f'{-months} month{"s" if -months != 1 else ""} before the record began'
            return f'month {months + 1} of the record'
        return _TOKEN.sub(rel, text)

    def export(self) -> list[dict]:
        out = []
        for it in self.items:
            item = {k: v for k, v in it.items() if k != 'text_raw'}
            item['text'] = self.render_ui(it['text_raw'])
            item['llm_text'] = self.render_llm(it['text_raw'])
            item['method'] = self.render_ui(it['method'])
            out.append(item)
        return out
