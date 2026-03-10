from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from alertbot.bots import airqualitybot


class HandleTelegramQueriesAuthTests(unittest.TestCase):
    def _config(self) -> airqualitybot.Config:
        return airqualitybot.Config(
            telegram_bot_token="token",
            telegram_chat_id="-100123",
            aqi_api_token="aqi-token",
            aqi_city="london",
            latitude=51.5074,
            longitude=-0.1278,
            aqi_stations=[],
            aqi_threshold=80,
            aqi_provider="waqi",
            aqi_max_age_hours=3,
            state_file="state/airqualitybot_state.json",
            location_display_name="London, United Kingdom",
        )

    @patch.object(airqualitybot, "send_telegram_alert")
    @patch.object(airqualitybot, "fetch_aqi")
    @patch.object(airqualitybot, "fetch_telegram_updates")
    def test_ignores_unauthorized_chat_commands(
        self,
        fetch_updates_mock,
        fetch_aqi_mock,
        send_alert_mock,
    ) -> None:
        startup = datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
        fetch_updates_mock.return_value = [
            {
                "update_id": 123,
                "message": {
                    "date": int(startup.timestamp()) + 5,
                    "chat": {"id": 999999},
                    "text": "/airquality",
                },
            }
        ]
        state = airqualitybot.State()

        result = airqualitybot.handle_telegram_queries(
            config=self._config(),
            state=state,
            now=startup,
            startup_time=startup,
            last_aqi=55,
            station_count=1,
            station_total=1,
            poll_timeout_seconds=0,
        )

        self.assertEqual(result, (55, 1, 1))
        self.assertEqual(state.last_update_id, 123)
        fetch_aqi_mock.assert_not_called()
        send_alert_mock.assert_not_called()

    @patch.object(airqualitybot, "send_telegram_alert")
    @patch.object(airqualitybot, "fetch_aqi")
    @patch.object(airqualitybot, "fetch_telegram_updates")
    def test_replies_to_authorized_chat_commands(
        self,
        fetch_updates_mock,
        fetch_aqi_mock,
        send_alert_mock,
    ) -> None:
        startup = datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
        fetch_updates_mock.return_value = [
            {
                "update_id": 124,
                "message": {
                    "date": int(startup.timestamp()) + 5,
                    "chat": {"id": -100123},
                    "text": "/airquality",
                },
            }
        ]
        fetch_aqi_mock.return_value = (91, 2, 3)
        state = airqualitybot.State()

        result = airqualitybot.handle_telegram_queries(
            config=self._config(),
            state=state,
            now=startup,
            startup_time=startup,
            last_aqi=None,
            station_count=None,
            station_total=None,
            poll_timeout_seconds=0,
        )

        self.assertEqual(result, (91, 2, 3))
        self.assertEqual(state.last_update_id, 124)
        fetch_aqi_mock.assert_called_once()
        send_alert_mock.assert_called_once()
        self.assertEqual(send_alert_mock.call_args.args[1], "-100123")


if __name__ == "__main__":
    unittest.main()
