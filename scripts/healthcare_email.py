"""Send a generated healthcare report by SMTP without exposing credentials.

The report generator owns facts and evidence.  This adapter only sends the
already-rendered Markdown/HTML when the JSON status is complete or explicitly
partial.  Missing SMTP secrets are a successful no-op so Actions still keeps
the artifact and writes a clear summary.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path


ALLOWED_STATUS = {"complete", "partial", "ok"}


def _latest(report_dir: Path, suffix: str) -> Path | None:
    files = sorted(report_dir.glob(f"*{suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _load_status(report_dir: Path) -> tuple[dict, Path | None]:
    for path in sorted(report_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and ("status" in data or "report_status" in data or "data_quality" in data or "coverage" in data):
            return data, path
    return {}, None


def _recipients(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def _coverage_summary(status: dict) -> str:
    """Return a bounded quote-coverage label for an email subject.

    This deliberately says ``行情覆盖`` only.  Quote coverage is not event or
    catalyst coverage, so the deployment-check subject also states that those
    sections remain source-dependent.
    """

    coverage = status.get("coverage") if isinstance(status.get("coverage"), dict) else {}
    requested = coverage.get("requested")
    covered = coverage.get("covered")
    if requested is None:
        requested = coverage.get("quote_requested")
    if covered is None:
        covered = coverage.get("quote_covered")
    try:
        requested_int = int(requested)
        covered_int = int(covered)
    except (TypeError, ValueError):
        return "覆盖范围见报告"
    return f"行情覆盖{covered_int}/{requested_int}；事件/催化剂按官方来源可得性"


def _send(message: EmailMessage, *, sender: str, password: str, host: str, port: int, mode: str) -> None:
    context = ssl.create_default_context()
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(sender, password)
            server.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(sender, password)
        server.send_message(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--session", required=True, choices=("us", "ah"))
    parser.add_argument(
        "--subject-prefix",
        default="",
        help="optional exact subject prefix, e.g. [部署验收样本]; intended for a validated deployment smoke",
    )
    args = parser.parse_args(argv)

    report_dir = Path(args.report_dir)
    status, status_path = _load_status(report_dir)
    quality_raw = status.get("data_quality")
    if isinstance(quality_raw, dict):
        quality_value = quality_raw.get("overall") or quality_raw.get("status") or quality_raw.get("level")
    else:
        quality_value = quality_raw
    status_value = str(status.get("status") or status.get("report_status") or "").lower()
    if not status_value:
        # The healthcare renderer uses data_quality as its stable top-level
        # contract. Translate it locally for delivery gating; never turn a
        # degraded/no-data report into a successful delivery.
        status_value = {"full": "complete", "degraded": "partial"}.get(
            str(quality_value or "").lower(), "failed"
        )
    if status_path is None:
        print("EMAIL_SKIPPED: no report status JSON found; artifacts are retained", file=sys.stderr)
        return 0
    if status_value not in ALLOWED_STATUS:
        print(f"EMAIL_SKIPPED: report status={status_value or 'unknown'}", file=sys.stderr)
        return 0

    sender = os.getenv("EMAIL_SENDER", "").strip()
    password = os.getenv("EMAIL_PASSWORD", "")
    # EMAIL_RECEIVERS is the upstream project's name. EMAIL_TO remains a
    # local compatibility alias for people running the adapter manually.
    recipients = _recipients(os.getenv("EMAIL_RECEIVERS", os.getenv("EMAIL_TO", "")))
    if not sender or not password or not recipients:
        print("EMAIL_DISABLED: set EMAIL_SENDER, EMAIL_PASSWORD and EMAIL_RECEIVERS GitHub secrets", file=sys.stderr)
        return 0

    markdown_path = _latest(report_dir, ".md")
    html_path = _latest(report_dir, ".html")
    if markdown_path is None:
        print("EMAIL_SKIPPED: report Markdown was not found", file=sys.stderr)
        return 0
    markdown = markdown_path.read_text(encoding="utf-8")
    coverage = status.get("coverage") if isinstance(status.get("coverage"), dict) else {}
    gap = bool(
        coverage.get("gaps")
        or coverage.get("uncovered")
        or coverage.get("missing")
        or coverage.get("uncovered_symbols")
        or status.get("source_gaps")
    )
    for key in ("events", "catalysts", "benchmarks"):
        nested = coverage.get(key)
        if isinstance(nested, dict):
            requested = nested.get("requested")
            covered = nested.get("covered")
            try:
                if requested is not None and covered is not None and int(covered) < int(requested):
                    gap = True
            except (TypeError, ValueError):
                gap = True
    for key in ("event_coverage", "catalyst_coverage", "evidence_coverage"):
        value = status.get(key)
        if isinstance(value, dict):
            value = value.get("status") or value.get("quality") or value.get("level")
        if value is not None and str(value).lower() not in {"full", "complete", "ok"}:
            gap = True
    label = "美股晨报" if args.session == "us" else "A/H晚报"
    if args.subject_prefix:
        # Keep deployment-check mail visibly scoped to the small smoke config;
        # never describe it as the 1,848-name industry universe.
        scope = "美股样本" if args.session == "us" else "A/H样本"
        subject = f"{args.subject_prefix}{scope}（{_coverage_summary(status)}）"
    else:
        subject = f"医药行业{label}"
        if gap or status_value == "partial":
            subject += " [部分覆盖]"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(markdown)
    if html_path is not None:
        msg.add_alternative(html_path.read_text(encoding="utf-8"), subtype="html")
    host = os.getenv("EMAIL_SMTP_HOST", "").strip()
    if not host:
        domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
        host = {"qq.com": "smtp.qq.com", "163.com": "smtp.163.com", "gmail.com": "smtp.gmail.com"}.get(domain, "")
    if not host:
        print("EMAIL_DISABLED: set EMAIL_SMTP_HOST for this sender domain", file=sys.stderr)
        return 0
    mode = os.getenv("EMAIL_SMTP_MODE", "").strip().lower() or "ssl"
    if mode not in {"ssl", "starttls"}:
        print("EMAIL_FAILED: EMAIL_SMTP_MODE must be ssl or starttls", file=sys.stderr)
        return 1
    port_value = os.getenv("EMAIL_SMTP_PORT", "").strip()
    try:
        port = int(port_value) if port_value else (465 if mode == "ssl" else 587)
    except ValueError:
        print("EMAIL_FAILED: EMAIL_SMTP_PORT must be an integer", file=sys.stderr)
        return 1
    try:
        _send(msg, sender=sender, password=password, host=host, port=port, mode=mode)
    except Exception as exc:  # do not print credentials or message contents
        print(f"EMAIL_FAILED: SMTP delivery failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"EMAIL_SENT: {label} report delivered to {len(recipients)} recipient(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
