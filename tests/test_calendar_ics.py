from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from alertbot.bots.calendar_ics import (
    append_calendar_event_to_ics,
    delete_event_from_ics,
    load_calendar_events_from_ics,
)


class CalendarICSTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ics_path = Path(self.tmpdir.name) / "calendarbot.ics"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_append_and_load_one_time_event(self) -> None:
        result = append_calendar_event_to_ics(
            name="Dentist",
            when_local=datetime(2026, 3, 10, 14, 30),
            reminder_minutes=45,
            message="Leave early",
            path=self.ics_path,
        )
        events = load_calendar_events_from_ics(self.ics_path)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["name"], "Dentist")
        self.assertEqual(event["recurrence"], "once")
        self.assertEqual(event["date"], "2026-03-10")
        self.assertEqual(event["time"], "14:30")
        self.assertEqual(event["reminder_minutes"], 45)
        self.assertEqual(event["message"], "Leave early")
        self.assertEqual(event["id"], result["uid"])

    def test_load_missing_ics_creates_empty_calendar_file(self) -> None:
        self.assertFalse(self.ics_path.exists())
        events = load_calendar_events_from_ics(self.ics_path)
        self.assertEqual(events, [])
        self.assertTrue(self.ics_path.exists())
        text = self.ics_path.read_text(encoding="utf-8")
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertIn("END:VCALENDAR", text)

    def test_append_and_load_recurring_weekly_with_tz_and_valarm(self) -> None:
        append_calendar_event_to_ics(
            name="Standup",
            when_local=datetime(2026, 3, 10, 9, 0),
            recurrence="weekly",
            reminder_minutes=15,
            tz_name="UTC",
            path=self.ics_path,
        )
        events = load_calendar_events_from_ics(self.ics_path)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["recurrence"], "weekly")
        self.assertEqual(event["weekday"], 1)  # 2026-03-10 is a Tuesday
        self.assertEqual(event["timezone"], "UTC")
        self.assertEqual(event["reminder_minutes"], 15)

    def test_delete_by_name(self) -> None:
        append_calendar_event_to_ics(
            name="Delete Me",
            when_local=datetime(2026, 3, 10, 9, 0),
            path=self.ics_path,
        )
        append_calendar_event_to_ics(
            name="Keep Me",
            when_local=datetime(2026, 3, 11, 9, 0),
            path=self.ics_path,
        )
        deleted = delete_event_from_ics("Delete Me", path=self.ics_path)
        self.assertEqual(deleted["name"], "Delete Me")
        remaining = load_calendar_events_from_ics(self.ics_path)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["name"], "Keep Me")


if __name__ == "__main__":
    unittest.main()
