from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.healthcare.catalysts import extract_catalysts
from src.healthcare.event_extraction import extract_forward_event
from src.healthcare.models import Evidence


class ForwardEventExtractionTests(TestCase):
    AS_OF = date(2026, 10, 27)

    def test_pdufa_target_action_date_is_bound_to_same_sentence(self):
        item = extract_forward_event(
            "The PDUFA target action date is December 1, 2026.",
            self.AS_OF,
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.event_date, date(2026, 12, 1))
        self.assertEqual(item.event_window_end, date(2026, 12, 1))
        self.assertEqual(item.precision, "day")

    def test_clinical_readout_date_requires_forward_topic(self):
        item = extract_forward_event(
            "The company expects clinical data readout on December 1, 2026.",
            self.AS_OF,
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.event_date, date(2026, 12, 1))

    def test_topic_date_without_a_forward_cue_is_rejected(self):
        self.assertIsNone(
            extract_forward_event(
                "Clinical data readout on December 1, 2026.",
                self.AS_OF,
            )
        )

    def test_current_month_and_quarter_windows_keep_precision(self):
        month = extract_forward_event("预计2026年11月披露临床结果。", self.AS_OF)
        quarter = extract_forward_event("公司预计2026年Q4公布临床数据。", self.AS_OF)
        self.assertIsNotNone(month)
        self.assertIsNotNone(quarter)
        assert month is not None and quarter is not None
        self.assertEqual((month.event_date, month.event_window_end, month.precision), (date(2026, 11, 1), date(2026, 11, 30), "month"))
        self.assertEqual((quarter.event_date, quarter.event_window_end, quarter.precision), (date(2026, 10, 1), date(2026, 12, 31), "quarter"))

    def test_historical_financial_quarter_without_forward_word_is_rejected(self):
        self.assertIsNone(extract_forward_event("2027年Q4业绩已公布。", self.AS_OF))
        self.assertIsNone(extract_forward_event("Q4 2027 earnings were already reported.", self.AS_OF))

    def test_unrelated_debt_date_with_expected_word_is_rejected(self):
        self.assertIsNone(
            extract_forward_event(
                "The debt maturity date is expected on June 1, 2027.",
                self.AS_OF,
            )
        )

    def test_out_of_horizon_date_is_rejected(self):
        self.assertIsNone(
            extract_forward_event(
                "The company expects clinical data readout on January 1, 2028.",
                self.AS_OF,
            )
        )

    def test_year_precision_is_supported_for_explicit_forward_earnings(self):
        item = extract_forward_event("预计2027年度业绩将在年末披露。", self.AS_OF)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.event_date, date(2027, 1, 1))
        self.assertEqual(item.event_window_end, date(2027, 12, 31))
        self.assertEqual(item.precision, "year")

    def test_historical_chinese_financial_quarter_is_not_a_future_window(self):
        self.assertIsNone(
            extract_forward_event(
                "2026年第三季度财务报告已发布；公司将持续推进研发。",
                self.AS_OF,
            )
        )

    def test_approval_announcement_is_not_promoted_by_unrelated_forward_cue(self):
        self.assertIsNone(
            extract_forward_event(
                "公司于2026年10月20日已获临床批件，后续将按计划商业化。",
                self.AS_OF,
            )
        )

    def test_future_earnings_meeting_keeps_explicit_day_precision(self):
        item = extract_forward_event(
            "公司预计将于2026年11月15日召开业绩说明会。",
            self.AS_OF,
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual((item.event_date, item.event_window_end, item.precision), (date(2026, 11, 15), date(2026, 11, 15), "day"))

    def test_chinese_quarter_readout_is_kept_as_a_quarter_window(self):
        item = extract_forward_event("公司预计2026年第四季度读出临床数据。", self.AS_OF)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual((item.event_date, item.event_window_end, item.precision), (date(2026, 10, 1), date(2026, 12, 31), "quarter"))


class CatalystEvidenceBindingTests(TestCase):
    AS_OF = date(2026, 10, 27)
    RETRIEVED = datetime(2026, 10, 28, tzinfo=ZoneInfo("UTC"))

    def _evidence(self, source_kind: str) -> Evidence:
        # SEC/AH event_date is the publication date.  The future event must be
        # extracted from the preserved source body, not from filing/notice day.
        return Evidence(
            id=source_kind,
            url="https://sec.gov/example" if source_kind == "sec_filing" else "https://www1.hkexnews.hk/example.pdf",
            publisher=source_kind,
            published_at=datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("UTC")),
            retrieved_at=self.RETRIEVED,
            snippet="The PDUFA target action date is December 1, 2026.",
            identifiers=("ABC",),
            source_kind=source_kind,
            event_date=self.AS_OF,
            event_precision="day",
            event_label="8-K" if source_kind == "sec_filing" else "A/H公告",
        )

    def test_sec_body_future_pdufa_is_not_blocked_by_filing_date(self):
        catalysts = extract_catalysts((self._evidence("sec_filing"),), self.AS_OF)
        self.assertTrue(any(item.event_date == date(2026, 12, 1) for item in catalysts))

    def test_ah_body_future_pdufa_is_not_blocked_by_notice_date(self):
        catalysts = extract_catalysts((self._evidence("hkex"),), self.AS_OF)
        self.assertTrue(any(item.event_date == date(2026, 12, 1) for item in catalysts))

    def test_official_filing_does_not_fallback_to_historical_year(self):
        item = Evidence(
            "history",
            "https://www.cninfo.com.cn/example",
            "巨潮资讯",
            datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
            self.RETRIEVED,
            "2026年第三季度财务报告已发布；公司将持续推进研发。",
            ("ABC",),
            source_kind="cninfo",
            event_date=self.AS_OF,
            event_precision="day",
        )
        self.assertEqual(extract_catalysts((item,), self.AS_OF), ())

    def test_period_catalyst_keeps_original_window_and_not_future_seven_days(self):
        item = Evidence(
            "period",
            "https://sec.gov/example",
            "SEC",
            None,
            self.RETRIEVED,
            "公司预计2026年Q4披露临床数据。",
            ("ABC",),
            source_kind="sec_filing",
        )
        catalysts = extract_catalysts((item,), self.AS_OF)
        self.assertEqual(len(catalysts), 1)
        catalyst = catalysts[0]
        self.assertEqual((catalyst.window_start, catalyst.window_end), (date(2026, 10, 1), date(2026, 12, 31)))
        self.assertNotEqual(catalyst.window, "未来7天")

    def test_future_month_window_uses_conservative_end_anchor(self):
        item = Evidence(
            "month",
            "https://sec.gov/example-month",
            "SEC",
            None,
            self.RETRIEVED,
            "公司预计2026年11月披露临床结果。",
            ("ABC",),
            source_kind="sec_filing",
        )
        catalysts = extract_catalysts((item,), self.AS_OF)
        self.assertEqual(len(catalysts), 1)
        self.assertEqual(catalysts[0].window, "月份窗口（未来90天）")

    def test_year_window_crossing_ninety_days_is_date_pending(self):
        item = Evidence(
            "year",
            "https://sec.gov/example-year",
            "SEC",
            None,
            self.RETRIEVED,
            "公司预计2027年度披露业绩结果。",
            ("ABC",),
            source_kind="sec_filing",
        )
        catalysts = extract_catalysts((item,), self.AS_OF)
        self.assertEqual(len(catalysts), 1)
        self.assertEqual(catalysts[0].window, "年度窗口（窗口跨越90天，日期待定）")


if __name__ == "__main__":
    import unittest

    unittest.main()
