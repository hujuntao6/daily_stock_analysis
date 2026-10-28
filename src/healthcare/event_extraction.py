"""Conservative extraction of explicitly forward-looking dates from source text.

Publication dates are deliberately not treated as catalysts.  This helper only
returns a future date/window when a forward-looking cue and a date occur in the
same short sentence/context; it does not infer PDUFA, readout, or quarter dates
from a filing date or from an arbitrary historical year.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class ForwardEvent:
    event_date: date
    event_window_end: date | None
    precision: str
    label: str


_FORWARD_CUE = re.compile(
    r"(?:预计|预期|计划|将于|将要|拟于|拟在|目标|有望|预定|安排|即将|将|"
    r"expected|expect|planned|plan(?:ned)?|target(?:ed)?|scheduled|anticipat|will|forecast)",
    re.IGNORECASE,
)
_TOPIC_CUE = re.compile(
    r"(?:clinical|trial|readout|data|pdufa|approval|action date|regulat|drug|therapy|medicine|"
    r"earnings|results|conference|meeting|milestone|业绩|财报|临床|试验|数据|批准|获批|获准|批件|上市|审评|药|药物|会议|里程碑|读出)",
    re.IGNORECASE,
)
_PAST_CUE = re.compile(
    r"(?:already reported|already published|was reported|were reported|completed|"
    r"已公布|已发布|已披露|已经完成|已(?:经)?获(?:得)?|已(?:经)?取得|已(?:经)?批准|"
    r"已获批|已获准|报告期|财务期间|上季度|历史数据)",
    re.IGNORECASE,
)
_PDUFA_CUE = re.compile(r"\b(?:pdufa|target action date)\b|目标行动日期", re.IGNORECASE)
_ISO = re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_CN_DAY = re.compile(r"(?<!\d)(20\d{2})年(\d{1,2})月(\d{1,2})日")
_CN_MONTH = re.compile(r"(?<!\d)(20\d{2})年(\d{1,2})月(?!\d)")
_EN_DAY = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})",
    re.IGNORECASE,
)
_Q = re.compile(r"\bQ([1-4])\s*(20\d{2})\b|(?<!\d)(20\d{2})\s*(?:年\s*)?Q([1-4])(?!\d)", re.IGNORECASE)
_CN_QUARTER = re.compile(r"(?<!\d)(20\d{2})年(?:第)?([一二三四1-4])季度")
_H = re.compile(r"\bH([12])\s*(20\d{2})\b|(?<!\d)(20\d{2})\s*(?:年\s*)?H([12])(?!\d)", re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)(20\d{2})年度?(?!\d)")


def _valid(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _context(text: str, start: int, end: int) -> str:
    boundaries = ("。", ".", ";", "；", "\n")
    left = max((text.rfind(mark, 0, start) for mark in boundaries), default=-1)
    right_candidates = [position for mark in boundaries if (position := text.find(mark, end)) >= 0]
    right = min(right_candidates) if right_candidates else min(len(text), end + 100)
    return text[left + 1 : right].strip()


def _has_forward_context(text: str, start: int, end: int) -> bool:
    context = _context(text, start, end)
    # A topic word ("data", "results", "earnings", etc.) is not itself a
    # forward-looking statement.  The only exception is an explicit PDUFA or
    # target-action-date disclosure, which is intrinsically prospective.
    forward = bool(_FORWARD_CUE.search(context))
    explicit_regulatory_date = bool(_PDUFA_CUE.search(context))
    return bool(
        (forward or explicit_regulatory_date)
        and _TOPIC_CUE.search(context)
        and not _PAST_CUE.search(context)
    )


def extract_forward_event(text: str, as_of: date) -> ForwardEvent | None:
    text = str(text or "")
    candidates: list[tuple[date, date | None, str, str, int, int]] = []

    for match in _ISO.finditer(text):
        event = _valid(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if event:
            candidates.append((event, event, "day", "明确日期", match.start(), match.end()))
    for match in _CN_DAY.finditer(text):
        event = _valid(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if event:
            candidates.append((event, event, "day", "明确日期", match.start(), match.end()))
    for match in _EN_DAY.finditer(text):
        month_names = {name.lower(): index for index, names in enumerate(("Jan January", "Feb February", "Mar March", "Apr April", "May", "Jun June", "Jul July", "Aug August", "Sep Sept September", "Oct October", "Nov November", "Dec December"), 1) for name in names.split()}
        month = month_names.get(match.group(0).split()[0].lower())
        event = _valid(int(match.group(2)), month or 1, int(match.group(1)))
        if event:
            candidates.append((event, event, "day", "明确日期", match.start(), match.end()))
    for match in _CN_MONTH.finditer(text):
        year, month = int(match.group(1)), int(match.group(2))
        event = _valid(year, month, 1)
        if event:
            candidates.append((event, date(year, month, monthrange(year, month)[1]), "month", "明确月份窗口", match.start(), match.end()))
    for match in _Q.finditer(text):
        if match.group(1):
            quarter, year = int(match.group(1)), int(match.group(2))
        else:
            year, quarter = int(match.group(3)), int(match.group(4))
        month = (quarter - 1) * 3 + 1
        end_month = month + 2
        candidates.append((date(year, month, 1), date(year, end_month, monthrange(year, end_month)[1]), "quarter", f"{year}年Q{quarter}", match.start(), match.end()))
    for match in _CN_QUARTER.finditer(text):
        year = int(match.group(1))
        quarter_token = match.group(2)
        quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(quarter_token)
        if quarter is None:
            quarter = int(quarter_token)
        month = (quarter - 1) * 3 + 1
        end_month = month + 2
        candidates.append((date(year, month, 1), date(year, end_month, monthrange(year, end_month)[1]), "quarter", f"{year}年第{quarter}季度", match.start(), match.end()))
    for match in _H.finditer(text):
        if match.group(1):
            half, year = int(match.group(1)), int(match.group(2))
        else:
            year, half = int(match.group(3)), int(match.group(4))
        start_month, end_month = (1, 6) if half == 1 else (7, 12)
        candidates.append((date(year, start_month, 1), date(year, end_month, monthrange(year, end_month)[1]), "half", f"{year}年H{half}", match.start(), match.end()))
    if not (_Q.search(text) or _CN_QUARTER.search(text) or _H.search(text) or _CN_MONTH.search(text) or _CN_DAY.search(text) or _EN_DAY.search(text)):
        for match in _YEAR.finditer(text):
            year = int(match.group(1))
            candidates.append((date(year, 1, 1), date(year, 12, 31), "year", f"{year}年度", match.start(), match.end()))

    horizon = as_of + timedelta(days=90)
    for event, event_end, precision, label, start, end in sorted(candidates, key=lambda item: item[0]):
        if event_end <= as_of or event > horizon or not _has_forward_context(text, start, end):
            continue
        context = _context(text, start, end)
        # PDUFA/action dates require the explicit regulatory cue; a generic
        # date in an unrelated paragraph must not be promoted to PDUFA.
        if _PDUFA_CUE.search(context) and precision not in {"day", "month"}:
            continue
        return ForwardEvent(event, event_end, precision, context[:180] or label)
    return None


__all__ = ["ForwardEvent", "extract_forward_event"]
