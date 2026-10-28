from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .calendar import TradingCalendar, parse_as_of
from .config import ConfigError, load_config
from .evidence import EvidenceCollector
from .market_data import MarketDataProvider
from .report import build_report, save_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A/H/美股医药行业每日复盘")
    parser.add_argument("--session", required=True, choices=("us", "ah"), help="复盘范围：us 或 ah")
    parser.add_argument("--config", required=True, help="healthcare.json")
    parser.add_argument("--output", required=True, help="报告输出目录")
    parser.add_argument("--as-of", help="ISO 日期或带时区时间；默认当前时间")
    parser.add_argument("--no-email", action="store_true", help="兼容参数；本扩展不会发送邮件")
    parser.add_argument("--fixture", help="离线 fixture JSON")
    parser.add_argument("--strict", action="store_true", help="日期/覆盖/证据不满足时失败")
    return parser


def _focus_securities(securities, market_data, focus_symbols=(), limit: int = 30):
    """Select important movers for expensive evidence retrieval.

    The JSON report still retains full-pool quote coverage.  Official event
    retrieval is intentionally focused on the top absolute movers plus
    explicitly configured focus symbols.  ``limit`` applies only to movers;
    configured focus symbols are always retained and never represented as
    full-universe event coverage.
    """

    by_symbol = {str(security.symbol).upper(): security for security in securities}
    ranked = sorted(
        (quote for quote in market_data.quotes if quote.return_pct is not None),
        key=lambda quote: abs(quote.return_pct or 0.0),
        reverse=True,
    )
    selected: list = []
    selected_symbols: set[str] = set()
    for quote in ranked[: max(0, int(limit))]:
        key = str(quote.symbol).upper()
        security = by_symbol.get(key)
        if security is not None and key not in selected_symbols:
            selected.append(security)
            selected_symbols.add(key)

    # Configured focus is resolved against the already selected session's
    # securities.  This prevents a US run from accidentally pulling an A/H
    # focus symbol that happens to share a numeric code.
    for symbol in focus_symbols or ():
        key = str(symbol).strip().upper()
        security = by_symbol.get(key)
        if security is not None and key not in selected_symbols:
            selected.append(security)
            selected_symbols.add(key)
    return tuple(selected)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        as_of = parse_as_of(args.as_of, config.timezone)
        fixture = {}
        if args.fixture:
            fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
            if not isinstance(fixture, dict):
                raise ValueError("fixture root must be an object")
        markets = config.markets_for_session(args.session)
        calendar = TradingCalendar()
        sessions, aligned, alignment_error = calendar.align(markets, as_of)
        securities = config.securities_for_session(args.session)
        market_data = MarketDataProvider(config).fetch(
            securities,
            sessions,
            fixture,
            offline=bool(args.fixture),
        )
        focus = _focus_securities(
            securities,
            market_data,
            focus_symbols=config.report.get("focus_symbols", ()),
            limit=min(30, int(config.report.get("evidence_focus_limit", 30))),
        )
        evidence_bundle = EvidenceCollector(config).collect(
            securities,
            as_of=as_of,
            fixture=fixture,
            offline=bool(args.fixture),
            focus_securities=focus,
        )
        report = build_report(
            config=config,
            session=args.session,
            as_of=as_of,
            sessions=sessions,
            aligned=aligned,
            alignment_error=alignment_error,
            market_data=market_data,
            evidence=evidence_bundle.evidence,
            source_statuses=evidence_bundle.statuses,
            source_gaps=evidence_bundle.gaps,
            raw_evidence=evidence_bundle.raw_archive,
            strict=False,
            evidence_coverage=evidence_bundle.coverage,
        )
        strict_reasons: list[str] = []
        if not aligned:
            strict_reasons.append(alignment_error or "selected markets have no completed session")
        if not securities:
            strict_reasons.append("selected session has no securities in universe")
        if report["data_quality"] != "full":
            strict_reasons.append(f"data_quality={report['data_quality']}")
        if not evidence_bundle.evidence:
            strict_reasons.append("no authoritative evidence passed validation")
        hard_failure = (
            not aligned
            or not securities
            or report["data_quality"] in {"no_data", "date_misaligned"}
        )
        if hard_failure or (args.strict and strict_reasons):
            report["status"] = "failed"
            report["report_status"] = "failed"
            report["failure_reasons"] = strict_reasons or [f"data_quality={report['data_quality']}"]
        paths = save_report(report, args.output)
        summary = {
            "status": report.get("status"),
            "data_quality": report["data_quality"],
            "coverage": report["coverage"],
            "files": {k: str(v) for k, v in paths.items()},
        }
        print(json.dumps(summary, ensure_ascii=False))
        if hard_failure or (args.strict and strict_reasons):
            reasons = report.get("failure_reasons", strict_reasons)
            print(f"healthcare report failed (artifact saved): {'; '.join(reasons)}", file=sys.stderr)
            return 2
        return 0
    except (ConfigError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"healthcare report failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
