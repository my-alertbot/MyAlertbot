from __future__ import annotations

import unittest

from alertbot.bots import sunrisebot


class SunriseBotTests(unittest.TestCase):
    def test_format_sun_times_message(self) -> None:
        payload = {
            "daily": {
                "time": ["2026-02-25"],
                "sunrise": ["2026-02-25T07:11"],
                "sunset": ["2026-02-25T17:29"],
            }
        }

        message = sunrisebot.format_sun_times_message(
            "London, United Kingdom",
            "Europe/London",
            payload,
        )

        self.assertIn("🌅 Sun Times for London, United Kingdom", message)
        self.assertIn("Date: 2026-02-25", message)
        self.assertIn("Sunrise: 07:11", message)
        self.assertIn("Sunset: 17:29", message)
        self.assertIn("Timezone: Europe/London", message)

    def test_scheduled_run_is_skipped(self) -> None:
        result = sunrisebot.run(manual_trigger=False)
        self.assertTrue(result["success"])
        self.assertEqual(result["alerts_sent"], 0)
        self.assertIn("manual-only", result["message"])


if __name__ == "__main__":
    unittest.main()
