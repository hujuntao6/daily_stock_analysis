from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.healthcare.__main__ import _focus_securities, main
from src.healthcare.market_data import MarketDataResult
from src.healthcare.models import Quote, Security


class HealthcareCliTests(TestCase):
    def test_focus_adds_configured_same_market_symbols_without_duplicate(self):
        securities = (
            Security("MICRO", "微盘药企", "us"),
            Security("PFE", "Pfizer", "us"),
        )
        market_data = MarketDataResult(
            quotes=(
                Quote("MICRO", "微盘药企", "us", "创新药", date(2026, 10, 26), 20, 10),
                Quote("PFE", "Pfizer", "us", "创新药", date(2026, 10, 26), 101, 100),
            ),
            benchmark_quotes=(),
            statuses=(),
            gaps=(),
        )

        selected = _focus_securities(
            securities,
            market_data,
            focus_symbols=("PFE", "PFE", "600276.SS"),
            limit=1,
        )

        self.assertEqual([security.symbol for security in selected], ["MICRO", "PFE"])

    def _config(self, root: Path) -> Path:
        config = {
            "timezone": "Asia/Shanghai",
            "universe": [
                {
                    "market": "us",
                    "symbol": "ABC",
                    "name": "测试药企",
                    "subsector": "创新药",
                    "official_domains": ["sec.gov"],
                    "ir_feeds": [],
                }
            ],
            "provider": {"max_workers": 2},
            "source_policy": {
                "sec": False,
                "ir": False,
                "clinicaltrials": False,
                "clinicaltrials_discovery": False,
            },
            "report": {"benchmarks": {"us": "SPY"}},
        }
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def _fixture(self, root: Path, *, include_evidence: bool = True) -> Path:
        fixture = {
            "quotes": [
                {
                    "market": "us",
                    "symbol": "ABC",
                    "session_date": "2026-10-26",
                    "close": 12,
                    "previous_close": 10,
                    "volume": 200,
                    "average_volume_20": 100,
                }
            ],
            "benchmarks": [
                {"market": "us", "symbol": "SPY", "session_date": "2026-10-26", "close": 101, "previous_close": 100}
            ],
        }
        if include_evidence:
            fixture["evidence"] = [
                {
                    "symbol": "ABC",
                    "url": "https://sec.gov/Archives/edgar/data/example/8k.htm",
                    "publisher": "SEC",
                    "published_at": "2026-10-26T15:00:00-04:00",
                    "event_date": "2026-10-26",
                    "identifiers": ["ABC"],
                    "snippet": "Official filing",
                }
            ]
        path = root / "fixture.json"
        path.write_text(json.dumps(fixture), encoding="utf-8")
        return path

    def test_strict_fixture_saves_complete_artifacts_without_network(self):
        with TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            config = self._config(root)
            fixture = self._fixture(root)
            output = root / "report"
            code = main(
                [
                    "--session",
                    "us",
                    "--config",
                    str(config),
                    "--output",
                    str(output),
                    "--fixture",
                    str(fixture),
                    "--as-of",
                    "2026-10-27T23:59:00+08:00",
                    "--strict",
                    "--no-email",
                ]
            )
            self.assertEqual(code, 0)
            report = json.loads((output / "healthcare_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["coverage"]["covered"], 1)
            self.assertTrue((output / "evidence_archive.jsonl").exists())

    def test_strict_failure_still_saves_failure_artifact(self):
        with TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            config = self._config(root)
            fixture = self._fixture(root, include_evidence=False)
            output = root / "report"
            code = main(
                [
                    "--session",
                    "us",
                    "--config",
                    str(config),
                    "--output",
                    str(output),
                    "--fixture",
                    str(fixture),
                    "--as-of",
                    "2026-10-27T23:59:00+08:00",
                    "--strict",
                ]
            )
            self.assertEqual(code, 2)
            report = json.loads((output / "healthcare_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["failure_reasons"])
            self.assertTrue((output / "healthcare_report.md").exists())

    def test_nonstrict_no_data_is_failed_after_saving_artifact(self):
        with TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            config = self._config(root)
            fixture = self._fixture(root, include_evidence=False)
            fixture.write_text(json.dumps({"quotes": [], "benchmarks": [], "evidence": []}), encoding="utf-8")
            output = root / "report"
            code = main(
                [
                    "--session", "us", "--config", str(config), "--output", str(output),
                    "--fixture", str(fixture), "--as-of", "2026-10-27T23:59:00+08:00",
                ]
            )
            self.assertEqual(code, 2)
            report = json.loads((output / "healthcare_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["data_quality"], "no_data")


if __name__ == "__main__":
    import unittest

    unittest.main()
