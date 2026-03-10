import unittest

from alertbot.bots import weatherbot


class WeatherBotFormattingTests(unittest.TestCase):
    def test_format_forecast_message_includes_current_conditions(self) -> None:
        forecast = {
            "current": {
                "temperature_2m": 12.8,
                "precipitation_probability": 35,
            },
            "current_units": {
                "temperature_2m": "°C",
                "precipitation_probability": "%",
            },
            "daily": {
                "time": ["2026-02-24", "2026-02-25"],
                "temperature_2m_min": [8.1, 7.5],
                "temperature_2m_max": [14.9, 16.2],
                "precipitation_probability_max": [60, 20],
                "precipitation_sum": [3.4, 0.2],
            },
            "daily_units": {
                "temperature_2m_max": "°C",
                "precipitation_sum": "mm",
            },
        }

        message = weatherbot.format_forecast_message("Test City", forecast)

        self.assertIn("Current | Temp: 13°C | Precip: 35%", message)
        self.assertIn("Today (2026-02-24) | Temp: 8–15°C | Precip: 60% | Total: 3.4mm", message)
        self.assertIn("Tomorrow (2026-02-25) | Temp: 8–16°C | Precip: 20% | Total: 0.2mm", message)

    def test_format_forecast_message_omits_current_line_when_missing(self) -> None:
        forecast = {
            "daily": {
                "time": ["2026-02-24", "2026-02-25"],
                "temperature_2m_min": [8, 7],
                "temperature_2m_max": [14, 16],
                "precipitation_probability_max": [60, 20],
                "precipitation_sum": [3.4, 0.2],
            },
            "daily_units": {
                "temperature_2m_max": "°C",
                "precipitation_sum": "mm",
            },
        }

        message = weatherbot.format_forecast_message("Test City", forecast)

        self.assertNotIn("\nCurrent", message)
        self.assertIn("Today (2026-02-24)", message)


if __name__ == "__main__":
    unittest.main()
