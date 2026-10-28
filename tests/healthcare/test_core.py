from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.healthcare.attribution import attribute_moves
from src.healthcare.calendar import Session, TradingCalendar
from src.healthcare.catalysts import extract_catalysts
from src.healthcare.config import HealthcareConfig
from src.healthcare.evidence import EvidenceCollector, is_authoritative_url
from src.healthcare.market_data import MarketDataProvider, _normalise_dataframe
from src.healthcare.models import Evidence, Quote, Security
from src.healthcare.report import build_report, render_html, render_markdown, save_report


def _config(*securities: Security) -> HealthcareConfig:
    return HealthcareConfig(
        timezone="Asia/Shanghai",
        universe=tuple(securities),
        provider={"max_workers": 2},
        source_policy={"sec": False, "ir": False, "clinicaltrials": False, "clinicaltrials_discovery": False},
        report={"benchmarks": {"us": "SPY", "cn": "000300.SS", "hk": "^HSI"}},
    )


class HealthcareCoreTests(TestCase):
    def test_calendar_before_close_and_weekend_are_previous_completed_sessions(self):
        calendar = TradingCalendar()
        calendar._project_calendar = None
        calendar._exchange_calendars = None
        zone = ZoneInfo("Asia/Shanghai")
        before_close = calendar.completed_session("cn", datetime(2026, 10, 27, 14, 0, tzinfo=zone))
        after_close = calendar.completed_session("cn", datetime(2026, 10, 27, 16, 0, tzinfo=zone))
        weekend = calendar.completed_session("cn", datetime(2026, 10, 31, 12, 0, tzinfo=zone))
        self.assertEqual(before_close.session_date, date(2026, 10, 26))
        self.assertEqual(after_close.session_date, date(2026, 10, 27))
        self.assertEqual(weekend.session_date, date(2026, 10, 30))
        self.assertEqual(before_close.source, "weekday_fallback")

    def test_dataframe_date_column_and_previous_volume_baseline(self):
        import pandas as pd

        security = Security("ABC", "测试", "us")
        frame = pd.DataFrame(
            {
                "日期": ["2026-10-23", "2026-10-24", "2026-10-27"],
                "收盘价": [10, 11, 12],
                "成交量": [100, 200, 600],
            }
        )
        quote = _normalise_dataframe(frame, security, date(2026, 10, 27), "fixture-dataframe")
        self.assertIsNotNone(quote)
        self.assertEqual(quote.previous_close, 11)
        self.assertEqual(quote.volume, 600)
        self.assertEqual(quote.average_volume_20, 150)
        self.assertAlmostEqual(quote.volume_ratio, 4.0)

    def test_nan_current_close_is_not_success_and_enters_fallback(self):
        import pandas as pd

        security = Security("600276.SS", "恒瑞医药", "cn", "创新药")
        target = date(2026, 10, 27)
        frame = pd.DataFrame(
            {
                "Close": [47.0, float("nan")],
                "Adj Close": [47.0, float("nan")],
                "Volume": [100.0, 52261260.0],
            },
            index=[date(2026, 10, 26), target],
        )
        self.assertIsNone(_normalise_dataframe(frame, security, target, "yfinance_batch"))

        config = _config(security)
        config.provider["timeout_seconds"] = 7
        provider = MarketDataProvider(config)
        session = Session("cn", target, datetime(2026, 10, 27, 15, tzinfo=ZoneInfo("Asia/Shanghai")), "fixture")
        fallback_quote = Quote(
            security.symbol,
            security.name,
            security.market,
            security.subsector,
            target,
            48.0,
            47.0,
            source="single-fallback",
        )

        def fallback_for(item: Security, _target: date, **_kwargs):
            return fallback_quote if item.symbol == security.symbol else None

        with patch.object(provider, "_fetch_yfinance_batch", return_value={}) as batch, patch.object(
            provider, "_fetch_host", return_value=None
        ), patch.object(provider, "_fetch_yfinance", side_effect=fallback_for) as single, patch.object(
            provider, "_fetch_akshare", return_value=None
        ):
            result = provider.fetch((security,), {"cn": session})

        self.assertEqual(result.quotes[0].source, "single-fallback")
        self.assertGreaterEqual(single.call_count, 1)
        self.assertEqual(batch.call_args_list[0].kwargs["workers"], 2)
        self.assertEqual(batch.call_args_list[0].kwargs["timeout"], 7.0)

    def test_akshare_ah_fallback_uses_verified_symbols_and_target_close(self):
        import types

        import pandas as pd

        calls = []

        def cn_hist(**kwargs):
            calls.append(("cn_hist", kwargs["symbol"]))
            raise ConnectionError("hist endpoint unavailable")

        def cn_daily(**kwargs):
            calls.append(("cn_daily", kwargs["symbol"]))
            return pd.DataFrame(
                {
                    "日期": ["2026-10-26", "2026-10-27"],
                    "收盘": [47.0, 45.55],
                    "成交量": [100.0, 200.0],
                }
            )

        def hk_daily(**kwargs):
            calls.append(("hk_daily", kwargs["symbol"]))
            return pd.DataFrame(
                {
                    "日期": ["2026-10-26", "2026-10-27"],
                    "收盘": [7.80, 7.91],
                    "成交量": [100.0, 200.0],
                }
            )

        fake_akshare = types.SimpleNamespace(
            stock_zh_a_hist=cn_hist,
            stock_zh_a_daily=cn_daily,
            stock_hk_daily=hk_daily,
        )
        config = _config(Security("600276.SS", "恒瑞医药", "cn"), Security("1093.HK", "石药集团", "hk"))
        provider = MarketDataProvider(config)
        target = date(2026, 10, 27)
        with patch.dict(sys.modules, {"akshare": fake_akshare}):
            cn_quote = provider._fetch_akshare(Security("600276.SS", "恒瑞医药", "cn"), target)
            hk_quote = provider._fetch_akshare(Security("1093.HK", "石药集团", "hk"), target)

        self.assertIsNotNone(cn_quote)
        self.assertIsNotNone(hk_quote)
        assert cn_quote is not None and hk_quote is not None
        self.assertEqual((cn_quote.close, cn_quote.previous_close), (45.55, 47.0))
        self.assertEqual((hk_quote.close, hk_quote.previous_close), (7.91, 7.8))
        self.assertEqual(calls, [("cn_hist", "600276"), ("cn_daily", "sh600276"), ("hk_daily", "01093")])

    def test_fixture_mode_never_calls_live_provider_for_missing_rows(self):
        security = Security("ABC", "测试", "us")
        config = _config(security)
        session = Session("us", date(2026, 10, 26), datetime(2026, 10, 26, 16, tzinfo=ZoneInfo("America/New_York")), "fixture")
        provider = MarketDataProvider(config)
        with patch.object(provider, "_fetch_yfinance", side_effect=AssertionError("network call")), patch.object(
            provider, "_fetch_host", side_effect=AssertionError("network call")
        ):
            result = provider.fetch(
                (security,),
                {"us": session},
                {"quotes": [], "benchmarks": []},
                offline=True,
            )
        self.assertEqual(result.quotes[0].status, "no_data")
        self.assertEqual(result.coverage_pct, 0)

    def test_url_validation_is_exact_and_requires_https(self):
        self.assertTrue(is_authoritative_url("https://data.sec.gov/submissions/x", ("sec.gov",)))
        self.assertFalse(is_authoritative_url("http://data.sec.gov/submissions/x", ("sec.gov",)))
        self.assertFalse(is_authoritative_url("https://sec.gov.attacker.example/x", ("sec.gov",)))

    def test_fixture_evidence_rejects_fake_domain_and_accepts_official_source(self):
        security = Security("ABC", "测试", "us", official_domains=("sec.gov",))
        config = _config(security)
        collector = EvidenceCollector(config)
        bundle = collector.collect(
            (security,),
            as_of=datetime(2026, 10, 27, 23, tzinfo=ZoneInfo("Asia/Shanghai")),
            fixture={
                "evidence": [
                    {"symbol": "ABC", "url": "https://evil.example/item", "identifiers": ["ABC"]},
                    {
                        "symbol": "ABC",
                        "url": "https://www.sec.gov/Archives/edgar/data/x/8k.htm",
                        "publisher": "SEC",
                        "published_at": "2026-10-27T10:00:00-04:00",
                        "event_date": "2026-10-27",
                        "identifiers": ["ABC"],
                        "snippet": "Official filing",
                    },
                ]
            },
            offline=True,
        )
        self.assertEqual(len(bundle.evidence), 1)
        self.assertTrue(any("证据拒绝" in gap for gap in bundle.gaps))
        self.assertEqual(bundle.coverage["covered_symbols"], 1)

    def test_attribution_keeps_post_close_out_of_same_day_explanation(self):
        security = Security("ABC", "测试", "us")
        session = Session("us", date(2026, 10, 26), datetime(2026, 10, 26, 16, tzinfo=ZoneInfo("America/New_York")), "fixture")
        quote = Quote("ABC", "测试", "us", "创新药", date(2026, 10, 26), 12, 10)
        pre = Evidence(
            "pre", "https://sec.gov/pre", "SEC", datetime(2026, 10, 26, 15, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 26, 16, tzinfo=ZoneInfo("America/New_York")), "pre", ("ABC",), published_precision="timestamp",
            event_date=date(2026, 10, 26),
        )
        post = Evidence(
            "post", "https://sec.gov/post", "SEC", datetime(2026, 10, 26, 17, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 26, 18, tzinfo=ZoneInfo("America/New_York")), "post", ("ABC",), published_precision="timestamp",
            event_date=date(2026, 10, 26),
        )
        result = attribute_moves((quote,), (post,), {"us": session})
        self.assertEqual(result[0].status, "未找到解释")
        self.assertEqual(result[0].timing, "post_close")
        result = attribute_moves((quote,), (pre,), {"us": session})
        self.assertEqual(result[0].status, "相关事件")
        self.assertEqual(result[0].timing, "pre_close")

    def test_attribution_does_not_cross_market_for_same_numeric_code(self):
        hk_session = Session("hk", date(2026, 10, 27), datetime(2026, 10, 27, 16, tzinfo=ZoneInfo("Asia/Hong_Kong")), "fixture")
        quote = Quote("2003.HK", "港股", "hk", "器械", date(2026, 10, 27), 12, 10)
        event = Evidence(
            "cn", "https://cninfo.com.cn/announcement", "巨潮资讯", datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2026, 10, 27, 11, tzinfo=ZoneInfo("Asia/Shanghai")), "公告", ("002003.SZ",), event_date=None,
        )
        result = attribute_moves((quote,), (event,), {"hk": hk_session})
        self.assertEqual(result[0].status, "未找到解释")

    def test_attribution_minute_date_unknown_and_late_next_day_boundaries(self):
        session = Session("us", date(2026, 10, 26), datetime(2026, 10, 26, 16, tzinfo=ZoneInfo("America/New_York")), "fixture")
        quote = Quote("ABC", "测试", "us", "创新药", date(2026, 10, 26), 12, 10)
        minute = Evidence(
            "minute", "https://sec.gov/minute", "SEC", datetime(2026, 10, 26, 15, 30, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 26, 16, tzinfo=ZoneInfo("America/New_York")), "minute", ("ABC",), published_precision="minute",
        )
        self.assertEqual(attribute_moves((quote,), (minute,), {"us": session})[0].status, "相关事件")
        date_only = Evidence(
            "date", "https://sec.gov/date", "SEC", datetime(2026, 10, 26, 23, 59, tzinfo=ZoneInfo("UTC")),
            datetime(2026, 10, 27, tzinfo=ZoneInfo("UTC")), "date", ("ABC",), published_precision="date",
        )
        self.assertEqual(attribute_moves((quote,), (date_only,), {"us": session})[0].timing, "unknown_time")
        next_day = Evidence(
            "next", "https://sec.gov/next", "SEC", datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 27, 11, tzinfo=ZoneInfo("America/New_York")), "next", ("ABC",), published_precision="timestamp",
        )
        self.assertEqual(attribute_moves((quote,), (next_day,), {"us": session})[0].timing, "post_close")
        trial = Evidence(
            "trial", "https://clinicaltrials.gov/study/NCT00000001", "ClinicalTrials.gov", datetime(2026, 10, 26, 12, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 26, 13, tzinfo=ZoneInfo("America/New_York")), "trial", ("ABC", "NCT00000001"), source_kind="clinicaltrials",
            event_date=date(2026, 10, 27), event_precision="day",
        )
        self.assertEqual(attribute_moves((quote,), (trial,), {"us": session})[0].status, "未找到解释")

    def test_catalyst_keeps_quarter_window_and_ctgov_warning(self):
        quarter = Evidence(
            "q", "https://sec.gov/q", "SEC", None, datetime.now(ZoneInfo("UTC")), "预计2026年Q4披露数据", ("ABC",),
            source_kind="company_ir",
        )
        trial = Evidence(
            "t", "https://clinicaltrials.gov/study/NCT00000001", "ClinicalTrials.gov", None,
            datetime.now(ZoneInfo("UTC")), "trial", ("ABC", "NCT00000001"), source_kind="clinicaltrials",
            event_date=date(2026, 11, 30), event_precision="day", event_label="预计主要完成日期", event_status="estimated",
        )
        catalysts = extract_catalysts((quarter, trial), date(2026, 10, 27))
        self.assertTrue(any(item.precision == "quarter" and item.window_start == date(2026, 10, 1) and item.window_end == date(2026, 12, 31) for item in catalysts))
        self.assertTrue(any(item.precision == "quarter" and item.window != "未来7天" for item in catalysts))
        self.assertTrue(any("不代表读出" in item.note for item in catalysts))
        self.assertTrue(all(item.event_date is None or item.precision == "day" for item in catalysts))

    def test_ctgov_month_type_and_date_only_publication_are_preserved(self):
        security = Security("ABC", "测试", "us", nct_ids=("NCT00000001",))
        config = HealthcareConfig(
            timezone="Asia/Shanghai",
            universe=(security,),
            provider={},
            source_policy={"clinicaltrials": True, "sec": False, "ir": False, "clinicaltrials_discovery": False},
            report={},
        )
        collector = EvidenceCollector(config)
        payload = {
            "protocolSection": {
                "identificationModule": {"briefTitle": "Phase 2 study"},
                "designModule": {"phases": ["PHASE2"]},
                "statusModule": {
                    "overallStatus": "RECRUITING",
                    "primaryCompletionDateStruct": {"date": "2026-11", "type": "ESTIMATED"},
                    "completionDateStruct": {"date": "2027-02-01", "type": "ESTIMATED"},
                    "lastUpdatePostDateStruct": {"date": "2026-10-27"},
                },
            }
        }
        with patch.object(collector, "_fetch", return_value=(json.dumps(payload), "https://clinicaltrials.gov/api/v2/studies/NCT00000001")):
            bundle = collector.collect(
                (security,), as_of=datetime(2026, 10, 27, 23, tzinfo=ZoneInfo("Asia/Shanghai")), offline=False
            )
        item = bundle.evidence[0]
        self.assertEqual(item.event_precision, "month")
        self.assertEqual(item.event_window_end, date(2026, 11, 30))
        self.assertEqual(item.event_status, "estimated")
        self.assertEqual(item.published_precision, "date")

    def test_report_separates_markets_coverage_and_renders_clickable_html(self):
        cn = Security("600001.SS", "甲", "cn", "创新药")
        us = Security("ABC", "乙", "us", "器械")
        config = _config(cn, us)
        sessions = {
            "cn": Session("cn", date(2026, 10, 27), datetime(2026, 10, 27, 15, tzinfo=ZoneInfo("Asia/Shanghai")), "fixture"),
            "us": Session("us", date(2026, 10, 26), datetime(2026, 10, 26, 16, tzinfo=ZoneInfo("America/New_York")), "fixture"),
        }
        from src.healthcare.market_data import MarketDataResult

        quote_cn = Quote("600001.SS", "甲", "cn", "创新药", date(2026, 10, 27), 11, 10, volume=200, average_volume_20=100)
        quote_us = Quote("ABC", "乙", "us", "器械", date(2026, 10, 26), 9, 10, volume=100, average_volume_20=100)
        benchmark_cn = Quote("000300.SS", "基准", "cn", "基准", date(2026, 10, 27), 101, 100)
        benchmark_us = Quote("SPY", "基准", "us", "基准", date(2026, 10, 26), 101, 100)
        event = Evidence(
            "e", "https://sec.gov/filing", "SEC", datetime(2026, 10, 26, 12, tzinfo=ZoneInfo("America/New_York")),
            datetime(2026, 10, 26, 13, tzinfo=ZoneInfo("America/New_York")), "event", ("ABC",), event_date=date(2026, 10, 26),
        )
        report = build_report(
            config=config, session="ah", as_of=datetime(2026, 10, 27, 23, tzinfo=ZoneInfo("Asia/Shanghai")),
            sessions=sessions, aligned=True, alignment_error=None,
            market_data=MarketDataResult((quote_cn, quote_us), (benchmark_cn, benchmark_us), (), ()),
            evidence=[event], source_statuses=[], source_gaps=[], raw_evidence={}, strict=False,
        )
        self.assertIn("cn", report["movers"]["by_market"])
        self.assertIn("us", report["movers"]["by_market"])
        self.assertIn("events", report["coverage"])
        markdown = render_markdown(report)
        html = render_html(markdown, report)
        self.assertIn("涨幅榜", markdown)
        self.assertIn("跌幅榜", markdown)
        self.assertIn("href=", html)
        self.assertNotIn("<pre", html)
        with TemporaryDirectory(dir=ROOT) as temp:
            paths = save_report(report, temp)
            self.assertTrue(paths["json"].exists())
            self.assertTrue(paths["markdown"].exists())
            self.assertTrue(paths["html"].exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
