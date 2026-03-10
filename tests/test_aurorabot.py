from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from alertbot.bots import aurorabot


class ParseKpValueTests(unittest.TestCase):
    def test_parse_kp_value_extracts_number_from_messy_string(self) -> None:
        self.assertEqual(aurorabot._parse_kp_value("Kp est: 7.33 (storm)"), 7.33)


class FetchKpPointsTests(unittest.TestCase):
    @patch.object(aurorabot, "request_json")
    def test_fetch_kp_points_parses_and_sorts_valid_points(self, request_json_mock) -> None:
        request_json_mock.return_value = [
            {"time_tag": "2026-02-25T11:00:00Z", "estimated_kp": "5.1"},
            {"time_tag": "invalid", "estimated_kp": "8.0"},
            {"time_tag": "2026-02-25T10:00:00Z", "kp_index": "6.2"},
            {"time_tag": "2026-02-25T09:00:00Z", "kp": "kp=4.4"},
        ]

        points = aurorabot.fetch_kp_points()

        self.assertEqual(len(points), 3)
        self.assertEqual([p[1] for p in points], [4.4, 6.2, 5.1])
        self.assertEqual(points[0][0].hour, 9)
        self.assertEqual(points[-1][0].hour, 11)


class ScheduledAlertDecisionTests(unittest.TestCase):
    def test_should_not_alert_when_below_threshold(self) -> None:
        should_alert, reason = aurorabot._should_send_scheduled_alert(
            peak_kp=5.0,
            threshold=7.0,
            cooldown_minutes=360,
            state={},
        )
        self.assertFalse(should_alert)
        self.assertIn("No strong aurora activity", reason)

    def test_should_not_realert_during_cooldown_without_significant_increase(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        should_alert, reason = aurorabot._should_send_scheduled_alert(
            peak_kp=7.4,
            threshold=7.0,
            cooldown_minutes=360,
            state={"last_alert_time": recent, "last_alert_kp": 7.0},
        )
        self.assertFalse(should_alert)
        self.assertIn("cooldown", reason.lower())

    def test_should_realert_during_cooldown_when_kp_jumps_by_delta(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        should_alert, reason = aurorabot._should_send_scheduled_alert(
            peak_kp=8.2,
            threshold=7.0,
            cooldown_minutes=360,
            state={"last_alert_time": recent, "last_alert_kp": 7.0},
        )
        self.assertTrue(should_alert)
        self.assertIn("increased significantly", reason)


if __name__ == "__main__":
    unittest.main()
