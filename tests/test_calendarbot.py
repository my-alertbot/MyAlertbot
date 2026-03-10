from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from alertbot.bots.calendarbot import format_message


class CalendarBotFormatMessageTests(unittest.TestCase):
    def test_formats_event_time_in_display_timezone(self) -> None:
        display_tz = ZoneInfo("America/New_York")
        now = datetime(2026, 2, 25, 9, 30, tzinfo=display_tz)
        event_time = datetime(2026, 2, 25, 15, 0, tzinfo=ZoneInfo("UTC"))

        message = format_message(
            {"name": "UTC Event"},
            event_time,
            now,
            display_tz=display_tz,
        )

        self.assertIn("⏰ In 30m", message)
        self.assertIn("🕐 2026-02-25 10:00 EST", message)


if __name__ == "__main__":
    unittest.main()
