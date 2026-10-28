from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import requests


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.healthcare import ah_sources
from src.healthcare.models import Security


class FakeResponse:
    def __init__(self, *, url: str, payload=None, content: bytes = b"", status_code: int = 200):
        self.url = url
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": "application/pdf" if content else "application/json"}
        self.text = content.decode("utf-8", errors="replace") if content else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class FakeTransport:
    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def session(self):
        return FakeSession(self)


class FakeSession:
    def __init__(self, transport: FakeTransport):
        self.transport = transport

    def get(self, url, **kwargs):
        return self._call("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, kwargs)

    def _call(self, method, url, kwargs):
        self.transport.calls.append((method, url, kwargs))
        handler = self.transport.handlers.get((method, url)) or self.transport.handlers.get(url)
        if handler is None:
            raise AssertionError(f"unexpected request: {method} {url}")
        return handler(self.transport.calls[-1]) if callable(handler) else handler


class AHSourceTests(TestCase):
    def setUp(self):
        ah_sources.clear_mapping_cache()

    def tearDown(self):
        ah_sources.clear_mapping_cache()

    def test_cninfo_uses_org_id_and_preserves_date_only_metadata_when_pdf_body_unavailable(self):
        security = Security("600276.SH", "恒瑞医药", "cn")
        query_url = ah_sources.CNINFO_QUERY_URL
        pdf_url = "https://static.cninfo.com.cn/finalpage/2026-10-27/1225623696.PDF"
        transport = FakeTransport(
            {
                ah_sources.CNINFO_STOCKS_URL: FakeResponse(
                    url=ah_sources.CNINFO_STOCKS_URL,
                    payload={"stockList": [{"code": "600276", "orgId": "gssh0600276", "zwjc": "恒瑞医药"}]},
                ),
                query_url: FakeResponse(
                    url=query_url,
                    payload={
                        "totalAnnouncement": 1,
                        "announcements": [
                            {
                                "announcementId": "1225623696",
                                "announcementTitle": "恒瑞医药关于获得药物临床试验批准通知书的公告",
                                "announcementTime": 1793030400000,
                                "adjunctUrl": "finalpage/2026-10-27/1225623696.PDF",
                            }
                        ],
                    },
                ),
                pdf_url: FakeResponse(url=pdf_url, content=b"not-a-readable-pdf"),
            }
        )
        with patch.object(ah_sources, "_SESSION_FACTORY", transport.session):
            events, statuses = ah_sources.collect_ah_announcements([security], "2026-10-27", max_workers=1)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["source_kind"], "cninfo")
        self.assertEqual(event["url"], pdf_url)
        self.assertEqual(event["published_at"], "2026-10-27")
        self.assertEqual(event["published_precision"], "date")
        self.assertEqual(event["snippet"], event["title"])
        self.assertTrue(event["body_unavailable"])
        self.assertTrue(event["raw"]["body_unavailable"])
        self.assertEqual(statuses, [{"source": "cninfo", "status": "ok", "count": 1, "detail": "ok", "symbol": "600276.SH"}])
        query_calls = [call for call in transport.calls if call[0] == "POST"]
        self.assertEqual(query_calls[0][2]["data"]["stock"], "600276,gssh0600276")

    def test_hkex_parses_nested_json_and_minute_timestamp_with_original_pdf(self):
        security = Security("00700.HK", "腾讯控股", "hk")
        query_url = ah_sources.HKEX_QUERY_URL
        pdf_url = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0825/2026082500556.pdf"
        row = {
            "NEWS_ID": "12297761",
            "STOCK_CODE": "00700<br/>80700",
            "STOCK_NAME": "TENCENT<br/>TENCENT-R",
            "TITLE": "INTERIM REPORT 2026",
            "DATE_TIME": "25/08/2026 16:53",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0825/2026082500556.pdf",
            "FILE_TYPE": "PDF",
        }
        transport = FakeTransport(
            {
                ah_sources.HKEX_STOCKS_URL: FakeResponse(
                    url=ah_sources.HKEX_STOCKS_URL,
                    payload=[{"i": 7609, "c": "00700", "n": "TENCENT", "s": 14988}],
                ),
                query_url: FakeResponse(
                    url=query_url,
                    payload={"result": json.dumps([row]), "hasNextRow": False, "rowRange": 3},
                ),
                pdf_url: FakeResponse(url=pdf_url, content=b"%PDF-fixture"),
            }
        )
        with patch.object(ah_sources, "_SESSION_FACTORY", transport.session), patch.object(
            ah_sources, "_extract_pdf_text", return_value=("正文摘录来自 PDF", None)
        ):
            events, statuses = ah_sources.collect_ah_announcements(
                [security], datetime(2026, 8, 25, 17, 0, tzinfo=ah_sources.ZoneInfo("Asia/Shanghai")), max_workers=1
            )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["source_kind"], "hkex")
        self.assertEqual(event["published_precision"], "minute")
        self.assertEqual(event["published_at"], "2026-08-25T16:53:00+08:00")
        self.assertEqual(event["snippet"], "正文摘录来自 PDF")
        self.assertEqual(event["raw"]["body_text"], "正文摘录来自 PDF")
        self.assertFalse(event["body_unavailable"])
        self.assertFalse(event["raw"]["body_unavailable"])
        self.assertEqual(statuses[0]["count"], 1)
        query_calls = [call for call in transport.calls if call[0] == "GET" and call[1] == query_url]
        self.assertEqual(query_calls[0][2]["params"]["stockId"], "7609")
        self.assertEqual(query_calls[0][2]["params"]["t1code"], "-2")
        self.assertEqual(query_calls[0][2]["params"]["t2Gcode"], "-2")
        self.assertEqual(query_calls[0][2]["params"]["t2code"], "-2")

    def test_future_hkex_row_is_not_created_and_http_failure_retries_once(self):
        security = Security("00700.HK", "腾讯控股", "hk")
        query_url = ah_sources.HKEX_QUERY_URL
        failures = {"count": 0}

        def failed_query(_call):
            failures["count"] += 1
            return FakeResponse(url=query_url, payload={}, status_code=503)

        transport = FakeTransport(
            {
                ah_sources.HKEX_STOCKS_URL: FakeResponse(
                    url=ah_sources.HKEX_STOCKS_URL, payload=[{"i": 7609, "c": "00700", "n": "TENCENT"}]
                ),
                query_url: failed_query,
            }
        )
        with patch.object(ah_sources, "_SESSION_FACTORY", transport.session):
            events, statuses = ah_sources.collect_ah_announcements([security], "2026-08-25", max_workers=1)

        self.assertEqual(events, [])
        self.assertEqual(failures["count"], 2)
        self.assertEqual(statuses[0]["status"], "error")
        self.assertIn("HTTP 503", statuses[0]["detail"])

    def test_redirect_to_non_official_host_fails_closed(self):
        security = Security("600276.SH", "恒瑞医药", "cn")
        query_url = ah_sources.CNINFO_QUERY_URL
        transport = FakeTransport(
            {
                ah_sources.CNINFO_STOCKS_URL: FakeResponse(
                    url=ah_sources.CNINFO_STOCKS_URL,
                    payload={"stockList": [{"code": "600276", "orgId": "gssh0600276"}]},
                ),
                query_url: FakeResponse(
                    url="https://evil.example/redirect", payload={"announcements": []}
                ),
            }
        )
        with patch.object(ah_sources, "_SESSION_FACTORY", transport.session):
            events, statuses = ah_sources.collect_ah_announcements([security], "2026-10-27", max_workers=1)

        self.assertEqual(events, [])
        self.assertEqual(statuses[0]["status"], "error")
        self.assertIn("official HTTPS host validation", statuses[0]["detail"])


if __name__ == "__main__":
    import unittest

    unittest.main()
