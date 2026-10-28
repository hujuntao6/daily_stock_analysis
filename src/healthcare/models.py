"""Small, serialisable domain models for the healthcare daily report."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from typing import Any, Iterable


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    market: str
    subsector: str = "未分类"
    official_domains: tuple[str, ...] = ()
    ir_feeds: tuple[str, ...] = ()
    cik: str | None = None
    nct_ids: tuple[str, ...] = ()
    # Optional, explicitly verified English legal sponsor names.  Discovery
    # never guesses a sponsor from a Chinese company name or ticker.
    sponsor_aliases: tuple[str, ...] = ()


@dataclass
class Quote:
    symbol: str
    name: str
    market: str
    subsector: str
    session_date: date
    close: float | None
    previous_close: float | None
    volume: float | None = None
    average_volume_20: float | None = None
    benchmark_symbol: str | None = None
    benchmark_return_pct: float | None = None
    source: str = "unknown"
    status: str = "ok"
    error: str | None = None

    @property
    def return_pct(self) -> float | None:
        if self.close is None or self.previous_close in (None, 0):
            return None
        return (self.close / self.previous_close - 1.0) * 100.0

    @property
    def relative_return_pct(self) -> float | None:
        if self.return_pct is None or self.benchmark_return_pct is None:
            return None
        return self.return_pct - self.benchmark_return_pct

    @property
    def volume_ratio(self) -> float | None:
        if self.volume is None or self.average_volume_20 in (None, 0):
            return None
        return self.volume / self.average_volume_20


@dataclass(frozen=True)
class Evidence:
    id: str
    url: str
    publisher: str
    published_at: datetime | None
    retrieved_at: datetime
    snippet: str
    identifiers: tuple[str, ...] = ()
    source_kind: str = "unknown"
    event_date: date | None = None
    event_precision: str | None = None  # day, quarter, estimated_milestone
    event_label: str | None = None
    event_window_end: date | None = None
    event_status: str | None = None  # actual, estimated, unknown
    published_precision: str = "timestamp"  # timestamp, date, unknown


@dataclass(frozen=True)
class SourceStatus:
    source: str
    status: str  # ok, not_configured, blocked, error, no_data
    count: int = 0
    detail: str | None = None


@dataclass(frozen=True)
class Attribution:
    symbol: str
    session_date: date
    status: str  # 相关事件, 可能相关, 未找到解释
    evidence_ids: tuple[str, ...] = ()
    note: str = ""
    timing: str = "unknown"  # pre_close, post_close, nearby, none


@dataclass(frozen=True)
class Catalyst:
    symbol: str
    label: str
    window: str
    event_date: date | None
    precision: str
    evidence_ids: tuple[str, ...]
    note: str = ""
    window_start: date | None = None
    window_end: date | None = None


def jsonable(value: Any) -> Any:
    """Convert domain objects to JSON-safe values without leaking secrets."""

    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def ensure_tuple(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in (values or ()) if str(v).strip())
