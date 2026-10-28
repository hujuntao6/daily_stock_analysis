"""Deterministic Chinese Markdown/HTML/JSON report rendering."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .attribution import attribute_moves
from .calendar import MARKET_TIMEZONES, Session
from .catalysts import extract_catalysts
from .config import HealthcareConfig
from .market_data import MarketDataResult
from .models import Evidence, Quote, jsonable


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _symbol_key(value: str) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".SH"):
        text = text[:-3] + ".SS"
    if text.endswith(".HK"):
        base = text[:-3]
        return base.zfill(5) + ".HK" if base.isdigit() else text
    if text.endswith((".SS", ".SZ")):
        base, suffix = text.rsplit(".", 1)
        return base.zfill(6) + "." + suffix if base.isdigit() else text
    # Never strip leading zeros from an unsuffixed identifier: it may be a
    # CIK/accession or a code from another market, not this security's ticker.
    return text


def _display_published(item: dict[str, Any]) -> str:
    value = item.get("published_at")
    precision = str(item.get("published_precision") or "timestamp")
    if not value or precision == "unknown":
        return "未知（未披露）"
    text = str(value)
    if precision == "date":
        return text[:10]
    if precision == "minute":
        return text[:16].replace("T", " ")
    return text


def build_report(
    *,
    config: HealthcareConfig,
    session: str,
    as_of: datetime,
    sessions: dict[str, Session],
    aligned: bool,
    alignment_error: str | None,
    market_data: MarketDataResult,
    evidence: list[Evidence],
    source_statuses: list[Any],
    source_gaps: list[str],
    raw_evidence: dict[str, str],
    strict: bool,
    evidence_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    benchmark_map = {q.market: q for q in market_data.benchmark_quotes}
    for quote in market_data.quotes:
        benchmark = benchmark_map.get(quote.market)
        if benchmark:
            quote.benchmark_symbol = benchmark.symbol
            quote.benchmark_return_pct = benchmark.return_pct

    covered = [q for q in market_data.quotes if q.status == "ok" and q.close is not None]
    uncovered = [q.symbol for q in market_data.quotes if q not in covered]
    movers = sorted(
        [
            {
                "symbol": q.symbol,
                "name": q.name,
                "market": q.market,
                "subsector": q.subsector,
                "return_pct": q.return_pct,
                "relative_return_pct": q.relative_return_pct,
                "session_date": q.session_date,
            }
            for q in covered
            if q.return_pct is not None
        ],
        key=lambda item: item["return_pct"],
        reverse=True,
    )
    limit = int(config.report.get("max_movers", 10))
    unusual_threshold = float(config.report.get("unusual_volume_ratio", 2.0))
    unusual = [
        {
            "symbol": q.symbol,
            "name": q.name,
            "volume_ratio": q.volume_ratio,
            "return_pct": q.return_pct,
            "session_date": q.session_date,
        }
        for q in covered
        if q.volume_ratio is not None and q.volume_ratio >= unusual_threshold
    ]
    subsector_rows: dict[tuple[str, str], list[float]] = {}
    for quote in covered:
        if quote.return_pct is not None:
            subsector_rows.setdefault((quote.market, quote.subsector), []).append(quote.return_pct)
    subsectors = [
        {"market": market, "subsector": name, "covered": len(values), "average_return_pct": sum(values) / len(values)}
        for (market, name), values in sorted(subsector_rows.items())
    ]
    aligned_dates = {market: value.session_date for market, value in sessions.items()}
    session_notes = {}
    for market, value in sessions.items():
        local_date = as_of.astimezone(ZoneInfo(MARKET_TIMEZONES[market])).date()
        if value.session_date < local_date:
            session_notes[market] = f"本地当日尚无新收盘行情，展示最近已完成交易日 {value.session_date.isoformat()}（休市或尚未收盘）"
    attributions = attribute_moves(market_data.quotes, evidence, sessions) if sessions else ()
    catalysts = extract_catalysts(evidence, as_of.date())
    coverage_pct = market_data.coverage_pct
    calendar_fallback = any(value.source == "weekday_fallback" for value in sessions.values())
    if not aligned:
        quality = "date_misaligned"
    elif market_data.requested_count == 0 or coverage_pct == 0:
        quality = "no_data"
    elif coverage_pct < 100:
        quality = "degraded"
    else:
        quality = "full"
    if calendar_fallback and quality == "full":
        quality = "degraded"
    symbol_movers = {row["symbol"] for row in movers[:limit] + list(reversed(movers[-limit:]))}
    configured_focus = {str(value) for value in config.report.get("focus_symbols", []) if str(value).strip()}
    focus_symbols = symbol_movers | configured_focus
    focus_attributions = tuple(item for item in attributions if item.symbol in focus_symbols)
    post_close_events = tuple(item for item in attributions if item.timing == "post_close")
    unknown_time_events = tuple(item for item in attributions if item.timing == "unknown_time")
    quote_symbols_by_key = {_symbol_key(quote.symbol): quote.symbol for quote in market_data.quotes}
    evidence_symbols = {
        quote_symbols_by_key[_symbol_key(identifier)]
        for item in evidence
        for identifier in item.identifiers
        if _symbol_key(identifier) in quote_symbols_by_key
    }
    catalyst_symbols = {
        quote_symbols_by_key[_symbol_key(item.symbol)]
        for item in catalysts
        if item.symbol != "未标的" and _symbol_key(item.symbol) in quote_symbols_by_key
    }
    quote_symbols = {quote.symbol for quote in market_data.quotes}
    events_cov = {
        "requested": len(quote_symbols),
        "covered": len(evidence_symbols),
        "coverage_pct": (len(evidence_symbols) / len(quote_symbols) * 100.0) if quote_symbols else 0.0,
        "uncovered_symbols": sorted(quote_symbols - evidence_symbols),
        "focus_requested": len(focus_symbols & quote_symbols),
        "focus_covered": len(evidence_symbols & focus_symbols),
    }
    catalysts_cov = {
        "requested": len(quote_symbols),
        "covered": len(catalyst_symbols & quote_symbols),
        "coverage_pct": (len(catalyst_symbols & quote_symbols) / len(quote_symbols) * 100.0) if quote_symbols else 0.0,
        "uncovered_symbols": sorted(quote_symbols - catalyst_symbols),
    }
    benchmark_cov = {
        "requested": len(sessions),
        "covered": market_data.benchmark_covered_count,
        "coverage_pct": (market_data.benchmark_covered_count / len(sessions) * 100.0) if sessions else 0.0,
    }
    all_gaps = [*market_data.gaps, *source_gaps]
    report_status = "complete"
    if (
        quality != "full"
        or calendar_fallback
        or benchmark_cov["covered"] < benchmark_cov["requested"]
        or not evidence
        or events_cov["covered"] == 0
    ):
        report_status = "partial"
    if strict and quality != "full":
        raise ValueError(f"strict report rejected: data_quality={quality}")

    report = {
        "generated_at": datetime.now(timezone.utc),
        "as_of": as_of,
        "session": session,
        "aligned": aligned,
        "alignment_error": alignment_error,
        "market_dates": aligned_dates,
        "session_notes": session_notes,
        "calendar_sources": {market: value.source for market, value in sessions.items()},
        "data_quality": quality,
        "status": report_status,
        "report_status": report_status,
        "calendar_date_mismatch": len(set(aligned_dates.values())) > 1,
        "coverage": {
            "requested": market_data.requested_count,
            "covered": market_data.covered_count,
            "coverage_pct": coverage_pct,
            "uncovered_symbols": uncovered,
            "gaps": all_gaps,
            "events": events_cov,
            "catalysts": catalysts_cov,
            "benchmarks": benchmark_cov,
        },
        "movers": {
            "top": movers[:limit],
            "bottom": list(reversed(movers[-limit:])) if movers else [],
            "by_market": {
                market: {
                    "top": [row for row in movers if row["market"] == market][:limit],
                    "bottom": list(reversed([row for row in movers if row["market"] == market][-limit:])),
                }
                for market in sorted({row["market"] for row in movers})
            },
        },
        "subsectors": subsectors,
        "unusual_volume": unusual[:limit],
        "benchmarks": [
            {
                "market": q.market,
                "symbol": q.symbol,
                "session_date": q.session_date,
                "return_pct": q.return_pct,
                "source": q.source,
            }
            for q in market_data.benchmark_quotes
        ],
        "quotes": market_data.quotes,
        "attributions": attributions,
        "focus_attributions": focus_attributions,
        "post_close_events": post_close_events,
        "unknown_time_events": unknown_time_events,
        "catalysts": catalysts,
        "evidence": evidence,
        "evidence_coverage": evidence_coverage or {},
        "raw_evidence": raw_evidence,
        "source_status": [*market_data.statuses, *source_statuses],
        "source_gaps": all_gaps,
        "disclaimer": "事件归因仅表示相关事件、可能相关或未找到解释，不代表因果关系；模型未参与事实字段计算。",
    }
    return jsonable(report)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# 医药行业每日复盘（{report['as_of'][:10]}）",
        "",
        f"- 复盘范围：`{report['session']}`；报告状态：`{report.get('status', 'unknown')}`；数据质量：`{report['data_quality']}`",
        f"- 对齐交易日：{', '.join(f'{k}={v}' for k, v in report['market_dates'].items())}",
        f"- 行情覆盖：{report['coverage']['covered']}/{report['coverage']['requested']}（{report['coverage']['coverage_pct']:.1f}%）",
        f"- 事件证据覆盖：{report['coverage']['events']['covered']}/{report['coverage']['events']['requested']}（{report['coverage']['events']['coverage_pct']:.1f}%）；未来催化剂覆盖：{report['coverage']['catalysts']['covered']}/{report['coverage']['catalysts']['requested']}（{report['coverage']['catalysts']['coverage_pct']:.1f}%）",
        "",
        "## 涨跌榜与子板块",
        "",
    ]
    for market, note in report.get("session_notes", {}).items():
        lines.append(f"- `{market}`：{note}")
    if report.get("session_notes"):
        lines.append("")
    for market, rows in report["movers"].get("by_market", {}).items():
        lines += [f"### {market} 涨幅榜", "", "| 标的 | 子板块 | 收益 | 相对基准 | 交易日 |", "|---|---|---:|---:|---|"]
        for row in rows["top"]:
            lines.append(f"| {row['symbol']} {row['name']} | {row['subsector']} | {_pct(row['return_pct'])} | {_pct(row['relative_return_pct'])} | {row['session_date']} |")
        if not rows["top"]:
            lines.append("| — | — | 无可用行情 | — | — |")
        lines += ["", f"### {market} 跌幅榜", "", "| 标的 | 子板块 | 收益 | 相对基准 | 交易日 |", "|---|---|---:|---:|---|"]
        for row in rows["bottom"]:
            lines.append(f"| {row['symbol']} {row['name']} | {row['subsector']} | {_pct(row['return_pct'])} | {_pct(row['relative_return_pct'])} | {row['session_date']} |")
        if not rows["bottom"]:
            lines.append("| — | — | 无可用行情 | — | — |")
    lines += ["", "### 子板块均值（按市场）", "", "| 市场 | 子板块 | 覆盖数 | 平均收益 |", "|---|---|---:|---:|"]
    for row in report["subsectors"]:
        lines.append(f"| {row['market']} | {row['subsector']} | {row['covered']} | {_pct(row['average_return_pct'])} |")
    lines += ["", "## 异常成交", ""]
    if report["unusual_volume"]:
        for row in report["unusual_volume"]:
            lines.append(f"- `{row['symbol']}`：成交量约为20日均值 {row['volume_ratio']:.1f} 倍，单日 {_pct(row['return_pct'])}。")
    else:
        lines.append("- 未发现满足阈值的异常成交，或成交量数据缺失。")
    evidence_index = {row["id"]: row for row in report.get("evidence", [])}
    lines += ["", "## 重点异动事件相关性提示", "", "| 标的 | 标签 | 说明 | 原文 |", "|---|---|---|---|"]
    displayed_attributions = report.get("focus_attributions") or report.get("attributions", [])[:20]
    for row in displayed_attributions:
        links = []
        for evidence_id in row.get("evidence_ids", []):
            item = evidence_index.get(evidence_id)
            if item and item.get("url"):
                links.append(f"[{item.get('publisher', '原文')}]({item['url']})")
        lines.append(f"| {row['symbol']} | {row['status']} | {row['note']} | {'; '.join(links) or '—'} |")
    for row in report.get("post_close_events", []):
        links = []
        for evidence_id in row.get("evidence_ids", []):
            item = evidence_index.get(evidence_id)
            if item and item.get("url"):
                links.append(f"[{item.get('publisher', '原文')}]({item['url']})")
        lines.append(f"| {row['symbol']} | 盘后消息 | {row['note']} | {'; '.join(links) or '—'} |")
    for row in report.get("unknown_time_events", []):
        links = []
        for evidence_id in row.get("evidence_ids", []):
            item = evidence_index.get(evidence_id)
            if item and item.get("url"):
                links.append(f"[{item.get('publisher', '原文')}]({item['url']})")
        lines.append(f"| {row['symbol']} | 时点不明 | {row['note']} | {'; '.join(links) or '—'} |")
    lines += ["", "## 未来催化剂", ""]
    if report["catalysts"]:
        for row in report["catalysts"]:
            event_date = row["event_date"] or f"{row.get('window_start', '窗口')}至{row.get('window_end', '窗口')}"
            note = f"（{row['note']}）" if row["note"] else ""
            links = []
            for evidence_id in row.get("evidence_ids", []):
                item = evidence_index.get(evidence_id)
                if item and item.get("url"):
                    links.append(f"[{item.get('publisher', '原文')}]({item['url']})")
            lines.append(f"- `{row['window']}` {row['symbol']}：{row['label']}，日期/窗口 `{event_date}`，精度 `{row.get('precision', 'unknown')}`，{'；'.join(links) or '无可点击原文'}{note}")
    else:
        lines.append("- 未找到未来90天内带有官方明确日期或季度窗口的事件；不推断 PDUFA 日期。")
    lines += ["", "## 来源状态与缺口", ""]
    for status in report["source_status"]:
        detail = f"：{status['detail']}" if status.get("detail") else ""
        lines.append(f"- `{status['source']}`：{status['status']}，记录 {status['count']}{detail}")
    for gap in report["source_gaps"]:
        lines.append(f"- 缺口：{gap}")
    lines += ["", "## 可核验事件原文", "", "| 证据 | 发布方 | 发布时间 | 摘要 |", "|---|---|---|---|"]
    for item in report.get("evidence", [])[:50]:
        published = _display_published(item)
        snippet = str(item.get("snippet") or "").replace("|", "／")[:240]
        url = item.get("url") or ""
        link = f"[{item.get('id', 'evidence')}]({url})" if url else item.get("id", "evidence")
        lines.append(f"| {link} | {item.get('publisher', '—')} | {published} | {snippet} |")
    if not report.get("evidence"):
        lines.append("| — | — | — | 未找到通过权威域名校验的事件证据。 |")
    lines += ["", "## 口径", "", report["disclaimer"], ""]
    return "\n".join(lines)


def render_html(markdown: str, report: dict[str, Any]) -> str:
    try:
        import markdown2  # type: ignore
    except ImportError as exc:
        raise RuntimeError("markdown2 is required to render healthcare HTML; install requirements-healthcare.txt") from exc
    body = markdown2.markdown(markdown, extras=["tables", "fenced-code-blocks", "strike"])
    style = """
    <style>
      body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.55;color:#1f2937;max-width:1200px;margin:2rem auto;padding:0 1rem}
      table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #d1d5db;padding:.45rem .6rem;text-align:left;vertical-align:top}th{background:#f3f4f6}
      a{color:#075985}code{background:#f3f4f6;padding:.1rem .25rem;border-radius:3px}
    </style>
    """
    return "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>医药行业每日复盘</title>" + style + "<body>" + body + "</body></html>"


def save_report(report: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "healthcare_report.json"
    md_path = root / "healthcare_report.md"
    html_path = root / "healthcare_report.html"
    evidence_path = root / "evidence_archive.jsonl"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(render_html(markdown, report), encoding="utf-8")
    with evidence_path.open("w", encoding="utf-8") as handle:
        for evidence_id, raw in report.get("raw_evidence", {}).items():
            handle.write(json.dumps({"id": evidence_id, "raw": raw}, ensure_ascii=False) + "\n")
    return {"json": json_path, "markdown": md_path, "html": html_path, "evidence": evidence_path}
