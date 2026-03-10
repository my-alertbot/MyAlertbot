from __future__ import annotations

import unittest

from alertbot.bots.calendar_commands import parse_addevent_request


class ParseAddEventRequestTests(unittest.TestCase):
    def test_parse_one_time_event_defaults(self) -> None:
        parsed = parse_addevent_request("2026-03-10 14:30 Dentist Appointment")
        self.assertEqual(parsed["name"], "Dentist Appointment")
        self.assertEqual(parsed["recurrence"], "once")
        self.assertEqual(parsed["reminder_minutes"], 0)
        self.assertIsNone(parsed["tz_name"])
        self.assertEqual(parsed["when_local"].strftime("%Y-%m-%d %H:%M"), "2026-03-10 14:30")

    def test_parse_recurring_event_with_options(self) -> None:
        parsed = parse_addevent_request(
            "2026-03-10 09:00 Standup | recurrence=weekly | reminder=15 | tz=UTC | message=Join call"
        )
        self.assertEqual(parsed["recurrence"], "weekly")
        self.assertEqual(parsed["reminder_minutes"], 15)
        self.assertEqual(parsed["tz_name"], "UTC")
        self.assertEqual(parsed["message"], "Join call")

    def test_parse_weekday_override(self) -> None:
        parsed = parse_addevent_request(
            "2026-03-10 09:00 Team Sync | recurrence=weekly | weekday=thursday"
        )
        self.assertEqual(parsed["weekday"], 3)

    def test_reject_invalid_recurrence(self) -> None:
        with self.assertRaises(ValueError):
            parse_addevent_request("2026-03-10 09:00 Thing | recurrence=hourly")


if __name__ == "__main__":
    unittest.main()
