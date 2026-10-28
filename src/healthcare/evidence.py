"""Authoritative evidence collection and URL provenance checks.

The collector deliberately fails closed for URLs outside an explicitly allowed
official domain. Search result pages and redirect destinations are never
accepted as evidence.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .config import HealthcareConfig
from .models import Evidence, Security, SourceStatus


def _forward_event(text: str, as_of: date) -> tuple[date, date | None, str, str, str] | None:
    """Extract only an explicitly forward-looking date from source text."""

    from .event_extraction import extract_forward_event

    row = extract_forward_event(text, as_of)
    if row:
        return row.event_date, row.event_window_end, row.precision, row.label, "confirmed"
    return None


class EvidenceError(ValueError):
    pass


KNOWN_OFFICIAL_DOMAINS = {
    "sec.gov",
    "data.sec.gov",
    "clinicaltrials.gov",
    "fda.gov",
    "ema.europa.eu",
    "hematology.org",
    "sitcancer.org",
}


def _hostname(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise EvidenceError("evidence URL must use HTTPS and include a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise EvidenceError("evidence URL has an invalid port") from exc
    if parsed.username or parsed.password or port:
        raise EvidenceError("evidence URL may not include credentials or a custom port")
    return parsed.hostname.lower().rstrip(".")


def normalise_domain(domain: str) -> str:
    text = str(domain or "").strip().lower().rstrip(".")
    text = text.removeprefix("https://").removeprefix("http://").split("/", 1)[0]
    return text.removeprefix("www.")


def is_authoritative_url(url: str, allowed_domains: Iterable[str]) -> bool:
    try:
        host = _hostname(url)
    except EvidenceError:
        return False
    allowed = {normalise_domain(d) for d in allowed_domains if normalise_domain(d)}
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def _parse_datetime(value: Any, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, IndexError):
                pass
    return default or datetime.now(timezone.utc)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10]) if value else None
    except ValueError:
        return None


def _parse_date_struct(value: Any) -> tuple[date | None, str | None]:
    """Read a ClinicalTrials.gov date struct without inventing precision."""

    if not isinstance(value, dict):
        parsed = _parse_date(value)
        return parsed, "day" if parsed else None
    text = str(value.get("date") or "").strip()
    if re.fullmatch(r"20\d{2}-\d{1,2}-\d{1,2}", text):
        parsed = _parse_date(text)
        return parsed, "day" if parsed else None
    if re.fullmatch(r"20\d{2}-\d{1,2}", text):
        year, month = (int(part) for part in text.split("-"))
        try:
            return date(year, month, 1), "month"
        except ValueError:
            return None, None
    year = value.get("year")
    if year:
        try:
            return date(int(year), 1, 1), "year"
        except (TypeError, ValueError):
            pass
    return None, None


def _evidence_id(url: str, published_at: datetime | None, identifiers: Iterable[str]) -> str:
    token = "|".join([url, published_at.isoformat() if published_at else "unknown", *sorted(str(x) for x in identifiers)])
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _date_only_timestamp(value: Any) -> datetime:
    """Represent a date-only official timestamp conservatively.

    Midnight would incorrectly make a date-only filing appear pre-close in
    some markets.  End-of-day UTC keeps the date while ensuring attribution
    requires an actual intraday timestamp before calling an event same-day.
    """

    parsed = _parse_date(value)
    if parsed is None:
        return datetime.now(timezone.utc)
    return datetime.combine(parsed, datetime.max.time(), tzinfo=timezone.utc)


def _clean_html_snippet(text: str, limit: int = 2000) -> str:
    text = html.unescape(re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S))
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _parse_published(value: Any) -> tuple[datetime | None, str]:
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "null", "none", "n/a", "na", "不详", "未知"}:
        return None, "unknown"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        if _parse_date(text) is None:
            return None, "unknown"
        return _date_only_timestamp(text), "date"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None, "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, "timestamp"


def _parse_web_published(value: Any) -> tuple[datetime | None, str]:
    """Parse publication metadata without inventing a time zone or time.

    Investor-relations JSON and HTML commonly expose ``2026-10-27`` or a
    naive ISO value.  Those values are useful as calendar dates, but treating
    them as UTC timestamps would create a false intraday attribution.  Only
    an explicit offset (or an RFC-2822 zone) is promoted to ``timestamp``.
    """

    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "null", "none", "n/a", "na", "不详", "未知"}:
        return None, "unknown"
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
        parsed = _parse_date(text)
        return (_date_only_timestamp(parsed), "date") if parsed else (None, "unknown")
    if re.fullmatch(r"20\d{2}[年/-]\d{1,2}[月/-]\d{1,2}日?", text):
        parsed = _parse_date(re.sub(r"年|月", "-", text).replace("日", "").replace("/", "-"))
        return (_date_only_timestamp(parsed), "date") if parsed else (None, "unknown")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return _date_only_timestamp(parsed.date()), "date"
        return parsed, "timestamp"
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            return _date_only_timestamp(parsed.date()), "date"
        return parsed, "timestamp"
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in (
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%m.%d.%Y",
        "%m/%d/%Y",
        "%m-%d-%Y",
    ):
        try:
            parsed = datetime.strptime(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I), fmt).date()
            return _date_only_timestamp(parsed), "date"
        except ValueError:
            continue
    return None, "unknown"


class _IRHTMLParser(HTMLParser):
    """Small dependency-free parser for official IR pages.

    It intentionally drops navigation, footer, scripts and style blocks.  It
    is not a general HTML sanitizer; it only provides enough structure to
    identify an official detail page, its links and publication metadata.
    """

    _SUPPRESSED = {"aside", "footer", "form", "header", "nav", "noscript", "script", "style", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.anchors: list[tuple[str, str]] = []
        self.times: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self._visible: list[str] = []
        self._title: list[str] = []
        self._heading: list[str] = []
        self._title_depth = 0
        self._heading_depth = 0
        self._suppressed: list[str] = []
        self._script_json = False
        self._script_buffer: list[str] = []
        self._anchor_stack: list[dict[str, Any]] = []
        self._time_stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {str(key).lower(): (value or "") for key, value in attrs}
        layout = f"{attr.get('id', '')} {attr.get('class', '')}".lower()
        layout_container = bool(re.search(r"(?:^|[\s_-])(footer|site-footer|navbar|navigation|cookie)(?:$|[\s_-])", layout))
        if tag == "script":
            self._script_json = "ld+json" in attr.get("type", "").lower()
            self._script_buffer = []
            self._suppressed.append(tag)
            return
        if self._suppressed:
            if tag in self._SUPPRESSED or layout_container:
                self._suppressed.append(tag)
            return
        if tag in self._SUPPRESSED or layout_container:
            self._suppressed.append(tag)
            return
        if tag == "meta":
            key = attr.get("property") or attr.get("name") or attr.get("itemprop")
            content = attr.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key.lower(), []).append(content)
        elif tag == "a":
            self._anchor_stack.append({"href": attr.get("href") or attr.get("data-href") or attr.get("data-url") or "", "text": []})
        elif tag == "time":
            self._time_stack.append({"datetime": attr.get("datetime", ""), "text": []})
        elif tag == "title":
            self._title_depth += 1
        elif tag in {"h1", "h2"}:
            self._heading_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            if self._script_json and self._script_buffer:
                self.jsonld.append("".join(self._script_buffer))
            self._script_json = False
            self._script_buffer = []
            if self._suppressed and self._suppressed[-1] == tag:
                self._suppressed.pop()
            return
        if self._suppressed:
            if self._suppressed[-1] == tag:
                self._suppressed.pop()
            return
        if tag == "a" and self._anchor_stack:
            row = self._anchor_stack.pop()
            self.anchors.append((str(row.get("href") or ""), " ".join(row.get("text") or []).strip()))
        elif tag == "time" and self._time_stack:
            row = self._time_stack.pop()
            self.times.append((str(row.get("datetime") or ""), " ".join(row.get("text") or []).strip()))
        elif tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
        elif tag in {"h1", "h2"}:
            self._heading_depth = max(0, self._heading_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._script_json:
            self._script_buffer.append(data)
            return
        if self._suppressed:
            return
        text = data.strip()
        if not text:
            return
        self._visible.append(text)
        if self._title_depth:
            self._title.append(text)
        if self._heading_depth:
            self._heading.append(text)
        if self._anchor_stack:
            self._anchor_stack[-1]["text"].append(text)
        if self._time_stack:
            self._time_stack[-1]["text"].append(text)

    @property
    def visible_text(self) -> str:
        return " ".join(self._visible)

    @property
    def title_text(self) -> str:
        return " ".join(self._title).strip()

    @property
    def heading_text(self) -> str:
        return " ".join(self._heading).strip()


def _jsonld_objects(values: Iterable[str]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            objects.append(value)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for raw in values:
        try:
            walk(json.loads(html.unescape(raw)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return objects


def _first_json_value(rows: Iterable[dict[str, Any]], keys: Iterable[str]) -> Any:
    wanted = {str(key).lower() for key in keys}
    for row in rows:
        for key, value in row.items():
            if str(key).lower() in wanted and value not in (None, "", []):
                return value
    return None


def _publication_date_from_text(text: str) -> tuple[datetime | None, str]:
    pattern = re.compile(
        r"(?:published|posted|issued|publication\s+date|发布日期|发布时间|发布于)"
        r"\s*(?:on|at|date)?\s*[:：-]?\s*"
        r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|20\d{2}年\d{1,2}月\d{1,2}日?|"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+20\d{2})",
        re.IGNORECASE,
    )
    match = pattern.search(text or "")
    return _parse_web_published(match.group(1)) if match else (None, "unknown")


def _parse_ir_html(text: str) -> dict[str, Any]:
    parser = _IRHTMLParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # HTMLParser is forgiving; retain the data collected before malformed
        # markup rather than treating a publisher's page as an empty source.
        pass
    json_rows = _jsonld_objects(parser.jsonld)
    published_value = _first_json_value(json_rows, ("datePublished", "dateCreated"))
    title_value = _first_json_value(json_rows, ("headline", "name"))
    body_value = _first_json_value(json_rows, ("articleBody", "description"))
    for key in ("article:published_time", "datepublished", "date", "pubdate", "publishdate", "published"):
        if published_value in (None, "") and parser.meta.get(key):
            published_value = parser.meta[key][0]
    if published_value in (None, ""):
        for datetime_value, text_value in parser.times:
            published_value = datetime_value or text_value
            if published_value:
                break
    published, precision = _parse_web_published(published_value)
    if published is None:
        published, precision = _publication_date_from_text(parser.visible_text)
    title = str(title_value or "").strip() or parser.meta.get("og:title", [""])[0].strip()
    title = title or parser.heading_text or parser.title_text or "IR update"
    body = " ".join(part for part in (str(body_value or "").strip(), parser.visible_text) if part)
    body = _clean_html_snippet(body, limit=20_000)
    return {
        "title": _clean_html_snippet(title, limit=500),
        "body": body,
        "published": published,
        "published_precision": precision,
        "anchors": parser.anchors,
        "json_rows": json_rows,
    }


def _json_listing_candidates(payload: Any) -> list[dict[str, Any]]:
    """Find explicit detail-link rows in an official JSON listing response."""

    link_keys = ("url", "link", "href", "detailurl", "articleurl", "newsurl", "releaseurl")
    title_keys = ("title", "headline", "name", "subject")
    date_keys = ("datepublished", "published_at", "publisheddate", "publishdate", "published", "date", "releasedate")
    body_keys = ("description", "summary", "content", "body", "snippet", "excerpt")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def find(row: dict[str, Any], keys: Iterable[str]) -> Any:
        wanted = {key.lower().replace("_", "") for key in keys}
        for key, value in row.items():
            if str(key).lower().replace("_", "") in wanted and isinstance(value, (str, int, float)):
                return value
        return None

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            link = find(value, link_keys)
            title = find(value, title_keys)
            if link and title:
                link_text = str(link).strip()
                if link_text and link_text not in seen:
                    seen.add(link_text)
                    result.append(
                        {
                            "url": link_text,
                            "title": str(title).strip(),
                            "published_value": find(value, date_keys),
                            "body": str(find(value, body_keys) or "").strip(),
                        }
                    )
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return result


def _parse_sec_acceptance(value: Any) -> datetime:
    """Parse SEC acceptanceDateTime, whose legacy no-offset values are ET."""

    text = str(value or "").strip()
    if text and not re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", text, flags=re.I):
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.replace(tzinfo=ZoneInfo("America/New_York"))
        except ValueError:
            pass
    return _parse_datetime(value)


@dataclass
class EvidenceBundle:
    evidence: list[Evidence] = field(default_factory=list)
    statuses: list[SourceStatus] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    raw_archive: dict[str, str] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)

    def add(self, item: Evidence, raw: str | None = None) -> None:
        if not any(existing.id == item.id for existing in self.evidence):
            self.evidence.append(item)
        if raw:
            self.raw_archive[item.id] = raw[:200_000]


class EvidenceCollector:
    def __init__(self, config: HealthcareConfig, *, timeout: float = 15.0) -> None:
        self.config = config
        self.timeout = timeout

    def collect(
        self,
        securities: Iterable[Security],
        *,
        as_of: datetime,
        fixture: dict[str, Any] | None = None,
        offline: bool = False,
        focus_securities: Iterable[Security] | None = None,
    ) -> EvidenceBundle:
        bundle = EvidenceBundle()
        fixture = fixture or {}
        securities = tuple(securities)
        focus_securities = tuple(focus_securities or securities)
        fixture_rows = fixture.get("evidence", [])
        if isinstance(fixture_rows, list):
            self._collect_fixture(fixture_rows, bundle, securities, as_of_date=as_of.date())

        source_securities = focus_securities
        if not offline:
            for security in source_securities:
                if security.cik and self._enabled("sec"):
                    self._collect_sec(security, bundle, as_of_date=as_of.date())
                if security.ir_feeds and self._enabled("ir"):
                    self._collect_feeds(security, bundle, as_of_date=as_of.date())
                if security.nct_ids and self._enabled("clinicaltrials"):
                    for nct_id in security.nct_ids:
                        self._collect_clinicaltrial(security, nct_id, bundle)
                if security.sponsor_aliases and self._enabled("clinicaltrials_discovery"):
                    self._discover_clinicaltrials(security, as_of, bundle)
            self._collect_regulator_feeds(bundle)
        else:
            bundle.statuses.append(SourceStatus("live_evidence", "not_configured", 0, "fixture mode is offline"))
        if not offline:
            self._collect_calendar_files(bundle)
        if not offline:
            self._collect_ah_adapter(bundle, focus_securities, as_of=as_of)
        else:
            bundle.statuses.append(SourceStatus("ah_announcements", "not_configured", 0, "fixture mode is offline"))
        symbols = {security.symbol for security in securities}
        covered_symbols = {
            identifier
            for item in bundle.evidence
            for identifier in item.identifiers
            if identifier in symbols
        }
        bundle.coverage = {
            "requested_symbols": len(symbols),
            "covered_symbols": len(covered_symbols),
            "covered_symbol_list": sorted(covered_symbols),
            "uncovered_symbols": sorted(symbols - covered_symbols),
            "evidence_count": len(bundle.evidence),
            "focus_symbols": len({security.symbol for security in focus_securities}),
            "focus_covered_symbols": len(covered_symbols.intersection({security.symbol for security in focus_securities})),
        }
        if not bundle.evidence:
            bundle.gaps.append("未找到可通过官方域名校验的证据；报告不会伪造事件或催化剂")
        return bundle

    def _enabled(self, name: str) -> bool:
        value = self.config.source_policy.get(name, True)
        return bool(value)

    def _collect_fixture(
        self,
        rows: list[Any],
        bundle: EvidenceBundle,
        securities: Iterable[Security],
        *,
        as_of_date: date,
    ) -> None:
        by_symbol = {s.symbol.upper(): s for s in securities}
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol", "")).upper()
            security = by_symbol.get(symbol)
            allowed = tuple(security.official_domains) if security else tuple(KNOWN_OFFICIAL_DOMAINS)
            url = str(raw.get("url", ""))
            if not is_authoritative_url(url, allowed):
                bundle.gaps.append(f"证据拒绝：{url or '<empty url>'} 不在允许的官方域名内")
                continue
            published_at, published_precision = _parse_published(raw.get("published_at"))
            identifiers = tuple(str(x) for x in (raw.get("identifiers") or ()))
            event_date = _parse_date(raw.get("event_date"))
            event_end = _parse_date(raw.get("event_window_end"))
            snippet = str(raw.get("snippet") or "")[:2000]
            event_precision = raw.get("event_precision")
            event_label = raw.get("event_label")
            event_status = raw.get("event_status")
            if not event_date and str(raw.get("source_kind") or "").lower() in {"sec_filing", "sec_exhibit", "company_ir", "cninfo", "hkex", "ah_announcement"}:
                future = _forward_event(snippet, as_of_date)
                if future:
                    event_date, event_end, event_precision, event_label, event_status = future
            item = Evidence(
                id=str(raw.get("id") or _evidence_id(url, published_at, identifiers)),
                url=url,
                publisher=str(raw.get("publisher") or "官方来源"),
                published_at=published_at,
                retrieved_at=_parse_datetime(raw.get("retrieved_at")),
                snippet=snippet,
                identifiers=identifiers,
                source_kind=str(raw.get("source_kind") or "fixture"),
                event_date=event_date,
                event_precision=event_precision,
                event_label=event_label,
                event_window_end=event_end,
                event_status=event_status,
                published_precision=str(raw.get("published_precision") or published_precision),
            )
            bundle.add(item, raw=json.dumps(raw, ensure_ascii=False))
        bundle.statuses.append(SourceStatus("fixture", "ok", len(bundle.evidence)))

    def _fetch(self, url: str, allowed_domains: Iterable[str]) -> tuple[str, str]:
        if not is_authoritative_url(url, allowed_domains):
            raise EvidenceError(f"URL is outside authoritative domains: {url}")
        # A security's allow-list often contains both SEC and company IR
        # domains.  SEC's mandatory identification header applies to the
        # actual request host, not to every request whose allow-list happens
        # to include ``sec.gov``.
        request_host = _hostname(url)
        if request_host == "sec.gov" or request_host.endswith(".sec.gov"):
            user_agent = os.getenv("SEC_USER_AGENT", "").strip()
            if not user_agent:
                sender = os.getenv("EMAIL_SENDER", "").strip()
                user_agent = f"daily-stock-analysis/1.0 contact={sender}" if sender else ""
            if not user_agent:
                raise EvidenceError("SEC_USER_AGENT is required for SEC requests")
        else:
            user_agent = "daily-stock-analysis-pharma/1.0"
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310 - URL was allow-listed
                final_url = response.geturl()
                if not is_authoritative_url(final_url, allowed_domains):
                    raise EvidenceError(f"redirected outside authoritative domains: {final_url}")
                data = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise EvidenceError(str(exc)) from exc
        return data.decode("utf-8", errors="replace"), final_url

    def _collect_sec(self, security: Security, bundle: EvidenceBundle, *, as_of_date: date) -> None:
        """Collect SEC filing bodies, not merely submissions metadata.

        The submissions endpoint is used to discover recent filings; the
        primary document and a bounded number of exhibit/8-K documents are
        then fetched from the SEC archive.  A metadata-only row is not
        promoted to event evidence because it cannot support attribution.
        """

        cik = re.sub(r"\D", "", security.cik or "").zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        try:
            text, final_url = self._fetch(url, ("data.sec.gov", "sec.gov"))
            payload = json.loads(text)
            recent = payload.get("filings", {}).get("recent", {})
            forms = recent.get("form", []) or []
            limit = min(len(forms), int(self.config.source_policy.get("sec_limit", 20)))
            exhibit_limit = max(0, int(self.config.source_policy.get("sec_exhibit_limit", 3)))
            count = 0
            for idx in range(limit):
                form = str(forms[idx] or "")
                if form not in {"8-K", "6-K", "10-Q", "10-K", "20-F"}:
                    continue
                filing_dates = recent.get("filingDate", []) or []
                accession_values = recent.get("accessionNumber", []) or []
                primary_values = recent.get("primaryDocument", []) or []
                filing_date = _parse_date(filing_dates[idx] if idx < len(filing_dates) else None)
                accession = str(accession_values[idx] if idx < len(accession_values) else "").replace("-", "")
                primary = str(primary_values[idx] if idx < len(primary_values) else "")
                if not filing_date or not accession or not primary:
                    continue
                archive_root = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
                filing_url = f"{archive_root}/{urllib.parse.quote(primary)}"
                acceptance_values = recent.get("acceptanceDateTime", []) or []
                acceptance = acceptance_values[idx] if idx < len(acceptance_values) else None
                if acceptance:
                    published = _parse_sec_acceptance(acceptance)
                    published_precision = "timestamp"
                else:
                    published = _date_only_timestamp(filing_date)
                    published_precision = "date"
                identifiers = tuple(x for x in (security.symbol, security.cik or "", accession, form) if x)

                try:
                    primary_text, primary_final = self._fetch(filing_url, ("sec.gov",))
                    snippet = _clean_html_snippet(primary_text)
                    if not snippet:
                        raise EvidenceError("SEC primary document has no readable body")
                    full_snippet = f"{form}；{security.name}；{snippet}"
                    future = _forward_event(full_snippet, as_of_date)
                    item = Evidence(
                        id=_evidence_id(primary_final, published, identifiers),
                        url=primary_final,
                        publisher="U.S. Securities and Exchange Commission",
                        published_at=published,
                        retrieved_at=datetime.now(timezone.utc),
                        snippet=full_snippet,
                        identifiers=identifiers,
                        source_kind="sec_filing",
                        event_date=future[0] if future else None,
                        event_precision=future[2] if future else None,
                        event_label=future[3] if future else form,
                        event_window_end=future[1] if future else None,
                        event_status=future[4] if future else None,
                        published_precision=published_precision,
                    )
                    bundle.add(item, raw=primary_text)
                    count += 1
                except Exception as exc:
                    bundle.gaps.append(f"SEC 正文缺口：{security.symbol}/{accession}/{primary}（{exc}）")

                # The index JSON is an official SEC archive listing.  Fetch a
                # small number of likely exhibits (especially EX-99) so an
                # 8-K's actual event text is not lost behind the cover page.
                if exhibit_limit:
                    index_url = f"{archive_root}/index.json"
                    try:
                        index_text, _ = self._fetch(index_url, ("sec.gov",))
                        index_payload = json.loads(index_text)
                        names = [
                            str(entry.get("name"))
                            for entry in index_payload.get("directory", {}).get("item", [])
                            if isinstance(entry, dict) and entry.get("name")
                        ]
                        candidates = [
                            name for name in names
                            if name.lower() != primary.lower()
                            and ("ex99" in name.lower() or "ex-99" in name.lower() or "99." in name.lower() or "8-k" in name.lower())
                        ][:exhibit_limit]
                        for name in candidates:
                            exhibit_url = f"{archive_root}/{urllib.parse.quote(name)}"
                            try:
                                exhibit_text, exhibit_final = self._fetch(exhibit_url, ("sec.gov",))
                                exhibit_snippet = _clean_html_snippet(exhibit_text)
                                if not exhibit_snippet:
                                    continue
                                exhibit_full_snippet = f"{form} exhibit {name}；{security.name}；{exhibit_snippet}"
                                exhibit_future = _forward_event(exhibit_full_snippet, as_of_date)
                                exhibit_ids = (*identifiers, name)
                                exhibit_item = Evidence(
                                    id=_evidence_id(exhibit_final, published, exhibit_ids),
                                    url=exhibit_final,
                                    publisher="U.S. Securities and Exchange Commission",
                                    published_at=published,
                                    retrieved_at=datetime.now(timezone.utc),
                                    snippet=exhibit_full_snippet,
                                    identifiers=exhibit_ids,
                                    source_kind="sec_exhibit",
                                    event_date=exhibit_future[0] if exhibit_future else None,
                                    event_precision=exhibit_future[2] if exhibit_future else None,
                                    event_label=exhibit_future[3] if exhibit_future else f"{form} {name}",
                                    event_window_end=exhibit_future[1] if exhibit_future else None,
                                    event_status=exhibit_future[4] if exhibit_future else None,
                                    published_precision=published_precision,
                                )
                                bundle.add(exhibit_item, raw=exhibit_text)
                                count += 1
                            except Exception as exc:
                                bundle.gaps.append(f"SEC 附件缺口：{security.symbol}/{name}（{exc}）")
                    except Exception as exc:
                        bundle.gaps.append(f"SEC 附件索引缺口：{security.symbol}/{accession}（{exc}）")
            bundle.statuses.append(SourceStatus("sec", "ok" if count else "no_data", count, final_url))
        except EvidenceError as exc:
            status = "not_configured" if "SEC_USER_AGENT" in str(exc) else "error"
            bundle.statuses.append(SourceStatus("sec", status, 0, str(exc)))
            bundle.gaps.append(f"SEC 数据缺口：{security.symbol}（{exc}）")
        except Exception as exc:
            bundle.statuses.append(SourceStatus("sec", "error", 0, str(exc)))
            bundle.gaps.append(f"SEC 数据缺口：{security.symbol}（{exc}）")

    def _add_ir_evidence(
        self,
        security: Security,
        bundle: EvidenceBundle,
        *,
        url: str,
        title: str,
        body: str,
        published: datetime | None,
        published_precision: str,
        raw: str,
        as_of_date: date,
        lookback_days: int,
    ) -> bool:
        """Add one official IR item after date, domain and provenance checks."""

        link = str(url or "")
        if not is_authoritative_url(link, security.official_domains):
            bundle.gaps.append(f"IR 证据拒绝：{link or '<empty url>'}")
            return False
        full_snippet = _clean_html_snippet("；".join(part for part in (title, body) if part), limit=20_000)
        if not full_snippet:
            bundle.gaps.append(f"IR 正文缺口：{security.symbol} {link}")
            return False
        future = _forward_event(full_snippet, as_of_date)
        if published is not None:
            published_day = published.astimezone(self.config.zone).date()
            if published_day > as_of_date:
                bundle.gaps.append(f"IR 未来日期条目跳过：{security.symbol} {link}")
                return False
            # A stale announcement that explicitly contains a still-future
            # catalyst is retained for the catalyst section.  Attribution is
            # independently bounded downstream by publication/session time.
            if (as_of_date - published_day).days > lookback_days and not (
                future and future[0] > as_of_date
            ):
                bundle.gaps.append(f"IR 旧公告跳过：{security.symbol} {link}")
                return False
        identifiers = (security.symbol, title or link)
        item = Evidence(
            id=_evidence_id(link, published, identifiers),
            url=link,
            publisher=security.name,
            published_at=published,
            retrieved_at=datetime.now(timezone.utc),
            snippet=full_snippet,
            identifiers=identifiers,
            source_kind="company_ir",
            event_date=future[0] if future else None,
            event_precision=future[2] if future else None,
            event_label=future[3] if future else None,
            event_window_end=future[1] if future else None,
            event_status=future[4] if future else None,
            published_precision=published_precision or "unknown",
        )
        before = len(bundle.evidence)
        bundle.add(item, raw=raw)
        return len(bundle.evidence) > before

    @staticmethod
    def _is_ir_detail_url(url: str) -> bool:
        path = urllib.parse.urlparse(url).path.lower().rstrip("/")
        if not path:
            return False
        last = path.rsplit("/", 1)[-1]
        generic = {
            "news",
            "news-details",
            "news-release",
            "news-releases",
            "press-release",
            "press-releases",
            "press-kits",
            "presskits",
            "press",
            "media",
            "newsroom",
            "investors",
            "investor-relations",
            "news-center",
            "index.html",
            "default.aspx",
        }
        if last in generic:
            return False
        return any(token in path for token in ("/news", "/press", "/release", "/article", "/story"))

    def _collect_ir_json(
        self,
        security: Security,
        bundle: EvidenceBundle,
        *,
        text: str,
        final_url: str,
        as_of_date: date,
        lookback_days: int,
    ) -> int:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            bundle.gaps.append(f"IR JSON 解析缺口：{security.symbol} {final_url}（{exc}）")
            return 0
        rows = _json_listing_candidates(payload)
        limit = self._ir_detail_limit()
        count = 0
        if not rows:
            bundle.gaps.append(f"IR JSON列表未发现明确详情链接：{security.symbol} {final_url}")
            return 0
        for row in rows[:limit]:
            link = urllib.parse.urljoin(final_url, str(row.get("url") or ""))
            if not is_authoritative_url(link, security.official_domains):
                bundle.gaps.append(f"IR 证据拒绝：{link}")
                continue
            if link == final_url:
                bundle.gaps.append(f"IR JSON列表详情链接缺失：{security.symbol} {link}")
                continue
            try:
                detail_text, detail_url = self._fetch(link, security.official_domains)
            except Exception as exc:
                bundle.gaps.append(f"IR 详情页缺口：{security.symbol} {link}（{exc}）")
                continue
            detail = _parse_ir_html(detail_text) if not detail_text.lstrip("\ufeff \t\r\n").startswith(("{", "[")) else {}
            detail_payload: dict[str, Any] = {}
            if not detail:
                try:
                    parsed_detail = json.loads(detail_text)
                    if isinstance(parsed_detail, dict):
                        detail_payload = parsed_detail
                    detail_rows = _json_listing_candidates(parsed_detail)
                    if detail_rows:
                        detail_payload = detail_rows[0]
                except (TypeError, ValueError, json.JSONDecodeError):
                    detail_payload = {}
            detail_title = str(detail.get("title") or detail_payload.get("title") or row.get("title") or "IR update")
            detail_body = str(detail.get("body") or detail_payload.get("body") or "")
            published = detail.get("published")
            precision = str(detail.get("published_precision") or "unknown")
            if published is None and detail_payload:
                published, precision = _parse_web_published(
                    detail_payload.get("published_value")
                    or detail_payload.get("datePublished")
                    or detail_payload.get("published")
                    or detail_payload.get("date")
                )
            if published is None:
                published, precision = _parse_web_published(row.get("published_value"))
            body = "；".join(part for part in (str(row.get("title") or ""), str(row.get("body") or ""), detail_body) if part)
            raw = json.dumps(
                {
                    # Put the detail first so EvidenceBundle's bounded raw
                    # archive preserves the article body if a listing is
                    # unusually large.
                    "detail_url": detail_url,
                    "detail": detail_text[:120_000],
                    "listing_url": final_url,
                    "listing": text[:30_000],
                    "truncated": len(detail_text) > 120_000 or len(text) > 30_000,
                },
                ensure_ascii=False,
            )
            if self._add_ir_evidence(
                security,
                bundle,
                url=detail_url,
                title=detail_title,
                body=body,
                published=published,
                published_precision=precision,
                raw=raw,
                as_of_date=as_of_date,
                lookback_days=lookback_days,
            ):
                count += 1
        return count

    def _collect_ir_html(
        self,
        security: Security,
        bundle: EvidenceBundle,
        *,
        text: str,
        final_url: str,
        as_of_date: date,
        lookback_days: int,
    ) -> int:
        document = _parse_ir_html(text)
        json_article = any(
            str(row.get("@type") or "").lower() in {"article", "newsarticle", "pressrelease"}
            for row in document.get("json_rows", [])
        )
        detail_page = json_article or self._is_ir_detail_url(final_url)
        if detail_page and (document.get("body") or document.get("title")):
            if self._add_ir_evidence(
                security,
                bundle,
                url=final_url,
                title=str(document.get("title") or "IR update"),
                body=str(document.get("body") or ""),
                published=document.get("published"),
                published_precision=str(document.get("published_precision") or "unknown"),
                raw=text,
                as_of_date=as_of_date,
                lookback_days=lookback_days,
            ):
                return 1
            return 0

        links: list[str] = []
        seen: set[str] = set()
        for href, anchor_text in document.get("anchors", []):
            link = urllib.parse.urljoin(final_url, href)
            if not href or link in seen:
                continue
            seen.add(link)
            if not is_authoritative_url(link, security.official_domains):
                # Cross-domain links are never followed, even if their text
                # looks like a press release or search result.
                if anchor_text and self._is_ir_detail_url(link):
                    bundle.gaps.append(f"IR 证据拒绝：{link}")
                continue
            if anchor_text and self._is_ir_detail_url(link):
                links.append(link)
            if len(links) >= self._ir_detail_limit():
                break
        if not links:
            bundle.gaps.append(f"IR HTML列表未发现明确详情链接：{security.symbol} {final_url}")
            return 0
        count = 0
        for link in links:
            try:
                detail_text, detail_url = self._fetch(link, security.official_domains)
            except Exception as exc:
                bundle.gaps.append(f"IR 详情页缺口：{security.symbol} {link}（{exc}）")
                continue
            detail = _parse_ir_html(detail_text)
            title = str(detail.get("title") or "IR update")
            body = str(detail.get("body") or "")
            if not body and title == "IR update":
                bundle.gaps.append(f"IR 详情正文缺口：{security.symbol} {detail_url}")
                continue
            raw = json.dumps(
                {
                    "detail_url": detail_url,
                    "detail": detail_text[:120_000],
                    "listing_url": final_url,
                    "listing": text[:30_000],
                    "truncated": len(detail_text) > 120_000 or len(text) > 30_000,
                },
                ensure_ascii=False,
            )
            if self._add_ir_evidence(
                security,
                bundle,
                url=detail_url,
                title=title,
                body=body,
                published=detail.get("published"),
                published_precision=str(detail.get("published_precision") or "unknown"),
                raw=raw,
                as_of_date=as_of_date,
                lookback_days=lookback_days,
            ):
                count += 1
        return count

    def _ir_detail_limit(self) -> int:
        try:
            return min(10, max(1, int(self.config.source_policy.get("ir_detail_limit", 10))))
        except (TypeError, ValueError):
            return 10

    def _collect_feeds(self, security: Security, bundle: EvidenceBundle, *, as_of_date: date) -> None:
        count = 0
        blocked = False
        error_details: list[str] = []
        try:
            # A seven-day window is sufficient for same-day attribution but
            # drops still-live catalysts disclosed weeks earlier.  Keep 90
            # days by default; attribution later applies its own session-time
            # cutoff, so this does not turn old notices into daily causes.
            lookback_days = max(1, int(self.config.source_policy.get("ir_lookback_days", 90)))
        except (TypeError, ValueError):
            lookback_days = 90
        for feed_url in security.ir_feeds:
            try:
                text, final_url = self._fetch(feed_url, security.official_domains)
            except Exception as exc:
                detail = str(exc)
                blocked = blocked or bool(re.search(r"\b(?:403|429)\b|access denied|forbidden", detail, flags=re.I))
                error_details.append(detail)
                bundle.gaps.append(f"IR 来源缺口：{security.symbol} {feed_url}（{exc}）")
                continue

            # Prefer the strict XML parser when the source is RSS/Atom.  If a
            # vendor returns an HTML error page or a JSON endpoint with a
            # misleading text/html content type, fall through to the matching
            # official-list parser instead of declaring the source unusable.
            parsed_xml = False
            try:
                root = ElementTree.fromstring(text)
                xml_entries = [
                    entry
                    for entry in root.iter()
                    if entry.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}
                ]
            except (ElementTree.ParseError, ValueError):
                xml_entries = []
            if xml_entries:
                parsed_xml = True
                for entry in xml_entries:
                    fields = {
                        child.tag.rsplit("}", 1)[-1].lower(): "".join(child.itertext()).strip()
                        for child in entry
                    }
                    link_node = next(
                        (child for child in entry if child.tag.rsplit("}", 1)[-1].lower() == "link"),
                        None,
                    )
                    link = fields.get("link") or (link_node.attrib.get("href", "") if link_node is not None else "") or final_url
                    link = urllib.parse.urljoin(final_url, link)
                    published, published_precision = _parse_published(
                        fields.get("pubdate") or fields.get("published") or fields.get("updated")
                    )
                    title = fields.get("title") or "IR update"
                    body = "；".join(
                        part for part in (fields.get("description", ""), fields.get("encoded", ""), fields.get("content", "")) if part
                    )
                    if self._add_ir_evidence(
                        security,
                        bundle,
                        url=link,
                        title=title,
                        body=body,
                        published=published,
                        published_precision=published_precision,
                        raw=text,
                        as_of_date=as_of_date,
                        lookback_days=lookback_days,
                    ):
                        count += 1
            if parsed_xml:
                continue

            stripped = text.lstrip("\ufeff \t\r\n")
            if stripped.startswith(("{", "[")):
                count += self._collect_ir_json(
                    security,
                    bundle,
                    text=text,
                    final_url=final_url,
                    as_of_date=as_of_date,
                    lookback_days=lookback_days,
                )
            elif stripped.startswith("<"):
                count += self._collect_ir_html(
                    security,
                    bundle,
                    text=text,
                    final_url=final_url,
                    as_of_date=as_of_date,
                    lookback_days=lookback_days,
                )
            else:
                bundle.gaps.append(f"IR 来源格式不支持：{security.symbol} {final_url}")
        status = "ok" if count else ("blocked" if blocked else "no_data")
        detail = "; ".join(error_details[:2]) if error_details else None
        bundle.statuses.append(SourceStatus("ir", status, count, detail))

    def _collect_regulator_feeds(self, bundle: EvidenceBundle) -> None:
        feeds = self.config.source_policy.get("regulator_feeds", [])
        if not isinstance(feeds, list):
            feeds = []
        count = 0
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            url = str(feed.get("url", ""))
            publisher = str(feed.get("publisher") or "FDA/EMA")
            domains = ("fda.gov",) if "fda" in publisher.lower() else ("ema.europa.eu",)
            try:
                text, final_url = self._fetch(url, domains)
                root = ElementTree.fromstring(text)
                for entry in root.iter():
                    tag = entry.tag.rsplit("}", 1)[-1].lower()
                    if tag not in {"item", "entry"}:
                        continue
                    fields = {child.tag.rsplit("}", 1)[-1].lower(): (child.text or "").strip() for child in entry}
                    link_node = next(
                        (child for child in entry if child.tag.rsplit("}", 1)[-1].lower() == "link"),
                        None,
                    )
                    link = fields.get("link") or (link_node.attrib.get("href", "") if link_node is not None else "") or final_url
                    if not is_authoritative_url(link, domains):
                        continue
                    published, published_precision = _parse_published(
                        fields.get("pubdate") or fields.get("published") or fields.get("updated")
                    )
                    title = fields.get("title") or "regulatory update"
                    identifiers = (title, publisher)
                    item = Evidence(
                        id=_evidence_id(link, published, identifiers),
                        url=link,
                        publisher=publisher,
                        published_at=published,
                        retrieved_at=datetime.now(timezone.utc),
                        snippet=title[:2000],
                        identifiers=identifiers,
                        source_kind="regulator_rss",
                        published_precision=published_precision,
                    )
                    bundle.add(item, raw=text)
                    count += 1
            except Exception as exc:
                bundle.gaps.append(f"监管 RSS 缺口：{url}（{exc}）")
        bundle.statuses.append(SourceStatus("regulator", "ok" if count else "not_configured", count))

    def _collect_calendar_files(self, bundle: EvidenceBundle) -> None:
        """Load a pre-verified conference calendar snapshot conservatively."""

        rows = self.config.source_policy.get("calendar_files", [])
        if not isinstance(rows, list):
            bundle.gaps.append("官方会议日历配置缺口：source_policy.calendar_files 必须是路径或事件字典列表")
            rows = []
        count = 0
        allowed = tuple(self.config.source_policy.get("calendar_domains", ())) or tuple(KNOWN_OFFICIAL_DOMAINS)
        events: list[dict[str, Any]] = []
        repo_root = Path(__file__).resolve().parents[2]
        for configured in rows:
            if isinstance(configured, dict):
                # Keep compatibility with the original inline event form.
                if isinstance(configured.get("events"), list):
                    events.extend(item for item in configured["events"] if isinstance(item, dict))
                else:
                    events.append(configured)
                continue
            if not isinstance(configured, str) or not configured.strip():
                bundle.gaps.append(f"官方会议日历配置缺口：无效路径/事件 {configured!r}")
                continue
            requested = Path(configured.strip())
            candidates = [requested] if requested.is_absolute() else [Path.cwd() / requested, repo_root / requested]
            calendar_path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if calendar_path is None:
                bundle.gaps.append(f"官方会议日历文件缺失：{configured}")
                continue
            try:
                payload = json.loads(calendar_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                bundle.gaps.append(f"官方会议日历文件读取失败：{calendar_path}（{exc}）")
                continue
            file_events = payload.get("events") if isinstance(payload, dict) else payload
            if not isinstance(file_events, list):
                bundle.gaps.append(f"官方会议日历文件缺少 events 列表：{calendar_path}")
                continue
            invalid_count = sum(1 for item in file_events if not isinstance(item, dict))
            if invalid_count:
                bundle.gaps.append(f"官方会议日历文件含无效事件：{calendar_path}（{invalid_count} 条）")
            events.extend(item for item in file_events if isinstance(item, dict))

        for raw in events:
            url = str(raw.get("url") or "")
            if not is_authoritative_url(url, allowed):
                bundle.gaps.append(f"官方会议日历拒绝：{url or '<empty url>'} 不在允许域名内")
                continue
            event_date = _parse_date(raw.get("event_date"))
            event_end = _parse_date(raw.get("event_window_end")) or event_date
            if event_date is None:
                bundle.gaps.append(f"官方会议日历缺少明确日期：{raw.get('label') or url}")
                continue
            published, precision = _parse_published(raw.get("published_at"))
            symbol = str(raw.get("symbol") or "行业")
            source_id = str(raw.get("source_id") or raw.get("label") or event_date.isoformat())
            identifiers = (symbol, source_id)
            item = Evidence(
                id=_evidence_id(url, published, identifiers),
                url=url,
                publisher=str(raw.get("publisher") or "官方会议组织方"),
                published_at=published,
                retrieved_at=datetime.now(timezone.utc),
                snippet=str(raw.get("snippet") or raw.get("label") or "官方会议日历")[:2000],
                identifiers=identifiers,
                source_kind="official_calendar",
                event_date=event_date,
                event_precision=str(raw.get("event_precision") or raw.get("precision") or "day"),
                event_label=str(raw.get("label") or "官方会议"),
                event_window_end=event_end,
                event_status=str(raw.get("event_status") or raw.get("status") or "confirmed"),
                published_precision=str(raw.get("published_precision") or precision),
            )
            bundle.add(item, raw=json.dumps(raw, ensure_ascii=False))
            count += 1
        status = "ok" if count else ("error" if bundle.gaps else "not_configured")
        bundle.statuses.append(SourceStatus("official_calendar", status, count))

    def _collect_clinicaltrial(self, security: Security, nct_id: str, bundle: EvidenceBundle) -> None:
        nct_id = nct_id.strip().upper()
        if not re.fullmatch(r"NCT\d{8}", nct_id):
            bundle.gaps.append(f"CT.gov 编号拒绝：{security.symbol} {nct_id}")
            return
        url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
        try:
            text, final_url = self._fetch(url, ("clinicaltrials.gov",))
            payload = json.loads(text)
            protocol = payload.get("protocolSection", {})
            identification = protocol.get("identificationModule", {})
            status = protocol.get("statusModule", {})
            design = protocol.get("designModule", {})
            primary_struct = status.get("primaryCompletionDateStruct") or {}
            completion_struct = status.get("completionDateStruct") or {}
            event_struct = primary_struct or completion_struct
            event_date, event_precision = _parse_date_struct(event_struct)
            event_end = None
            if event_date and event_precision == "year":
                event_end = date(event_date.year, 12, 31)
            elif event_date and event_precision == "month":
                from calendar import monthrange

                event_end = date(event_date.year, event_date.month, monthrange(event_date.year, event_date.month)[1])
            date_type = str(event_struct.get("type") or event_struct.get("dateType") or "").upper() if isinstance(event_struct, dict) else ""
            event_status = "actual" if date_type == "ACTUAL" else ("estimated" if date_type == "ESTIMATED" else "unknown")
            update_struct = status.get("lastUpdatePostDateStruct") or status.get("studyFirstPostDateStruct") or {}
            published_value = update_struct.get("date") if isinstance(update_struct, dict) else update_struct
            published, published_precision = _parse_published(published_value)
            title = identification.get("briefTitle") or nct_id
            phases = ",".join(str(phase) for phase in design.get("phases", []) or []) or "phase unknown"
            snippet = f"{title}; status={status.get('overallStatus', 'unknown')}; phase={phases}"
            primary_text = primary_struct.get("date") or primary_struct.get("year") if isinstance(primary_struct, dict) else None
            completion_text = completion_struct.get("date") or completion_struct.get("year") if isinstance(completion_struct, dict) else None
            if primary_text:
                snippet += f"; primaryCompletionDate={primary_text}"
            if completion_text:
                snippet += f"; completionDate={completion_text}"
            if date_type:
                snippet += f"; dateType={date_type}"
            identifiers = (security.symbol, nct_id)
            item = Evidence(
                id=_evidence_id(f"https://clinicaltrials.gov/study/{nct_id}", published, identifiers),
                url=f"https://clinicaltrials.gov/study/{nct_id}",
                publisher="ClinicalTrials.gov",
                published_at=published,
                retrieved_at=datetime.now(timezone.utc),
                snippet=snippet[:2000],
                identifiers=identifiers,
                source_kind="clinicaltrials",
                event_date=event_date,
                event_precision=event_precision,
                event_label="预计主要完成日期" if primary_struct else ("预计试验完成日期" if completion_struct else None),
                event_window_end=event_end,
                event_status=event_status,
                published_precision=published_precision,
            )
            bundle.add(item, raw=text)
            bundle.statuses.append(SourceStatus("clinicaltrials", "ok", 1))
        except Exception as exc:
            bundle.statuses.append(SourceStatus("clinicaltrials", "error", 0, str(exc)))
            bundle.gaps.append(f"CT.gov 缺口：{security.symbol}/{nct_id}（{exc}）")

    def _discover_clinicaltrials(self, security: Security, as_of: datetime, bundle: EvidenceBundle) -> None:
        """Discover phase 2/3 active studies for explicit sponsor aliases.

        Aliases are intentionally opt-in and must be English legal sponsor
        names supplied by the operator.  This avoids guessing a sponsor from a
        Chinese display name or ticker.  The endpoint and the study pages are
        both official ClinicalTrials.gov URLs and every discovered study is
        retained with a concrete NCT identifier.
        """

        allowed_statuses = {
            "RECRUITING",
            "NOT_YET_RECRUITING",
            "ACTIVE_NOT_RECRUITING",
            "ENROLLING_BY_INVITATION",
        }
        horizon_days = self.config.source_policy.get("clinicaltrials_horizon_days", 90)
        try:
            horizon = as_of.date() + timedelta(days=max(1, int(horizon_days)))
        except (TypeError, ValueError):
            horizon = as_of.date() + timedelta(days=90)
        total = 0
        aliases_without_matches: list[str] = []
        for alias in security.sponsor_aliases:
            page_size = min(100, int(self.config.source_policy.get("clinicaltrials_page_size", 100)))
            max_pages = max(1, int(self.config.source_policy.get("clinicaltrials_max_pages", 5)))
            params: dict[str, str] = {
                "query.term": f'AREA[LeadSponsorName]"{alias}"',
                "filter.overallStatus": "|".join(sorted(allowed_statuses)),
                "pageSize": str(page_size),
                "format": "json",
            }
            if bool(self.config.source_policy.get("clinicaltrials_use_date_filter", True)):
                params["filter.advanced"] = f"AREA[PrimaryCompletionDate]RANGE[{as_of.date().isoformat()},{horizon.isoformat()}]"
            try:
                matched = 0
                next_token: str | None = None
                page_count = 0
                while page_count < max_pages:
                    if next_token:
                        params["pageToken"] = next_token
                    query = urllib.parse.urlencode(params)
                    url = f"https://clinicaltrials.gov/api/v2/studies?{query}"
                    text, _ = self._fetch(url, ("clinicaltrials.gov",))
                    payload = json.loads(text)
                    page_count += 1
                    for study in payload.get("studies", []) or []:
                        if not isinstance(study, dict):
                            continue
                        protocol = study.get("protocolSection", {})
                        identification = protocol.get("identificationModule", {})
                        nct_id = str(identification.get("nctId") or "").upper()
                        status = protocol.get("statusModule", {})
                        design = protocol.get("designModule", {})
                        phases = {str(phase).upper() for phase in design.get("phases", []) or []}
                        if not re.fullmatch(r"NCT\d{8}", nct_id):
                            continue
                        if not phases.intersection({"PHASE2", "PHASE3", "PHASE2|PHASE3"}):
                            continue
                        if str(status.get("overallStatus") or "").upper() not in allowed_statuses:
                            continue
                        primary_struct = status.get("primaryCompletionDateStruct") or {}
                        completion_struct = status.get("completionDateStruct") or {}
                        event_struct = primary_struct or completion_struct
                        event_date, event_precision = _parse_date_struct(event_struct)
                        if event_date and event_date > horizon:
                            continue
                        update_struct = status.get("lastUpdatePostDateStruct") or status.get("studyFirstPostDateStruct") or {}
                        update_value = update_struct.get("date") if isinstance(update_struct, dict) else update_struct
                        published, published_precision = _parse_published(update_value)
                        title = identification.get("briefTitle") or nct_id
                        event_end = None
                        if event_date and event_precision == "year":
                            event_end = date(event_date.year, 12, 31)
                        elif event_date and event_precision == "month":
                            from calendar import monthrange

                            event_end = date(event_date.year, event_date.month, monthrange(event_date.year, event_date.month)[1])
                        event_label = "预计主要完成日期" if primary_struct else ("预计试验完成日期" if completion_struct else None)
                        identifiers = (security.symbol, nct_id)
                        study_url = f"https://clinicaltrials.gov/study/{nct_id}"
                        item = Evidence(
                            id=_evidence_id(study_url, published, identifiers),
                            url=study_url,
                            publisher="ClinicalTrials.gov",
                            published_at=published,
                            retrieved_at=datetime.now(timezone.utc),
                            snippet=f"{title}; sponsor_alias={alias}; status={status.get('overallStatus', 'unknown')}; phase={','.join(sorted(phases))}"[:2000],
                            identifiers=identifiers,
                            source_kind="clinicaltrials_discovery",
                            event_date=event_date,
                            event_precision=event_precision,
                            event_label=event_label,
                            event_window_end=event_end,
                            event_status=("actual" if str(event_struct.get("type") or event_struct.get("dateType") or "").upper() == "ACTUAL" else "estimated" if event_date else "unknown"),
                            published_precision=published_precision,
                        )
                        before = len(bundle.evidence)
                        bundle.add(item, raw=json.dumps(study, ensure_ascii=False))
                        if len(bundle.evidence) > before:
                            matched += 1
                    next_token = payload.get("nextPageToken")
                    if not next_token:
                        break
                if next_token:
                    bundle.gaps.append(f"CT.gov sponsor discovery 分页未完：{security.symbol}/{alias}（达到 {max_pages} 页上限）")
                total += matched
                bundle.statuses.append(SourceStatus("clinicaltrials_discovery", "ok", matched, alias))
                if matched == 0:
                    aliases_without_matches.append(alias)
            except Exception as exc:
                bundle.statuses.append(SourceStatus("clinicaltrials_discovery", "error", 0, alias))
                bundle.gaps.append(f"CT.gov sponsor discovery 缺口：{security.symbol}/{alias}（{exc}）")
        if aliases_without_matches:
            bundle.gaps.append(
                f"CT.gov 未覆盖 sponsor_aliases：{security.symbol}/" + ", ".join(aliases_without_matches)
            )
        if not security.sponsor_aliases:
            bundle.gaps.append(f"CT.gov 未覆盖：{security.symbol} 未配置已核验 sponsor_aliases 或 NCT 编号")

    def _collect_ah_adapter(self, bundle: EvidenceBundle, securities: Iterable[Security], *, as_of: datetime) -> None:
        ah = tuple(s for s in securities if s.market in {"cn", "hk"})
        if not ah:
            return
        try:
            from .ah_sources import collect_ah_announcements  # type: ignore
        except Exception:
            bundle.statuses.append(
                SourceStatus("ah_announcements", "not_configured", 0, "adapter interface reserved for host announcement provider")
            )
            bundle.gaps.append("A/H 公告适配器未配置；不使用搜索摘要或伪官方域名替代")
            return
        try:
            events, statuses = collect_ah_announcements(ah, as_of=as_of)
            for status in statuses or []:
                if isinstance(status, SourceStatus):
                    bundle.statuses.append(status)
                elif isinstance(status, dict):
                    bundle.statuses.append(
                        SourceStatus(
                            str(status.get("source") or status.get("market") or "ah_announcements"),
                            str(status.get("status") or "unknown"),
                            int(status.get("count") or 0),
                            status.get("detail"),
                        )
                    )
            by_symbol = {security.symbol.upper(): security for security in ah}
            for raw in events or []:
                if not isinstance(raw, dict):
                    continue
                symbol = str(raw.get("symbol") or "").strip()
                security = by_symbol.get(symbol.upper())
                if security is None:
                    continue
                url = str(raw.get("url") or "")
                allowed = security.official_domains or (("cninfo.com.cn", "sse.com.cn", "szse.cn") if security.market == "cn" else ("hkexnews.hk", "hkex.com.hk"))
                if not is_authoritative_url(url, allowed):
                    bundle.gaps.append(f"A/H 证据拒绝：{symbol} {url}")
                    continue
                published, precision = _parse_published(raw.get("published_at"))
                identifiers = tuple(str(value) for value in (raw.get("identifiers") or (symbol, raw.get("source_id") or "")) if str(value))
                snippet = str(raw.get("snippet") or raw.get("title") or "")
                future = _forward_event(snippet, as_of.date())
                item = Evidence(
                    id=str(raw.get("source_id") or _evidence_id(url, published, identifiers)),
                    url=url,
                    publisher=str(raw.get("publisher") or ("巨潮资讯" if raw.get("source_kind") == "cninfo" else "HKEXnews")),
                    published_at=published,
                    retrieved_at=datetime.now(timezone.utc),
                    snippet=(
                        snippet
                        + ("（公告正文未能提取，仅标题/元数据；不足以解释具体事件）" if raw.get("body_unavailable") else "")
                    )[:2000],
                    identifiers=identifiers,
                    source_kind=str(raw.get("source_kind") or "ah_announcement"),
                    event_date=future[0] if future else None,
                    event_precision=future[2] if future else None,
                    event_label=future[3] if future else str(raw.get("title") or "A/H 公告"),
                    event_window_end=future[1] if future else None,
                    event_status=(future[4] if future else "confirmed") if not raw.get("body_unavailable") else "metadata_only",
                    published_precision=str(raw.get("published_precision") or precision),
                )
                bundle.add(item, raw=json.dumps(raw, ensure_ascii=False, default=str))
            if not events:
                bundle.gaps.append("A/H 公告适配器未返回可校验记录；重点异动事件覆盖为0")
        except Exception as exc:
            bundle.statuses.append(SourceStatus("ah_announcements", "error", 0, str(exc)))
            bundle.gaps.append(f"A/H 公告适配器缺口：{exc}")
