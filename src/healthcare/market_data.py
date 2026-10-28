"""Market-data adapters with fixture support and conservative coverage reporting."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from typing import Any, Iterable

from .calendar import Session
from .config import HealthcareConfig
from .models import Quote, Security, SourceStatus


@dataclass(frozen=True)
class MarketDataResult:
    quotes: tuple[Quote, ...]
    benchmark_quotes: tuple[Quote, ...]
    statuses: tuple[SourceStatus, ...]
    gaps: tuple[str, ...]

    @property
    def covered_count(self) -> int:
        return sum(1 for quote in self.quotes if quote.status == "ok" and quote.close is not None)

    @property
    def requested_count(self) -> int:
        return len(self.quotes)

    @property
    def coverage_pct(self) -> float:
        return (self.covered_count / self.requested_count * 100.0) if self.requested_count else 0.0

    @property
    def benchmark_covered_count(self) -> int:
        return sum(1 for quote in self.benchmark_quotes if quote.status == "ok" and quote.close is not None)


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _price(value: Any) -> float | None:
    """Return a usable positive market price, never a placeholder/old value."""

    result = _float(value)
    return result if result is not None and result > 0 else None


def _date(value: Any) -> date | None:
    # ``datetime`` (and pandas.Timestamp) subclasses ``date``; return the
    # calendar date rather than the datetime object so comparisons against an
    # aligned session never fail due to type mismatch.
    if hasattr(value, "to_pydatetime"):
        try:
            value = value.to_pydatetime()
        except Exception:
            pass
    if hasattr(value, "date") and not isinstance(value, date):
        try:
            value = value.date()
        except Exception:
            pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _symbol_for_yfinance(security: Security) -> str:
    symbol = security.symbol.strip()
    if security.market == "hk" and symbol.isdigit() and "." not in symbol:
        return symbol.zfill(4) + ".HK"
    if security.market == "cn" and symbol.isdigit() and "." not in symbol:
        if symbol.startswith(("6", "68", "5")):
            return symbol + ".SS"
        return symbol + ".SZ"
    return symbol


def _record_from_mapping(item: dict[str, Any], security: Security, target: date, source: str) -> Quote | None:
    session_date = _date(item.get("session_date") or item.get("date")) or target
    close = _price(item.get("close"))
    previous_raw = item.get("previous_close")
    if previous_raw is None:
        previous_raw = item.get("prev_close")
    previous_close = _price(previous_raw)
    if close is None or previous_close is None:
        return None
    return Quote(
        symbol=security.symbol,
        name=security.name,
        market=security.market,
        subsector=security.subsector,
        session_date=session_date,
        close=close,
        previous_close=previous_close,
        volume=_float(item.get("volume")),
        average_volume_20=_float(item.get("average_volume_20") or item.get("avg_volume_20")),
        source=source,
        status=str(item.get("status") or "ok"),
        error=item.get("error"),
    )


def _fixture_rows(fixture: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = fixture.get(key, [])
    return value if isinstance(value, list) else []


def _fixture_quote(security: Security, target: date, fixture: dict[str, Any], *, benchmark: bool = False) -> Quote | None:
    key = "benchmarks" if benchmark else "quotes"
    for item in _fixture_rows(fixture, key):
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol", "")).upper() != security.symbol.upper():
            continue
        if str(item.get("market", security.market)).lower() != security.market:
            continue
        row_date = _date(item.get("session_date") or item.get("date"))
        if row_date and row_date != target:
            continue
        return _record_from_mapping(item, security, target, "fixture")
    return None


def _normalise_dataframe(df: Any, security: Security, target: date, source: str) -> Quote | None:
    """Read common yfinance/AkShare/DSA DataFrame shapes without asserting a schema."""

    if df is None or getattr(df, "empty", True):
        return None
    try:
        frame = df.copy()
        frame.columns = [str(c).strip().lower().replace(" ", "_") for c in frame.columns]

        # DSA/AkShare commonly expose the bar date as a column while yfinance
        # puts it in a DatetimeIndex.  A RangeIndex has a dtype too, so using
        # ``index.dtype`` as the discriminator silently discarded every row in
        # the former shape.  Prefer explicit date columns and only then inspect
        # the index.
        date_key = next(
            (
                key
                for key in ("date", "日期", "交易日期", "datetime", "时间", "timestamp")
                if key in frame.columns
            ),
            None,
        )
        if date_key:
            frame["__date"] = [_date(v) for v in frame[date_key]]
        else:
            frame["__date"] = [_date(v) for v in frame.index]

        # Keep the full history for the previous close and 20-session volume
        # baseline, then select the requested completed session.
        history = frame.sort_values("__date").dropna(subset=["__date"])
        selected = history[history["__date"] == target]
        if selected.empty:
            return None
        row = selected.iloc[-1]
        prior = history[history["__date"] < target]
        previous = prior.iloc[-1] if not prior.empty else None
        # Price-change attribution uses the unadjusted closing price.  Do not
        # silently substitute Adj Close, Low/High, or a stale prior close.
        close_key = next((k for k in ("close", "收盘", "收盘价") if k in row.index), None)
        volume_key = next((k for k in ("volume", "成交量", "vol") if k in row.index), None)
        if close_key is None:
            return None
        close = _price(row[close_key])
        previous_close = _price(previous[close_key]) if previous is not None else None
        if close is None or previous_close is None:
            return None
        volumes = prior[volume_key].tail(20).tolist() if volume_key else []
        avg_volume = _float(sum(_float(v) or 0 for v in volumes) / len(volumes)) if volumes else None
        return Quote(
            symbol=security.symbol,
            name=security.name,
            market=security.market,
            subsector=security.subsector,
            session_date=target,
            close=close,
            previous_close=previous_close,
            volume=_float(row[volume_key]) if volume_key else None,
            average_volume_20=avg_volume,
            source=source,
            status="ok",
        )
    except Exception:
        return None


class MarketDataProvider:
    """Provider facade. The host DSA manager is preferred when available."""

    def __init__(self, config: HealthcareConfig) -> None:
        self.config = config
        self._manager = None
        self._manager_cls = None
        try:
            from data_provider.base import DataFetcherManager  # type: ignore

            self._manager_cls = DataFetcherManager
            self._manager = DataFetcherManager()
        except Exception:
            self._manager = None

    def _max_workers(self) -> int:
        try:
            return max(1, min(int(self.config.provider.get("max_workers", 8)), 32))
        except (TypeError, ValueError):
            return 8

    def _timeout_seconds(self) -> float:
        try:
            return max(1.0, float(self.config.provider.get("timeout_seconds", 20)))
        except (TypeError, ValueError):
            return 20.0

    def fetch(
        self,
        securities: Iterable[Security],
        sessions: dict[str, Session],
        fixture: dict[str, Any] | None = None,
        *,
        offline: bool = False,
    ) -> MarketDataResult:
        fixture = fixture or {}
        securities = tuple(securities)
        # A representative pool is small, but the production configuration can
        # contain hundreds of authorised tickers.  A single yfinance download
        # is much cheaper and less rate-limit-prone than one HTTP request per
        # ticker; host/secondary fallbacks are then bounded below.
        workers = self._max_workers()
        timeout = self._timeout_seconds()
        statuses: list[SourceStatus] = []
        gaps: list[str] = []
        quotes_by_symbol: dict[str, Quote] = {}
        remaining: list[Security] = []
        for security in securities:
            target = sessions[security.market].session_date
            quote = _fixture_quote(security, target, fixture)
            if quote is not None:
                quotes_by_symbol[security.symbol.upper()] = quote
            else:
                remaining.append(security)

        # An explicit fixture is an offline contract, not merely a cache.  It
        # must never trigger a live request for a missing row.
        if not offline and remaining:
            quotes_by_symbol.update(
                self._fetch_yfinance_batch(remaining, sessions, workers=workers, timeout=timeout)
            )

        # Reuse DSA's canonical provider for rows not returned by the batch
        # path.  Calls are bounded and each worker gets its own manager when
        # possible, avoiding a global lock that serializes a 500-name pool.
        unresolved = [s for s in remaining if s.symbol.upper() not in quotes_by_symbol]
        if not offline and unresolved and self._manager_cls is not None:
            def host_one(security: Security) -> tuple[str, Quote | None]:
                return security.symbol.upper(), self._fetch_host(security, sessions[security.market].session_date)

            with ThreadPoolExecutor(
                max_workers=min(workers, len(unresolved)),
                thread_name_prefix="healthcare-host",
            ) as pool:
                for symbol, quote in pool.map(host_one, unresolved):
                    if quote is not None:
                        quotes_by_symbol[symbol] = quote

        # Single-ticker yfinance and AkShare are deliberately last-resort
        # paths for symbols that batch/host providers did not cover.
        unresolved = [s for s in remaining if s.symbol.upper() not in quotes_by_symbol]
        if not offline and unresolved:
            def fallback_one(security: Security) -> tuple[str, Quote | None]:
                target = sessions[security.market].session_date
                quote = self._fetch_yfinance(security, target)
                if quote is None and security.market in {"cn", "hk"}:
                    quote = self._fetch_akshare(security, target)
                return security.symbol.upper(), quote

            with ThreadPoolExecutor(
                max_workers=min(workers, len(unresolved)),
                thread_name_prefix="healthcare-fallback",
            ) as pool:
                for symbol, quote in pool.map(fallback_one, unresolved):
                    if quote is not None:
                        quotes_by_symbol[symbol] = quote

        quotes: list[Quote] = []
        for security in securities:
            target = sessions[security.market].session_date
            quote = quotes_by_symbol.get(security.symbol.upper())
            if quote is None:
                quote = Quote(
                    symbol=security.symbol,
                    name=security.name,
                    market=security.market,
                    subsector=security.subsector,
                    session_date=target,
                    close=None,
                    previous_close=None,
                    source="none",
                    status="no_data",
                    error="no provider returned a completed bar for the aligned session",
                )
            quotes.append(quote)
        for quote in quotes:
            if quote.status != "ok" or quote.close is None:
                detail = "；AkShare不支持 .BJ" if quote.market == "cn" and quote.symbol.upper().endswith(".BJ") else ""
                gaps.append(f"行情缺口：{quote.symbol} {quote.session_date.isoformat()}{detail}")

        benchmark_quotes: list[Quote] = []
        for market, session in sessions.items():
            symbol = self.config.benchmark_for(market)
            benchmark = Security(symbol, symbol, market)
            row = _fixture_quote(benchmark, session.session_date, fixture, benchmark=True)
            if row is None and not offline:
                row = self._fetch_yfinance_batch(
                    (benchmark,), {market: session}, workers=workers, timeout=timeout
                ).get(benchmark.symbol.upper())
            if row is None and not offline:
                row = self._fetch_host(benchmark, session.session_date) or self._fetch_yfinance(
                    benchmark, session.session_date, timeout=timeout
                )
            if row is None and not offline and market in {"cn", "hk"}:
                row = self._fetch_akshare(benchmark, session.session_date)
            if row is not None:
                benchmark_quotes.append(row)
            else:
                gaps.append(f"基准缺口：{market} {symbol} {session.session_date.isoformat()}")

        if securities:
            statuses.append(SourceStatus("market_data", "ok" if any(q.status == "ok" for q in quotes) else "no_data", sum(q.status == "ok" for q in quotes)))
        else:
            statuses.append(SourceStatus("market_data", "no_data", 0, "selected session has no securities"))
        return MarketDataResult(tuple(quotes), tuple(benchmark_quotes), tuple(statuses), tuple(gaps))

    def _fetch_host(self, security: Security, target: date) -> Quote | None:
        manager = self._manager
        if manager is None:
            return None
        # The host's stock router does not recognise Yahoo's Hong Kong index
        # notation.  Never send it through A-share stock fallbacks (^HSI.SZ).
        if security.market == "hk" and security.symbol.startswith("^"):
            return None
        try:
            # DataFetcherManager is mostly stateless at this boundary, but a
            # thread-local manager avoids sharing mutable circuit-breakers if
            # the host exposes the class.  A test-injected ``_manager`` remains
            # usable for deterministic unit tests.
            import threading

            if self._manager_cls is not None and threading.current_thread() is not threading.main_thread():
                local_manager = getattr(threading.current_thread(), "_healthcare_manager", None)
                if local_manager is None:
                    local_manager = self._manager_cls()
                    setattr(threading.current_thread(), "_healthcare_manager", local_manager)
                manager = local_manager
            start = (target - timedelta(days=45)).isoformat()
            end = (target + timedelta(days=1)).isoformat()
            host_symbol = "sh000300" if security.market == "cn" and security.symbol.upper() == "000300.SS" else security.symbol
            frame, provider = manager.get_daily_data(
                host_symbol,
                start_date=start,
                end_date=end,
                days=45,
            )
            return _normalise_dataframe(frame, security, target, str(provider or "dsa"))
        except Exception:
            return None

    def _fetch_yfinance_batch(
        self,
        securities: Iterable[Security],
        sessions: dict[str, Session],
        *,
        workers: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Quote]:
        """Fetch all same-window yfinance bars in one bounded request.

        yfinance's MultiIndex shape has changed between releases; this method
        intentionally normalises each per-ticker frame through the same
        tested path as single-ticker fallback and returns only exact session
        matches.
        """

        securities = tuple(securities)
        if not securities:
            return {}
        workers = workers or self._max_workers()
        timeout = timeout or self._timeout_seconds()
        try:
            import yfinance as yf  # type: ignore

            by_market: dict[tuple[str, date], list[Security]] = {}
            for security in securities:
                by_market.setdefault((security.market, sessions[security.market].session_date), []).append(security)
            result: dict[str, Quote] = {}
            for (market, target), group in by_market.items():
                symbols = [_symbol_for_yfinance(security) for security in group]
                frame = yf.download(
                    tickers=symbols,
                    start=(target - timedelta(days=45)).isoformat(),
                    end=(target + timedelta(days=1)).isoformat(),
                    auto_adjust=False,
                    group_by="ticker",
                    threads=int(workers),
                    progress=False,
                    timeout=timeout,
                )
                for security, yf_symbol in zip(group, symbols):
                    ticker_frame = frame
                    columns = getattr(frame, "columns", ())
                    if getattr(columns, "nlevels", 1) > 1:
                        # Current yfinance uses (Ticker, Field) with group_by
                        # ticker, while older builds may use (Field, Ticker).
                        try:
                            if yf_symbol in columns.get_level_values(0):
                                ticker_frame = frame[yf_symbol]
                            elif yf_symbol in columns.get_level_values(1):
                                ticker_frame = frame.xs(yf_symbol, axis=1, level=1)
                        except Exception:
                            ticker_frame = frame
                    quote = _normalise_dataframe(ticker_frame, security, target, "yfinance_batch")
                    if quote is not None:
                        result[security.symbol.upper()] = quote
            return result
        except Exception:
            return {}

    def _fetch_yfinance(self, security: Security, target: date, *, timeout: float | None = None) -> Quote | None:
        try:
            import yfinance as yf  # type: ignore

            frame = yf.Ticker(_symbol_for_yfinance(security)).history(
                start=(target - timedelta(days=45)).isoformat(),
                end=(target + timedelta(days=1)).isoformat(),
                auto_adjust=False,
                timeout=timeout or self._timeout_seconds(),
            )
            return _normalise_dataframe(frame, security, target, "yfinance")
        except Exception:
            return None

    def _fetch_akshare(self, security: Security, target: date) -> Quote | None:
        raw_symbol = security.symbol.strip().upper()
        try:
            import akshare as ak  # type: ignore
        except Exception:
            return None

        start_date = (target - timedelta(days=45)).strftime("%Y%m%d")
        end_date = target.strftime("%Y%m%d")
        if security.market == "cn":
            suffix = ""
            if "." in raw_symbol:
                raw_symbol, suffix = raw_symbol.rsplit(".", 1)
                suffix = "." + suffix
            if suffix == ".BJ" or not raw_symbol.isdigit():
                return None
            ak_symbol = raw_symbol.zfill(6)
            try:
                frame = ak.stock_zh_a_hist(
                    symbol=ak_symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                )
            except Exception:
                frame = None
            quote = _normalise_dataframe(frame, security, target, "akshare")
            if quote is not None:
                return quote

            # Eastmoney's hist endpoint can disconnect or omit the requested
            # session.  The daily endpoint uses the exchange-prefixed symbol
            # and has been verified for the same target-day/close semantics.
            exchange_prefix = "sh" if suffix in {".SS", ".SH"} or (not suffix and raw_symbol.startswith(("5", "6"))) else "sz"
            try:
                frame = ak.stock_zh_a_daily(
                    symbol=exchange_prefix + ak_symbol,
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                )
            except Exception:
                return None
            return _normalise_dataframe(frame, security, target, "akshare")

        if security.market == "hk":
            if "." in raw_symbol:
                raw_symbol = raw_symbol.rsplit(".", 1)[0]
            if not raw_symbol.isdigit():
                return None
            try:
                frame = ak.stock_hk_daily(symbol=raw_symbol.zfill(5), adjust="")
            except Exception:
                return None
            return _normalise_dataframe(frame, security, target, "akshare")

        return None
