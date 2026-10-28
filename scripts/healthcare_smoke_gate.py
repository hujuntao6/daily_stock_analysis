"""Preserve the actual status of a small real-market smoke run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    root = Path(args.report_dir)
    report_path = root / "healthcare_report.json"
    if not report_path.exists():
        print(f"{args.label}: no report JSON; smoke unavailable")
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    coverage = report.get("coverage") or {}
    covered = coverage.get("covered")
    requested = coverage.get("requested")
    pct = coverage.get("coverage_pct")
    quality_raw = report.get("data_quality")
    if isinstance(quality_raw, dict):
        quality_raw = quality_raw.get("overall") or quality_raw.get("status") or quality_raw.get("level")
    quality = str(quality_raw or report.get("status") or "unknown").lower()
    status = str(report.get("status") or report.get("report_status") or "").lower()
    print(f"{args.label}: data_quality={quality} coverage={covered}/{requested} ({pct}%)")
    # This gate checks that the live read-only path returned usable prices for
    # the sample. It intentionally does not claim that events/catalysts are
    # complete; those are reported as source gaps by the report itself.
    if quality in {"failed", "no_data", "date_misaligned", "unknown"} or status == "failed":
        return 1
    try:
        if requested and (float(pct) < 50.0):
            return 1
    except (TypeError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
