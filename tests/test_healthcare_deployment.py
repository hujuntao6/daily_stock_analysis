from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.healthcare.config import ConfigError, load_config
from scripts.healthcare_email import main as email_main


class HealthcareDeploymentTests(unittest.TestCase):
    def test_representative_universe_covers_all_requested_markets(self):
        config = load_config(ROOT / "config" / "healthcare.json")
        self.assertEqual(len(config.universe), 1848)
        self.assertEqual({item.market for item in config.universe}, {"cn", "hk", "us"})
        self.assertTrue(config.report["representative_universe"])
        self.assertEqual(config.markets_for_session("us"), ("us",))
        self.assertEqual(config.markets_for_session("ah"), ("cn", "hk"))

    def test_full_loader_preserves_snapshot_metadata_and_overlays_focus(self):
        config = load_config(ROOT / "config" / "healthcare.json")
        self.assertEqual(len(config.raw_universe), 1848)
        self.assertEqual(len(config.report["focus_symbols"]), 37)
        row = next(item for item in config.raw_universe if item["market"] == "cn" and item["symbol"] == "600276.SS")
        self.assertEqual(row["source"], "ifind-cn-complete.json")
        self.assertEqual(row["source_asof"], "2026-10-27")
        self.assertEqual(row["original_symbol"], "600276.SH")
        self.assertEqual(row["sponsor_aliases"], ["Jiangsu HengRui Medicine Co., Ltd."])
        self.assertIn("600276.SS", config.report["focus_symbols"])

    def test_loader_rejects_missing_or_invalid_universe_file(self):
        with TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            missing = root / "missing.json"
            missing.write_text(
                json.dumps(
                    {
                        "timezone": "Asia/Shanghai",
                        "universe_files": ["does-not-exist.json"],
                        "universe": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(missing)
            bad_universe = root / "bad-universe.json"
            bad_universe.write_text("{not-json", encoding="utf-8")
            bad = root / "bad.json"
            bad.write_text(
                json.dumps(
                    {
                        "timezone": "Asia/Shanghai",
                        "universe_files": [str(bad_universe)],
                        "universe": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(bad)

    def test_source_policy_has_authoritative_regulatory_domains(self):
        policy = load_config(ROOT / "config" / "healthcare.json").source_policy
        for domain in ("cninfo.com.cn", "hkexnews.hk", "sec.gov", "clinicaltrials.gov", "fda.gov", "hematology.org", "sitcancer.org"):
            self.assertIn(domain, policy["allowed_domains"])
        self.assertTrue(policy["reject_search_snippet_as_evidence"])
        self.assertEqual(policy["calendar_files"], ["config/healthcare-calendar.json"])

    def test_full_snapshot_counts_and_traceability(self):
        snapshot = json.loads((ROOT / "config" / "healthcare-universe.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["counts"], {"cn": 515, "hk": 302, "us": 1031, "total": 1848, "us_large": 664, "us_small": 367})
        self.assertEqual(len(snapshot["universe"]), 1848)
        self.assertTrue(all(row.get("source") and row.get("source_asof") == "2026-10-27" for row in snapshot["universe"]))
        self.assertEqual(len({row["symbol"] for row in snapshot["universe"] if row["market"] == "us"}), 1031)
        bio = next(row for row in snapshot["universe"] if row.get("original_symbol") == "BIO.B.N")
        self.assertEqual(bio["symbol"], "BIO-B")
        self.assertEqual(bio["original_symbol"], "BIO.B.N")
        hengrui = next(row for row in snapshot["universe"] if row["symbol"] == "600276.SS")
        self.assertEqual(hengrui["sponsor_aliases"], ["Jiangsu HengRui Medicine Co., Ltd."])
        pfizer = next(row for row in snapshot["universe"] if row["symbol"] == "PFE")
        self.assertEqual(pfizer["sponsor_aliases"], ["Pfizer Inc."])

    def test_action_schedule_keeps_two_beijing_windows(self):
        workflow = (ROOT / ".github" / "workflows" / "healthcare-daily.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "30 23 * * 1-5"', workflow)
        self.assertIn('cron: "30 10 * * 1-5"', workflow)
        self.assertIn('retention-days: 60', workflow)

    def test_email_without_secrets_is_safe_noop(self):
        with TemporaryDirectory(dir=ROOT) as temp:
            report_dir = Path(temp)
            (report_dir / "report.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
            (report_dir / "report.md").write_text("# report", encoding="utf-8")
            env = {key: os.environ.pop(key, None) for key in ("EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVERS", "EMAIL_TO")}
            try:
                self.assertEqual(email_main(["--report-dir", str(report_dir), "--session", "us"]), 0)
            finally:
                for key, value in env.items():
                    if value is not None:
                        os.environ[key] = value

    @patch("scripts.healthcare_email._send")
    def test_email_subject_marks_partial_coverage(self, send):
        with TemporaryDirectory(dir=ROOT) as temp:
            report_dir = Path(temp)
            (report_dir / "report.json").write_text(
                json.dumps({"status": "partial", "coverage": {"gaps": ["hk"]}}), encoding="utf-8"
            )
            (report_dir / "report.md").write_text("# report", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "EMAIL_SENDER": "sender@example.com",
                    "EMAIL_PASSWORD": "secret",
                    "EMAIL_RECEIVERS": "to@example.com",
                    "EMAIL_SMTP_HOST": "smtp.example.com",
                    "EMAIL_SMTP_PORT": "465",
                    "EMAIL_SMTP_MODE": "ssl",
                },
                clear=False,
            ):
                self.assertEqual(email_main(["--report-dir", str(report_dir), "--session", "ah"]), 0)
            message = send.call_args.args[0]
            self.assertIn("[部分覆盖]", str(message["Subject"]))

    @patch("scripts.healthcare_email._send")
    def test_deployment_subject_is_sample_scoped(self, send):
        with TemporaryDirectory(dir=ROOT) as temp:
            report_dir = Path(temp)
            (report_dir / "report.json").write_text(
                json.dumps({"status": "partial", "coverage": {"requested": 2, "covered": 1}}),
                encoding="utf-8",
            )
            (report_dir / "report.md").write_text("# sample", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "EMAIL_SENDER": "sender@example.com",
                    "EMAIL_PASSWORD": "secret",
                    "EMAIL_RECEIVERS": "to@example.com",
                    "EMAIL_SMTP_HOST": "smtp.example.com",
                    "EMAIL_SMTP_PORT": "465",
                    "EMAIL_SMTP_MODE": "ssl",
                },
                clear=False,
            ):
                self.assertEqual(
                    email_main(
                        [
                            "--report-dir",
                            str(report_dir),
                            "--session",
                            "us",
                            "--subject-prefix",
                            "[部署验收样本]",
                        ]
                    ),
                    0,
                )
            subject = str(send.call_args.args[0]["Subject"])
            self.assertTrue(subject.startswith("[部署验收样本]美股样本"))
            self.assertIn("行情覆盖1/2", subject)
            self.assertIn("事件/催化剂按官方来源可得性", subject)


if __name__ == "__main__":
    unittest.main()
