"""Forward catalyst extraction from explicit official dates and CT.gov milestones."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Iterable

from .models import Catalyst, Evidence


_QUARTER_PATTERNS = (
    re.compile(r"\bQ([1-4])\s*(20\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(20\d{2})\s*年\s*第?([1-4])季度"),
    re.compile(r"\b(20\d{2})\s*(?:年\s*)?Q([1-4])\b", re.IGNORECASE),
)
_HALF_PATTERNS = (
    re.compile(r"\bH([12])\s*(20\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(20\d{2})\s*年\s*上半年"),
    re.compile(r"\b(20\d{2})\s*年\s*下半年"),
)
_YEAR_PATTERN = re.compile(r"\b(20\d{2})\s*年(?:度)?\b")
_FORWARD_CUE = re.compile(
    r"(?:预计|预期|计划|将于|将要|拟于|拟在|目标|有望|预定|安排|即将|将|"
    r"expected|expect|planned|plan(?:ned)?|target(?:ed)?|scheduled|anticipat|will|forecast)",
    re.IGNORECASE,
)


def _quarter_window(year: int, quarter: int) -> tuple[date, date]:
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    return date(year, start_month, 1), date(year, end_month, monthrange(year, end_month)[1])


def _half_window(year: int, half: int) -> tuple[date, date]:
    start_month = 1 if half == 1 else 7
    end_month = 6 if half == 1 else 12
    return date(year, start_month, 1), date(year, end_month, monthrange(year, end_month)[1])


def _year_window(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def _window(days: int) -> str:
    if days <= 7:
        return "未来7天"
    if days <= 30:
        return "未来30天"
    if days <= 90:
        return "未来90天"
    return "超过90天"


def _identifier(item: Evidence) -> str:
    return next(
        (identifier for identifier in item.identifiers if identifier and not identifier.upper().startswith("NCT")),
        "未标的",
    )


def _parsed_window(item: Evidence, as_of: date) -> tuple[date | None, date | None, str, str]:
    """Return start/end/precision/label without making a quarter a day."""

    if item.source_kind in {"sec_filing", "sec_exhibit", "company_ir", "cninfo", "hkex", "ah_announcement"}:
        from .event_extraction import extract_forward_event

        row = extract_forward_event(item.snippet, as_of)
        if row:
            return row.event_date, row.event_window_end, row.precision, row.label

    if item.event_date:
        precision = item.event_precision or "day"
        end = item.event_window_end or item.event_date
        return item.event_date, end, precision, item.event_label or item.snippet.split(";", 1)[0][:120]
    text = item.snippet or ""
    if not _FORWARD_CUE.search(text):
        return None, None, "unknown", item.event_label or text.split(";", 1)[0][:120]
    for pattern in _QUARTER_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if pattern in (_QUARTER_PATTERNS[1], _QUARTER_PATTERNS[2]):
            year, quarter = int(match.group(1)), int(match.group(2))
        else:
            quarter, year = int(match.group(1)), int(match.group(2))
        start, end = _quarter_window(year, quarter)
        return start, end, "quarter", item.event_label or f"{year}年Q{quarter}"
    for pattern in _HALF_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if pattern is _HALF_PATTERNS[1]:
            year, half = int(match.group(1)), 1
        elif pattern is _HALF_PATTERNS[2]:
            year, half = int(match.group(1)), 2
        else:
            half, year = int(match.group(1)), int(match.group(2))
        start, end = _half_window(year, half)
        return start, end, "half", item.event_label or f"{year}年H{half}"
    match = _YEAR_PATTERN.search(text)
    if match:
        year = int(match.group(1))
        start, end = _year_window(year)
        return start, end, "year", item.event_label or f"{year}年度"
    return None, None, "unknown", item.event_label or text.split(";", 1)[0][:120]


def extract_catalysts(evidence: Iterable[Evidence], as_of: date) -> tuple[Catalyst, ...]:
    horizon = as_of + timedelta(days=90)
    catalysts: list[Catalyst] = []
    for item in evidence:
        start, end, precision, label = _parsed_window(item, as_of)
        if start is None or end is None or end <= as_of or start > horizon:
            continue
        effective_start = max(start, as_of + timedelta(days=1))
        effective_end = min(end, horizon)
        if effective_start > effective_end:
            continue
        note = ""
        if item.source_kind.startswith("clinicaltrials") and item.event_date:
            note = "预计试验里程碑，不代表读出"
            if item.event_status == "actual":
                note += "；注册记录标记为 ACTUAL，但不等于数据读出"
        days = (effective_start - as_of).days
        event_date = effective_start if precision == "day" and effective_start == effective_end else None
        catalysts.append(
            Catalyst(
                symbol=_identifier(item),
                label=label or "官方事件",
                window=_window(days),
                event_date=event_date,
                precision=precision,
                evidence_ids=(item.id,),
                note=note,
                window_start=effective_start,
                window_end=effective_end,
            )
        )
    deduped: dict[tuple[str, date | None, date | None, str], Catalyst] = {}
    for catalyst in catalysts:
        key = (catalyst.symbol, catalyst.window_start, catalyst.window_end, catalyst.label)
        deduped[key] = catalyst
    return tuple(sorted(deduped.values(), key=lambda x: (x.window_start or date.max, x.symbol)))
