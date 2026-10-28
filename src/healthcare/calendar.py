"""Exchange-session alignment with an exchange_calendars fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


MARKET_TIMEZONES = {
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
}
MARKET_CALENDARS = {"cn": "XSHG", "hk": "XHKG", "us": "XNYS"}
MARKET_CLOSE = {"cn": time(15, 0), "hk": time(16, 0), "us": time(16, 0)}


@dataclass(frozen=True)
class Session:
    market: str
    session_date: date
    close_at: datetime
    source: str  # exchange_calendars or weekday_fallback


def parse_as_of(value: str | None, default_timezone: str) -> datetime:
    """Parse ISO date/time; date-only input means end of day in config timezone."""

    zone = ZoneInfo(default_timezone)
    if not value:
        return datetime.now(zone)
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return datetime.combine(date.fromisoformat(text), time(23, 59, 59), tzinfo=zone)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed


def _previous_weekday(day: date) -> date:
    result = day - timedelta(days=1)
    while result.weekday() >= 5:
        result -= timedelta(days=1)
    return result


class TradingCalendar:
    def __init__(self) -> None:
        self._project_calendar = None
        try:
            # When installed inside daily_stock_analysis, reuse its canonical
            # market calendar instead of creating a competing date policy.
            from src.core import trading_calendar as project_calendar  # type: ignore

            if hasattr(project_calendar, "get_effective_trading_date"):
                self._project_calendar = project_calendar
        except Exception:
            self._project_calendar = None
        self._exchange_calendars = None
        try:
            import exchange_calendars as exchange_calendars  # type: ignore

            self._exchange_calendars = exchange_calendars
        except Exception:
            self._exchange_calendars = None

    def completed_session(self, market: str, as_of: datetime) -> Session:
        if market not in MARKET_TIMEZONES:
            raise ValueError(f"unsupported market: {market}")
        if self._project_calendar is not None:
            try:
                session_date = self._project_calendar.get_effective_trading_date(market, as_of)
                close_at = self._verified_close(market, session_date)
                if close_at is not None and close_at <= as_of.astimezone(close_at.tzinfo):
                    return Session(market, session_date, close_at, "daily_stock_analysis")
                # The host calendar can fail-open to a natural date.  Without
                # an independently verified exchange close, do not call that
                # date authoritative; the explicit fallback remains visible in
                # report quality and status.
            except Exception:
                pass
        if self._exchange_calendars is not None:
            try:
                return self._from_exchange_calendar(market, as_of)
            except Exception:
                # A provider/calendar version mismatch should degrade to the
                # explicit weekday fallback, never silently use a future date.
                pass
        return self._from_weekday_fallback(market, as_of)

    def _verified_close(self, market: str, session_date: date) -> datetime | None:
        """Return the exchange calendar's actual close, including half-days."""

        if self._exchange_calendars is None:
            return None
        calendar = self._exchange_calendars.get_calendar(MARKET_CALENDARS[market])
        try:
            session = calendar.date_to_session(session_date, direction="none")
        except TypeError:
            session = calendar.date_to_session(session_date)
        close = calendar.session_close(session)
        if hasattr(close, "to_pydatetime"):
            close = close.to_pydatetime()
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        return close.astimezone(ZoneInfo(MARKET_TIMEZONES[market]))

    def _from_exchange_calendar(self, market: str, as_of: datetime) -> Session:
        calendar = self._exchange_calendars.get_calendar(MARKET_CALENDARS[market])
        local_day = as_of.astimezone(ZoneInfo(MARKET_TIMEZONES[market])).date()
        start = local_day - timedelta(days=10)
        schedule = calendar.schedule.loc[str(start) : str(local_day)]
        if schedule.empty:
            raise ValueError("no exchange sessions in lookback")
        as_of_utc = as_of.astimezone(timezone.utc)
        eligible = []
        for idx, row in schedule.iterrows():
            close_key = "market_close" if "market_close" in row.index else "close"
            if close_key not in row.index:
                raise ValueError("exchange calendar schedule has no close column")
            close = row[close_key]
            if getattr(close, "tzinfo", None) is None:
                close = close.replace(tzinfo=timezone.utc)
            close_dt = close.to_pydatetime() if hasattr(close, "to_pydatetime") else close
            if close_dt <= as_of_utc:
                session_date = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx))
                eligible.append((session_date, close_dt))
        if not eligible:
            raise ValueError("no completed exchange session")
        session_date, close_at = eligible[-1]
        return Session(market, session_date, close_at, "exchange_calendars")

    def _from_weekday_fallback(self, market: str, as_of: datetime) -> Session:
        exchange_zone = ZoneInfo(MARKET_TIMEZONES[market])
        local = as_of.astimezone(exchange_zone)
        candidate = local.date()
        close_at = datetime.combine(candidate, MARKET_CLOSE[market], tzinfo=exchange_zone)
        if candidate.weekday() >= 5 or local < close_at:
            candidate = _previous_weekday(candidate)
            close_at = datetime.combine(candidate, MARKET_CLOSE[market], tzinfo=exchange_zone)
        return Session(market, candidate, close_at, "weekday_fallback")

    def align(self, markets: tuple[str, ...], as_of: datetime) -> tuple[dict[str, Session], bool, str | None]:
        """Resolve one completed session per market.

        A/H markets do not share every holiday.  Different latest dates are
        therefore a normal informational condition, not a reason to discard
        both markets' valid sessions.  ``aligned`` means each requested market
        has a resolved completed session; consumers can compare ``session_date``
        values independently.
        """

        sessions: dict[str, Session] = {}
        errors: list[str] = []
        for market in markets:
            try:
                sessions[market] = self.completed_session(market, as_of)
            except Exception as exc:
                errors.append(f"{market}: {exc}")
        if errors:
            return sessions, False, "; ".join(errors)
        return sessions, True, None
