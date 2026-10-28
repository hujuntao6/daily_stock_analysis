"""Read-only CNInfo and HKEXnews announcement adapters.

The two exchange sites do not expose a documented, versioned public REST API.
This module therefore talks to the JSON endpoints used by their official web
front ends, validates the exact HTTPS host after redirects, and fails closed
when a response is not an official source.  It intentionally returns plain
JSON-compatible dictionaries so the host report can decide how to turn an
announcement into :class:`~src.healthcare.models.Evidence`.
"""

from __future__ import annotations

import html
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from .models import Security


CNINFO_STOCKS_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_BASE = "https://static.cninfo.com.cn/"

HKEX_STOCKS_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
HKEX_QUERY_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEX_PDF_BASE = "https://www1.hkexnews.hk"

_CNINFO_HOSTS = {"www.cninfo.com.cn", "static.cninfo.com.cn"}
_HKEX_HOSTS = {"www1.hkexnews.hk"}
_USER_AGENT = "daily-stock-analysis-pharma-ah/1.0"
_MAX_RETRIES = 2  # one retry after the initial request
_MAX_BODY_CHARS = 20_000

_CACHE_LOCK = threading.Lock()
_MAPPING_CACHE: dict[str, dict[str, dict[str, str]]] = {}

# Tests replace this factory with a fixture session.  Keeping the factory
# module-local also prevents a caller from injecting a session through the
# public function and accidentally carrying cookies across sources.
_SESSION_FACTORY: Callable[[], Any] = requests.Session


class _SourceError(RuntimeError):
    """A source request failed or violated the official-host policy."""


def clear_mapping_cache() -> None:
    """Clear in-memory issuer mappings (useful for isolated offline tests)."""

    with _CACHE_LOCK:
        _MAPPING_CACHE.clear()


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _market(security: Security) -> str:
    return str(_value(security, "market", "")).strip().lower()


def _symbol(security: Security) -> str:
    return str(_value(security, "symbol", "")).strip()


def _clean_text(value: Any, limit: int | None = None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def _safe_error(exc: BaseException) -> str:
    """Return a stable, non-sensitive status detail."""

    if isinstance(exc, _SourceError) and str(exc):
        return str(exc)[:200]
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code:
        return f"{type(exc).__name__} (HTTP {code})"
    return type(exc).__name__


def _exact_https(url: str, hosts: set[str]) -> str:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or host not in hosts or parsed.username or parsed.password or parsed.port:
        raise _SourceError("official HTTPS host validation failed")
    return str(url)


def _validate_response_url(response: Any, requested_url: str, hosts: set[str]) -> None:
    final_url = str(getattr(response, "url", "") or requested_url)
    _exact_https(final_url, hosts)


def _raise_for_status(response: Any) -> None:
    try:
        response.raise_for_status()
    except AttributeError:
        status = int(getattr(response, "status_code", 200) or 200)
        if status >= 400:
            raise _SourceError(f"HTTP {status}")


def _request(session: Any, method: str, url: str, hosts: set[str], timeout: float, **kwargs: Any) -> Any:
    """Issue an allow-listed request and retry transient failures once."""

    _exact_https(url, hosts)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", _USER_AGENT)
    last: BaseException | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            fn = getattr(session, method.lower())
            response = fn(url, timeout=timeout, headers=headers, **kwargs)
            _validate_response_url(response, url, hosts)
            _raise_for_status(response)
            return response
        except (requests.RequestException, TimeoutError, _SourceError) as exc:
            last = exc
            if attempt + 1 >= _MAX_RETRIES:
                break
    if isinstance(last, _SourceError):
        raise last
    raise _SourceError(_safe_error(last or RuntimeError("request failed"))) from last


def _response_json(response: Any) -> Any:
    try:
        return response.json()
    except (AttributeError, ValueError, json.JSONDecodeError):
        raw = getattr(response, "text", "")
        if not raw:
            content = getattr(response, "content", b"")
            raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
        return json.loads(raw)


def _response_bytes(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    text = getattr(response, "text", "")
    return str(text).encode("utf-8", errors="replace")


def _new_session() -> Any:
    return _SESSION_FACTORY()


def _normalise_cn_code(symbol: str) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", symbol)
    if match:
        return match.group(1)
    digits = "".join(re.findall(r"\d", symbol))
    return digits[:6] if len(digits) >= 6 else ""


def _normalise_hk_code(symbol: str) -> str:
    digits = "".join(re.findall(r"\d", symbol))
    return digits[-5:].zfill(5) if digits else ""


def _load_cninfo_mapping(session: Any, timeout: float) -> dict[str, dict[str, str]]:
    with _CACHE_LOCK:
        cached = _MAPPING_CACHE.get("cninfo")
    if cached is not None:
        return cached
    response = _request(session, "GET", CNINFO_STOCKS_URL, _CNINFO_HOSTS, timeout)
    payload = _response_json(response)
    rows = payload.get("stockList", []) if isinstance(payload, Mapping) else []
    mapping: dict[str, dict[str, str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        code = _normalise_cn_code(str(row.get("code", "")))
        org_id = str(row.get("orgId", "")).strip()
        if code and org_id:
            mapping[code] = {"code": code, "orgId": org_id, "name": _clean_text(row.get("zwjc"))}
    with _CACHE_LOCK:
        _MAPPING_CACHE["cninfo"] = mapping
    return mapping


def _load_hkex_mapping(session: Any, timeout: float) -> dict[str, dict[str, str]]:
    with _CACHE_LOCK:
        cached = _MAPPING_CACHE.get("hkex")
    if cached is not None:
        return cached
    response = _request(session, "GET", HKEX_STOCKS_URL, _HKEX_HOSTS, timeout)
    payload = _response_json(response)
    rows = payload if isinstance(payload, list) else []
    mapping: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        code = _normalise_hk_code(str(row.get("c", "")))
        stock_id = str(row.get("i", "")).strip()
        if code and stock_id:
            mapping[code] = {"code": code, "stockId": stock_id, "name": _clean_text(row.get("n"))}
    with _CACHE_LOCK:
        _MAPPING_CACHE["hkex"] = mapping
    return mapping


def _as_of(value: Any) -> tuple[datetime, date, bool]:
    """Return UTC-ish datetime, local-independent date, and time precision."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value, value.date(), True
    if isinstance(value, date):
        return datetime.combine(value, time.max, tzinfo=timezone.utc), value, False
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = date.fromisoformat(text)
        return datetime.combine(parsed, time.max, tzinfo=timezone.utc), parsed, False
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, parsed.date(), True


def _cn_published(value: Any) -> tuple[str, str, datetime | None]:
    """CNInfo timestamps are often date-only epoch values."""

    if isinstance(value, (int, float)) or str(value or "").isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
        if local.hour == local.minute == local.second == local.microsecond == 0:
            return local.date().isoformat(), "date", local
        local = local.replace(second=0, microsecond=0)
        return local.isoformat(), "minute", local
    text = _clean_text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = date.fromisoformat(text)
        return text, "date", datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=ZoneInfo("Asia/Shanghai"), second=0, microsecond=0)
            return local.isoformat(), "minute", local
        except ValueError:
            continue
    return "", "unknown", None


def _hkex_published(value: Any) -> tuple[str, str, datetime | None]:
    text = _clean_text(value)
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=ZoneInfo("Asia/Hong_Kong"), second=0, microsecond=0)
            return local.isoformat(), "minute", local
        except ValueError:
            continue
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed = date.fromisoformat(text)
        return text, "date", datetime.combine(parsed, time.min, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    return "", "unknown", None


def _in_as_of(
    event_dt: datetime | None,
    event_date: date | None,
    precision: str,
    as_of_dt: datetime,
    as_of_date: date,
    has_time: bool,
) -> bool:
    if event_dt is None or event_date is None:
        return False
    if event_date > as_of_date:
        return False
    if precision == "date" or not has_time:
        return True
    if event_dt.tzinfo is None:
        return event_dt.date() <= as_of_date
    return event_dt <= as_of_dt.astimezone(event_dt.tzinfo)


def _extract_pdf_text(content: bytes) -> tuple[str | None, str | None]:
    """Extract bounded text when pypdf is installed; never invent body text."""

    if len(content) > 12 * 1024 * 1024:
        return None, "pdf_too_large"
    if not content.lstrip().startswith(b"%PDF"):
        return None, "not_pdf"
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None, "pypdf_unavailable"
    try:
        reader = PdfReader(BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        text = _clean_text(text, _MAX_BODY_CHARS)
        return (text or None), (None if text else "pdf_text_empty")
    except Exception:
        return None, "pdf_parse_failed"


def _pdf_event(
    *,
    security: Security,
    source_kind: str,
    publisher: str,
    source_id: str,
    title: str,
    published_at: str,
    published_precision: str,
    published_dt: datetime,
    url: str,
    landing_url: str,
    metadata: Mapping[str, Any],
    query: Mapping[str, Any],
    session: Any,
    hosts: set[str],
    timeout: float,
    as_of_dt: datetime,
    as_of_date: date,
    as_of_has_time: bool,
) -> dict[str, Any] | None:
    if not _in_as_of(published_dt, published_dt.date(), published_precision, as_of_dt, as_of_date, as_of_has_time):
        return None
    try:
        response = _request(session, "GET", url, hosts, timeout)
        content = _response_bytes(response)
        body_text, body_error = _extract_pdf_text(content)
    except _SourceError as exc:
        body_text, body_error = None, _safe_error(exc)
    raw: dict[str, Any] = {
        "metadata": dict(metadata),
        "query": dict(query),
        "source_url": url,
        "landing_url": landing_url,
        "body_text": body_text,
        "body_unavailable": body_text is None,
    }
    if body_error:
        raw["body_error"] = body_error
    snippet = body_text[:2000] if body_text else title
    return {
        "symbol": _symbol(security),
        "url": url,
        "landing_url": landing_url,
        "publisher": publisher,
        "published_at": published_at,
        "published_precision": published_precision,
        "title": title,
        "snippet": snippet[:2000],
        "body_unavailable": body_text is None,
        "source_kind": source_kind,
        "source_id": source_id,
        "raw": raw,
    }


def _cninfo_for_security(
    security: Security,
    mapping: Mapping[str, Mapping[str, str]],
    as_of_dt: datetime,
    as_of_date: date,
    as_of_has_time: bool,
    lookback_days: int,
    timeout: float,
    max_per_security: int,
) -> tuple[list[dict[str, Any]], str]:
    code = _normalise_cn_code(_symbol(security))
    issuer = mapping.get(code)
    if not issuer:
        return [], "official stock mapping not found"
    start = as_of_date - timedelta(days=max(0, lookback_days - 1))
    query: dict[str, Any] = {
        "pageNum": 1,
        "pageSize": max(1, max_per_security),
        "column": "sse" if str(issuer.get("orgId", "")).startswith("gssh") else "szse",
        "tabName": "fulltext",
        "plate": "sse" if str(issuer.get("orgId", "")).startswith("gssh") else "szse",
        "stock": f"{issuer['code']},{issuer['orgId']}",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start.isoformat()}~{as_of_date.isoformat()}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    session = _new_session()
    try:
        response = _request(session, "POST", CNINFO_QUERY_URL, _CNINFO_HOSTS, timeout, data=query)
        payload = _response_json(response)
    except _SourceError as exc:
        return [], f"error: {_safe_error(exc)}"
    rows = payload.get("announcements") if isinstance(payload, Mapping) else None
    events: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return [], "no announcement rows"
    for row in rows:
        if len(events) >= max_per_security or not isinstance(row, Mapping):
            break
        source_id = str(row.get("announcementId", "")).strip()
        adjunct = str(row.get("adjunctUrl", "")).strip()
        if not source_id or not adjunct:
            continue
        published_at, precision, published_dt = _cn_published(row.get("announcementTime"))
        title = _clean_text(row.get("announcementTitle") or row.get("shortTitle"))
        if not published_at or not title or published_dt is None:
            continue
        url = adjunct if adjunct.startswith("https://") else CNINFO_PDF_BASE + adjunct.lstrip("/")
        try:
            _exact_https(url, _CNINFO_HOSTS)
        except _SourceError:
            continue
        landing = (
            "https://www.cninfo.com.cn/new/disclosure/detail?"
            f"stockCode={issuer['code']}&orgId={issuer['orgId']}&announcementId={source_id}"
            f"&announcementTime={published_at[:10]}"
        )
        event = _pdf_event(
            security=security,
            source_kind="cninfo",
            publisher="巨潮资讯网",
            source_id=source_id,
            title=title,
            published_at=published_at,
            published_precision=precision,
            published_dt=published_dt,
            url=url,
            landing_url=landing,
            metadata=row,
            query=query,
            session=session,
            hosts=_CNINFO_HOSTS,
            timeout=timeout,
            as_of_dt=as_of_dt,
            as_of_date=as_of_date,
            as_of_has_time=as_of_has_time,
        )
        if event:
            events.append(event)
    return events, "ok" if events else "no usable announcement rows"


def _hkex_for_security(
    security: Security,
    mapping: Mapping[str, Mapping[str, str]],
    as_of_dt: datetime,
    as_of_date: date,
    as_of_has_time: bool,
    lookback_days: int,
    timeout: float,
    max_per_security: int,
) -> tuple[list[dict[str, Any]], str]:
    code = _normalise_hk_code(_symbol(security))
    issuer = mapping.get(code)
    if not issuer:
        return [], "official stock mapping not found"
    start = as_of_date - timedelta(days=max(0, lookback_days - 1))
    query: dict[str, Any] = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": issuer["stockId"],
        "documentType": "-1",
        "fromDate": start.strftime("%Y%m%d"),
        "toDate": as_of_date.strftime("%Y%m%d"),
        "title": "",
        "searchType": "0",
        # -2 is the HKEX Title Search UI's all-category value.  40000 is
        # Financial Statements/ESG Information and silently drops clinical,
        # licensing, BD and other announcement titles.
        "t1code": "-2",
        "t2Gcode": "-2",
        "t2code": "-2",
        "rowRange": max(1, max_per_security),
        "lang": "E",
    }
    session = _new_session()
    try:
        response = _request(session, "GET", HKEX_QUERY_URL, _HKEX_HOSTS, timeout, params=query)
        payload = _response_json(response)
        result = payload.get("result", "[]") if isinstance(payload, Mapping) else "[]"
        rows = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError, _SourceError) as exc:
        return [], f"error: {_safe_error(exc)}"
    if not isinstance(rows, list):
        return [], "no announcement rows"
    events: list[dict[str, Any]] = []
    for row in rows:
        if len(events) >= max_per_security or not isinstance(row, Mapping):
            break
        source_id = _clean_text(row.get("NEWS_ID"))
        file_link = _clean_text(row.get("FILE_LINK"))
        if not source_id or not file_link or file_link.lower().endswith(".zip"):
            continue
        published_at, precision, published_dt = _hkex_published(row.get("DATE_TIME"))
        title = _clean_text(row.get("TITLE"))
        if not published_at or not title or published_dt is None:
            continue
        url = file_link if file_link.startswith("https://") else HKEX_PDF_BASE + "/" + file_link.lstrip("/")
        try:
            _exact_https(url, _HKEX_HOSTS)
        except _SourceError:
            continue
        landing = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en&stockId=" + issuer["stockId"]
        event = _pdf_event(
            security=security,
            source_kind="hkex",
            publisher="HKEXnews",
            source_id=source_id,
            title=title,
            published_at=published_at,
            published_precision=precision,
            published_dt=published_dt,
            url=url,
            landing_url=landing,
            metadata=row,
            query=query,
            session=session,
            hosts=_HKEX_HOSTS,
            timeout=timeout,
            as_of_dt=as_of_dt,
            as_of_date=as_of_date,
            as_of_has_time=as_of_has_time,
        )
        if event:
            events.append(event)
    return events, "ok" if events else "no usable announcement rows"


def _status(source: str, status: str, count: int, detail: str, symbol: str) -> dict[str, Any]:
    return {"source": source, "status": status, "count": int(count), "detail": str(detail)[:500], "symbol": symbol}


def collect_ah_announcements(
    securities: Iterable[Security],
    as_of: datetime | date | str,
    *,
    lookback_days: int = 7,
    timeout: float = 15,
    max_per_security: int = 3,
    max_workers: int = 4,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect recent A/H announcements from official CNInfo/HKEX sources.

    ``events`` are JSON-compatible announcement dictionaries.  ``statuses``
    always contains one row per requested A/H security and source, including
    mapping failures and no-data cases.  No event is created when the source
    has no direct original document URL or when its publication time is after
    ``as_of``.
    """

    security_list = [item for item in securities if _symbol(item)]
    if not security_list or max_per_security <= 0:
        return [], [_status("ah", "no_data", 0, "empty universe or max_per_security <= 0", _symbol(item)) for item in security_list]
    as_of_dt, as_of_date, as_of_has_time = _as_of(as_of)
    lookback_days = max(1, int(lookback_days))
    timeout = max(0.1, float(timeout))
    max_per_security = max(1, int(max_per_security))
    max_workers = max(1, int(max_workers))

    # Deduplicate by market and symbol while preserving the first Security
    # object (which carries the host report's canonical symbol spelling).
    unique: dict[tuple[str, str], Security] = {}
    for item in security_list:
        unique.setdefault((_market(item), _symbol(item)), item)

    source_mappings: dict[str, Mapping[str, Mapping[str, str]]] = {}
    mapping_errors: dict[str, str] = {}
    for source, relevant in (("cninfo", "cn"), ("hkex", "hk")):
        if not any(market == relevant for market, _ in unique):
            continue
        session = _new_session()
        try:
            source_mappings[source] = (
                _load_cninfo_mapping(session, timeout) if source == "cninfo" else _load_hkex_mapping(session, timeout)
            )
        except _SourceError as exc:
            mapping_errors[source] = _safe_error(exc)

    jobs: list[tuple[str, Security]] = []
    for (market, _), item in unique.items():
        if market == "cn":
            jobs.append(("cninfo", item))
        elif market == "hk":
            jobs.append(("hkex", item))

    events: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    def run(source: str, item: Security) -> tuple[Security, str, list[dict[str, Any]], str]:
        if source in mapping_errors:
            return item, source, [], mapping_errors[source]
        mapping = source_mappings.get(source, {})
        if source == "cninfo":
            rows, detail = _cninfo_for_security(item, mapping, as_of_dt, as_of_date, as_of_has_time, lookback_days, timeout, max_per_security)
        else:
            rows, detail = _hkex_for_security(item, mapping, as_of_dt, as_of_date, as_of_has_time, lookback_days, timeout, max_per_security)
        return item, source, rows, detail

    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(jobs)))) as pool:
        futures = {pool.submit(run, source, item): (source, item) for source, item in jobs}
        for future in as_completed(futures):
            requested_source, requested_item = futures[future]
            source, item = "ah", None
            try:
                security, source, rows, detail = future.result()
                events.extend(rows)
                status_value = "ok" if rows else ("error" if source in mapping_errors or detail.startswith("error:") else "no_data")
                statuses.append(_status(source, status_value, len(rows), detail, _symbol(security)))
            except Exception as exc:  # defensive boundary: one issuer cannot stop the daily report
                statuses.append(_status(requested_source, "error", 0, _safe_error(exc), _symbol(requested_item)))

    covered = {(str(row.get("source_kind")), str(row.get("symbol"))) for row in events}
    for (market, symbol), item in unique.items():
        if market not in {"cn", "hk"}:
            statuses.append(_status("ah", "not_applicable", 0, "security market is outside CN/HK", symbol))
            continue
        source = "cninfo" if market == "cn" else "hkex"
        if not any(row.get("symbol") == symbol and row.get("source") == source for row in statuses):
            detail = mapping_errors.get(source, "official stock mapping not found")
            statuses.append(_status(source, "error" if source in mapping_errors else "no_data", 0, detail, symbol))

    events.sort(key=lambda row: (str(row.get("symbol", "")), str(row.get("source_kind", "")), str(row.get("published_at", ""))), reverse=True)
    statuses.sort(key=lambda row: (str(row.get("symbol", "")), str(row.get("source", ""))))
    return events, statuses


__all__ = ["collect_ah_announcements", "clear_mapping_cache"]
