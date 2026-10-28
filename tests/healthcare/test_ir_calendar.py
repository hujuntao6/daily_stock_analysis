from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.healthcare.catalysts import extract_catalysts
from src.healthcare.config import HealthcareConfig, load_config
from src.healthcare.evidence import EvidenceBundle, EvidenceCollector
from src.healthcare.models import Security


class IRAndCalendarEvidenceTests(TestCase):
    def test_production_calendar_path_loads_four_nodes_and_catalysts(self):
        config = load_config(str(ROOT / "config" / "healthcare.json"))
        collector = EvidenceCollector(config)
        bundle = EvidenceBundle()

        collector._collect_calendar_files(bundle)

        self.assertEqual(len(bundle.evidence), 4)
        self.assertEqual(bundle.statuses[-1].status, "ok")
        self.assertEqual(bundle.statuses[-1].count, 4)
        self.assertEqual(len(extract_catalysts(bundle.evidence, date(2026, 10, 27))), 4)
        first = bundle.evidence[0]
        self.assertIsNone(first.published_at)
        self.assertEqual(first.published_precision, "unknown")
        self.assertEqual(first.event_precision, "day")
        self.assertEqual(first.event_status, "confirmed")
        raw = json.loads(bundle.raw_archive[first.id])
        self.assertEqual(raw["verified_at"], "2026-10-27T13:53:55Z")

    def test_ir_rss_uses_namespaced_content_and_retains_old_forward_catalyst(self):
        security = Security(
            "PFE",
            "Pfizer",
            "us",
            official_domains=("ir.example.com",),
            ir_feeds=("https://ir.example.com/rss",),
        )
        config = HealthcareConfig(
            timezone="Asia/Shanghai",
            universe=(security,),
            provider={},
            source_policy={"ir": True, "ir_lookback_days": 7},
            report={},
        )
        rss = """
        <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel>
            <item>
              <title>Pipeline update</title>
              <link>https://ir.example.com/news/current</link>
              <pubDate>Tue, 27 Oct 2026 09:00:00 +0000</pubDate>
              <description><![CDATA[Official summary without the date.]]></description>
              <content:encoded><![CDATA[The company expects clinical data readout on December 1, 2026.]]></content:encoded>
            </item>
            <item>
              <title>Old pipeline update</title>
              <link>https://ir.example.com/news/old</link>
              <pubDate>Mon, 1 Jan 2024 09:00:00 +0000</pubDate>
              <content:encoded><![CDATA[The company expects clinical data readout on December 1, 2026.]]></content:encoded>
            </item>
            <item>
              <title>Cross-domain copy</title>
              <link>https://search.example.com/result</link>
              <pubDate>Tue, 27 Oct 2026 09:00:00 +0000</pubDate>
              <description><![CDATA[The company expects clinical data readout on December 1, 2026.]]></description>
            </item>
          </channel>
        </rss>
        """
        collector = EvidenceCollector(config)
        bundle = EvidenceBundle()
        with patch.object(collector, "_fetch", return_value=(rss, "https://ir.example.com/rss")):
            collector._collect_feeds(security, bundle, as_of_date=date(2026, 10, 27))

        # The old announcement is retained because it contains an explicit
        # still-future catalyst; its publication date remains old and is not
        # eligible for same-day attribution downstream.
        self.assertEqual(len(bundle.evidence), 2)
        self.assertEqual(bundle.statuses[-1].status, "ok")
        self.assertEqual(bundle.statuses[-1].count, 2)
        item = bundle.evidence[0]
        self.assertIn("clinical data readout", item.snippet)
        self.assertEqual(item.event_date, date(2026, 12, 1))
        self.assertEqual(item.event_precision, "day")
        self.assertTrue(any(item.published_at and item.published_at.year == 2024 for item in bundle.evidence))
        self.assertFalse(any("旧公告跳过" in gap for gap in bundle.gaps))
        self.assertTrue(any("IR 证据拒绝" in gap for gap in bundle.gaps))

    def test_ir_json_listing_fetches_official_details_and_preserves_date_precision(self):
        security = Security(
            "AMGN",
            "Amgen",
            "us",
            official_domains=("amgen.com",),
            ir_feeds=("https://www.amgen.com/MyApi/Custom/NewsListing/GetRecentNewsReleasesV2",),
        )
        config = HealthcareConfig(
            timezone="Asia/Shanghai",
            universe=(security,),
            provider={},
            source_policy={"ir": True},
            report={},
        )
        listing_url = security.ir_feeds[0]
        detail_url = "https://www.amgen.com/newsroom/press-releases/2026/10/pipeline-update"
        listing = json.dumps(
            [
                {
                    "Month": "October",
                    "NewsReleases": [
                        {"Title": "Pipeline update", "Date": "10.27.2026", "Url": "/newsroom/press-releases/2026/10/pipeline-update"}
                    ],
                }
            ]
        )
        detail = """
        <html><head>
          <meta property="article:published_time" content="2026-10-27">
          <meta property="og:title" content="Pipeline update">
        </head><body><nav>Navigation must not be evidence.</nav>
          <article><h1>Pipeline update</h1>
          <p>The company expects clinical data readout on December 1, 2026.</p>
          </article><footer>Legal navigation</footer>
        </body></html>
        """

        def fake_fetch(url, _domains):
            if url == listing_url:
                return listing, listing_url
            self.assertEqual(url, detail_url)
            return detail, detail_url

        collector = EvidenceCollector(config)
        bundle = EvidenceBundle()
        with patch.object(collector, "_fetch", side_effect=fake_fetch):
            collector._collect_feeds(security, bundle, as_of_date=date(2026, 10, 28))

        self.assertEqual(len(bundle.evidence), 1)
        item = bundle.evidence[0]
        self.assertEqual(item.url, detail_url)
        self.assertEqual(item.published_precision, "date")
        self.assertEqual(item.published_at.date(), date(2026, 10, 27))
        self.assertEqual(item.event_date, date(2026, 12, 1))
        self.assertNotIn("Navigation must not be evidence", item.snippet)
        raw = json.loads(bundle.raw_archive[item.id])
        self.assertEqual(raw["detail_url"], detail_url)
        self.assertIn("NewsReleases", raw["listing"])
        self.assertFalse(raw["truncated"])

    def test_ir_html_detail_without_publication_date_stays_unknown(self):
        security = Security(
            "PFE",
            "Pfizer",
            "us",
            official_domains=("ir.example.com",),
            ir_feeds=("https://ir.example.com/news/news-details/dateunknown",),
        )
        config = HealthcareConfig(
            timezone="Asia/Shanghai",
            universe=(security,),
            provider={},
            source_policy={"ir": True},
            report={},
        )
        detail = """
        <html><head><title>Future study update</title></head>
        <body><article><h1>Future study update</h1>
        <p>The company expects clinical data readout on December 1, 2026.</p>
        </article></body></html>
        """
        collector = EvidenceCollector(config)
        bundle = EvidenceBundle()
        with patch.object(
            collector,
            "_fetch",
            return_value=(detail, security.ir_feeds[0]),
        ):
            collector._collect_feeds(security, bundle, as_of_date=date(2026, 10, 28))

        self.assertEqual(len(bundle.evidence), 1)
        item = bundle.evidence[0]
        self.assertIsNone(item.published_at)
        self.assertEqual(item.published_precision, "unknown")
        self.assertEqual(item.event_date, date(2026, 12, 1))


if __name__ == "__main__":
    import unittest

    unittest.main()
