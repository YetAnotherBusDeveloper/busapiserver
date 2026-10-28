from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import Settings
from app.discord_webhook import notify_realtime_feed
from app.realtime_health import EmptyRealtimeMonitor, RealtimeFeedEvent


def _settings(**overrides) -> Settings:
    settings = Settings(
        project_dir=Path("."),
        db_path=Path("bus.db"),
        download_db_path=Path("downloads/bus.db"),
        tdx_client_id="test",
        tdx_client_secret="test",
        tdx_base_url="https://example.invalid",
        tdx_token_url="https://example.invalid/token",
        tdx_cities=("Kaohsiung", "Taichung"),
        tdx_request_timeout=30,
        tdx_token_refresh_skew=300,
        tdx_retry_attempts=1,
        tdx_retry_backoff=1.0,
        tdx_min_request_interval=0.0,
        realtime_cache_ttl=5,
        realtime_track_ttl=30,
        cors_origins=(),
        auth_public_base_url="https://bus.example.invalid",
        auth_state_ttl_seconds=600,
        auth_snowflake_node_id=0,
        discord_oauth_client_id=None,
        discord_oauth_client_secret=None,
        google_oauth_client_id=None,
        google_oauth_client_secret=None,
        google_native_oauth_client_ids=(),
        realtime_alert_discord_webhook_url="https://discord.example.invalid/alerts",
        realtime_empty_alert_after_seconds=900,
    )
    return replace(settings, **overrides)


class EmptyRealtimeMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_000_000.0
        self.events: list[RealtimeFeedEvent] = []

    def _monitor(self, settings: Settings | None = None) -> EmptyRealtimeMonitor:
        return EmptyRealtimeMonitor(
            settings or _settings(),
            notify=self.events.append,
            clock=lambda: self.now,
        )

    def _at(self, offset: float) -> None:
        self.now = 1_000_000.0 + offset

    def test_alerts_once_when_a_city_stays_empty(self) -> None:
        monitor = self._monitor()
        monitor.record("Kaohsiung", ["KHH11", "KHH100"], 0)
        self._at(600)
        monitor.record("Kaohsiung", ["KHH123"], 0)
        self.assertEqual(self.events, [])

        self._at(900)
        monitor.record("Kaohsiung", ["KHH11"], 0)
        self._at(1800)
        monitor.record("Kaohsiung", ["KHH11"], 0)

        self.assertEqual(
            self.events,
            [RealtimeFeedEvent(city="Kaohsiung", empty_since=1_000_000, route_count=3)],
        )

    def test_one_route_without_data_is_not_a_dead_city(self) -> None:
        monitor = self._monitor()
        for offset in range(0, 7200, 60):
            self._at(offset)
            monitor.record("Taichung", ["TXG999"], 0)

        self.assertEqual(self.events, [])

    def test_data_restarts_the_streak_without_a_recovery_notice(self) -> None:
        monitor = self._monitor()
        monitor.record("Kaohsiung", ["KHH11", "KHH100", "KHH123"], 0)
        self._at(600)
        monitor.record("Kaohsiung", ["KHH11"], 42)
        self._at(700)
        monitor.record("Kaohsiung", ["KHH11", "KHH100", "KHH123"], 0)
        self._at(1500)
        monitor.record("Kaohsiung", ["KHH11"], 0)
        self.assertEqual(self.events, [])

        self._at(1600)
        monitor.record("Kaohsiung", ["KHH11"], 0)
        self.assertEqual(
            self.events,
            [RealtimeFeedEvent(city="Kaohsiung", empty_since=1_000_700, route_count=3)],
        )

    def test_reports_recovery_after_an_alert(self) -> None:
        monitor = self._monitor()
        monitor.record("Kaohsiung", ["KHH11", "KHH100", "KHH123"], 0)
        self._at(900)
        monitor.record("Kaohsiung", ["KHH11"], 0)
        self._at(5000)
        monitor.record("Kaohsiung", ["KHH11"], 91)
        self._at(6000)
        monitor.record("Kaohsiung", ["KHH11"], 91)

        self.assertEqual(
            self.events,
            [
                RealtimeFeedEvent(city="Kaohsiung", empty_since=1_000_000, route_count=3),
                RealtimeFeedEvent(
                    city="Kaohsiung",
                    empty_since=1_000_000,
                    route_count=3,
                    recovered_at=1_005_000,
                ),
            ],
        )

    def test_cities_are_tracked_separately(self) -> None:
        monitor = self._monitor()
        monitor.record("Kaohsiung", ["KHH11", "KHH100", "KHH123"], 0)
        self._at(900)
        monitor.record("Taichung", ["TXG1"], 0)
        monitor.record("Taichung", ["TXG1"], 30)

        self.assertEqual(self.events, [])
        monitor.record("Kaohsiung", ["KHH11"], 0)
        self.assertEqual([event.city for event in self.events], ["Kaohsiung"])

    def test_disabled_without_a_webhook_or_threshold(self) -> None:
        for settings in (
            _settings(realtime_alert_discord_webhook_url=None),
            _settings(realtime_empty_alert_after_seconds=0),
        ):
            monitor = self._monitor(settings)
            self._at(0)
            monitor.record("Kaohsiung", ["KHH11", "KHH100", "KHH123"], 0)
            self._at(10_000)
            monitor.record("Kaohsiung", ["KHH11"], 0)

        self.assertEqual(self.events, [])

    def test_feedback_webhook_enables_the_alert(self) -> None:
        monitor = self._monitor(
            _settings(
                realtime_alert_discord_webhook_url=None,
                feedback_discord_webhook_url="https://discord.example.invalid/feedback",
            )
        )
        monitor.record("Kaohsiung", ["KHH11", "KHH100", "KHH123"], 0)
        self._at(900)
        monitor.record("Kaohsiung", ["KHH11"], 0)

        self.assertEqual(len(self.events), 1)


class NotifyRealtimeFeedTests(unittest.TestCase):
    def _post(self, settings: Settings, **kwargs) -> Mock:
        with patch("app.discord_webhook.requests.post") as post:
            post.return_value.ok = True
            self.assertTrue(notify_realtime_feed(settings, **kwargs))
        return post

    def test_empty_alert_goes_to_the_alert_webhook(self) -> None:
        post = self._post(
            _settings(feedback_discord_webhook_url="https://discord.example.invalid/feedback"),
            city="Kaohsiung",
            empty_since=1_793_000_000,
            route_count=12,
        )

        self.assertEqual(post.call_args.args[0], "https://discord.example.invalid/alerts")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("Kaohsiung", payload["embeds"][0]["title"])
        self.assertIn("<t:1793000000:R>", payload["embeds"][0]["description"])

    def test_recovery_falls_back_to_the_feedback_webhook(self) -> None:
        post = self._post(
            _settings(
                realtime_alert_discord_webhook_url=None,
                feedback_discord_webhook_url="https://discord.example.invalid/feedback",
            ),
            city="Kaohsiung",
            empty_since=1_793_000_000,
            route_count=12,
            recovered_at=1_793_000_000 + 2 * 3600 + 5 * 60,
        )

        self.assertEqual(post.call_args.args[0], "https://discord.example.invalid/feedback")
        embed = post.call_args.kwargs["json"]["embeds"][0]
        self.assertIn("recovered", embed["title"])
        self.assertIn("2h 5m", embed["description"])

    def test_no_webhook_sends_nothing(self) -> None:
        with patch("app.discord_webhook.requests.post") as post:
            self.assertFalse(
                notify_realtime_feed(
                    _settings(realtime_alert_discord_webhook_url=None),
                    city="Kaohsiung",
                    empty_since=1_793_000_000,
                    route_count=12,
                )
            )
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
