from __future__ import annotations

import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import init_app_db, init_db
from app.request_analytics import (
    build_analytics_report,
    parse_user_agent,
    record_request_analytics,
    should_record_analytics,
    AnalyticsWriter,
)


class AnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bus.db"
        self.app_db_path = self.db_path.parent / "app.db"
        init_db(self.db_path)
        init_app_db(self.app_db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_app_user_agent(self) -> None:
        parsed = parse_user_agent("YABus/1.2.3-a1b2c3d4 (Android)")

        self.assertEqual(parsed.client_family, "app")
        self.assertEqual(parsed.app_version, "1.2.3")
        self.assertEqual(parsed.app_commit_hash, "a1b2c3d4")
        self.assertEqual(parsed.platform_name, "Android")
        self.assertEqual(parsed.system_name, "Android")

    def test_parse_web_user_agent(self) -> None:
        parsed = parse_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        )

        self.assertEqual(parsed.client_family, "web")
        self.assertEqual(parsed.browser_name, "Chrome")
        self.assertEqual(parsed.browser_version, "136.0.0.0")
        self.assertEqual(parsed.platform_name, "desktop-web")
        self.assertEqual(parsed.system_name, "Windows")
        self.assertEqual(parsed.system_version, "10.0")

    def test_build_report_groups_app_and_web_requests(self) -> None:
        now = int(time.time())
        ten_days_ago = now - (10 * 86400)

        record_request_analytics(
            self.app_db_path,
            method="GET",
            endpoint="/api/v1/routes/{routeid}/realtime",
            path="/api/v1/routes/TPE307/realtime",
            status_code=200,
            user_agent="YABus/1.2.3-a1b2c3d4 (Android)",
            requested_at=now,
        )
        record_request_analytics(
            self.app_db_path,
            method="GET",
            endpoint="/api/v1/routes/{routeid}/realtime",
            path="/api/v1/routes/TPE307/realtime",
            status_code=200,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            requested_at=now,
        )
        record_request_analytics(
            self.app_db_path,
            method="GET",
            endpoint="/api/v1/routes/{routeid}/stops",
            path="/api/v1/routes/TPE307/stops",
            status_code=200,
            user_agent="YABus/0.9.0-deadbeef (iOS)",
            requested_at=ten_days_ago,
        )

        recent_report = build_analytics_report(self.app_db_path, days=7, limit=20)
        all_time_report = build_analytics_report(self.app_db_path, days=0, limit=20)

        self.assertEqual(recent_report["summary"]["total_requests"], 2)
        self.assertEqual(all_time_report["summary"]["total_requests"], 3)
        self.assertEqual(recent_report["summary"]["unique_app_versions"], 1)
        self.assertEqual(recent_report["summary"]["unique_browsers"], 1)
        self.assertEqual(len(recent_report["endpoints"]), 1)
        self.assertEqual(recent_report["endpoints"][0]["app_requests"], 1)
        self.assertEqual(recent_report["endpoints"][0]["web_requests"], 1)
        self.assertEqual(recent_report["app_usage"][0]["app_version"], "1.2.3")
        self.assertEqual(recent_report["web_usage"][0]["browser_name"], "Chrome")

    def test_should_skip_admin_analytics_routes(self) -> None:
        self.assertFalse(should_record_analytics("/admin/analytics"))
        self.assertFalse(should_record_analytics("/api/v1/admin/analytics"))

    def test_writer_flushes_accepted_events_on_shutdown(self) -> None:
        writer = AnalyticsWriter(self.app_db_path, flush_interval=0.01)
        writer.start()
        for _ in range(5):
            self.assertTrue(writer.submit(method="GET", endpoint="/route", path="/route",
                                          status_code=200, user_agent="YABus/1.0 (Android)"))
        self.assertTrue(writer.close())
        self.assertEqual(writer.written, 5)
        self.assertEqual(build_analytics_report(self.app_db_path)["summary"]["total_requests"], 5)
        self.assertFalse(writer.submit(method="GET", endpoint="/route", path="/route",
                                       status_code=200, user_agent=None))

    def test_slow_database_does_not_block_submit_and_queue_is_bounded(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def slow_write(*_args):
            entered.set()
            release.wait(3)

        writer = AnalyticsWriter(self.app_db_path, capacity=1, batch_size=1)
        event = dict(method="GET", endpoint="/route", path="/route", status_code=200, user_agent=None)
        with patch("app.request_analytics.record_request_analytics_batch", side_effect=slow_write):
            writer.start()
            try:
                self.assertTrue(writer.submit(**event))
                self.assertTrue(entered.wait(1))
                self.assertTrue(writer.submit(**event))
                self.assertFalse(writer.submit(**event))
                self.assertEqual(writer.dropped, 1)
            finally:
                release.set()
                self.assertTrue(writer.close())

    def test_failed_write_does_not_stop_the_background_writer(self) -> None:
        writer = AnalyticsWriter(self.app_db_path, batch_size=1)
        event = dict(method="GET", endpoint="/route", path="/route", status_code=200, user_agent=None)
        with patch("app.request_analytics.record_request_analytics_batch",
                   side_effect=[RuntimeError("database busy"), None]):
            writer.start()
            writer.submit(**event)
            writer.submit(**event)
            self.assertTrue(writer.close())
        self.assertEqual(writer.written, 1)
        self.assertEqual(writer.dropped, 1)

    def test_should_skip_the_city_bus_map_poll(self) -> None:
        # The map polls this every ~15 s per open screen; recording every poll
        # would drown out every other endpoint in the table.
        self.assertFalse(should_record_analytics("/api/v1/cities/{city}/buses"))
        self.assertTrue(should_record_analytics("/api/v1/cities/{city}/routes"))


if __name__ == "__main__":
    unittest.main()
