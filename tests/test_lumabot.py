from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import patch

from alertbot.bots import lumabot


def _html_with_next_data(data: dict) -> str:
    return '<script id="__NEXT_DATA__" type="application/json">%s</script>' % json.dumps(data)


def _api_only_page_html() -> str:
    """Mirror luma.com/garaza: calendar present, event_start_ats populated, no embedded events."""
    return _html_with_next_data({
        "props": {"pageProps": {"initialData": {"kind": "calendar", "data": {
            "calendar": {"api_id": "cal-x", "name": "Garaza"},
            "featured_items": [],
            "event_start_ats": ["2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z"],
            "has_upcoming_events": False,
        }}}}
    })


def _truly_empty_page_html() -> str:
    """Page with a calendar but zero event_start_ats (genuinely no events)."""
    return _html_with_next_data({
        "props": {"pageProps": {"initialData": {"kind": "calendar", "data": {
            "calendar": {"api_id": "cal-y"},
            "featured_items": [],
            "event_start_ats": [],
        }}}}
    })


class PageLoadsEventsViaApiTests(unittest.TestCase):
    def test_detects_client_side_calendar(self) -> None:
        self.assertTrue(lumabot._page_loads_events_via_api(_api_only_page_html()))

    def test_empty_calendar_is_not_flagged(self) -> None:
        self.assertFalse(lumabot._page_loads_events_via_api(_truly_empty_page_html()))


class FetchPageEventsGracefulTests(unittest.TestCase):
    """Regression: 'no events found' must not spam a WARNING every scheduled run.

    A page that renders events client-side (Luma moved them out of the embedded
    payload) yields zero extracted events on every run. That must surface as a
    single WARNING (so the structural change stays visible) followed by DEBUG,
    not a recurring WARNING that reads like an error.
    """

    def setUp(self) -> None:
        lumabot._WARNED_API_ONLY_PAGES.clear()

    def test_warns_once_then_debug_for_api_only_page(self) -> None:
        with patch.object(lumabot, "request_text", return_value=_api_only_page_html()):
            with self.assertLogs(level=logging.DEBUG) as cm:
                for _ in range(3):
                    self.assertEqual(lumabot.fetch_page_events("https://luma.com/garaza"), [])

        warnings = [r for r in cm.records if r.levelname == "WARNING"]
        debugs = [r for r in cm.records if r.levelname == "DEBUG" and "No events" in r.getMessage()]
        self.assertEqual(len(warnings), 1, f"expected one WARNING, got {[r.getMessage() for r in warnings]}")
        self.assertGreaterEqual(len(debugs), 2, "subsequent runs should log at DEBUG")
        self.assertIn("client-side", warnings[0].getMessage())

    def test_truly_empty_page_logs_debug_only(self) -> None:
        with patch.object(lumabot, "request_text", return_value=_truly_empty_page_html()):
            with self.assertLogs(level=logging.DEBUG) as cm:
                lumabot.fetch_page_events("https://luma.com/empty")

        warnings = [r for r in cm.records if r.levelname == "WARNING"]
        self.assertEqual(warnings, [], "a calendar with no events must not warn")


class ProcessPageUpdatesLastCheckedTests(unittest.TestCase):
    """Regression: last_checked_at must advance even when a page yields no events."""

    def test_records_check_when_no_events(self) -> None:
        page_state: dict = {}
        with patch.object(lumabot, "request_text", return_value=_truly_empty_page_html()):
            sent, failed = lumabot.process_page(
                "https://luma.com/empty", page_state, "tg", "chat", 50
            )
        self.assertEqual((sent, failed), (0, 0))
        self.assertIn("last_checked_at", page_state)
        self.assertTrue(page_state["last_checked_at"])


if __name__ == "__main__":
    unittest.main()
