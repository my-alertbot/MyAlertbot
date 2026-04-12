#!/usr/bin/env python3
"""Calendar reminder bot - sends Telegram alerts for configured events."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alertbot.common import (
    STATE_DIR,
    calculate_lookback_minutes,
    format_run_info,
    getenv_required,
    iso_now,
    load_env_file,
    load_location,
    load_json,
    save_json,
    send_telegram_alert,
    setup_logging,
)
from alertbot.bots.calendar_ics import DEFAULT_ICS_PATH, load_calendar_events_from_ics

DEFAULT_STATE_PATH = STATE_DIR / "calendarbot.state.json"

WEEKDAY_ALIASES = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
def parse_weekday(weekday: Any, event_name: str | None) -> int | None:
    """Parse weekday as int (0-6), numeric string, or weekday name."""
    if isinstance(weekday, int):
        if 0 <= weekday <= 6:
            return weekday
        logging.warning("Weekly event %s has invalid weekday: %s", event_name, weekday)
        return None

    if isinstance(weekday, str):
        normalized = weekday.strip().lower()
        if normalized.isdigit():
            parsed = int(normalized)
            if 0 <= parsed <= 6:
                return parsed
        if normalized in WEEKDAY_ALIASES:
            return WEEKDAY_ALIASES[normalized]

    logging.warning("Weekly event %s has invalid weekday: %s", event_name, weekday)
    return None


def parse_event_time(event: dict, default_tz: ZoneInfo, now: datetime) -> datetime | None:
    """Parse event time based on recurrence type.

    Returns the next occurrence of the event, or None if not applicable today.
    """
    recurrence = event.get("recurrence", "once")
    time_str = event.get("time", "09:00")
    event_name = event.get("name")

    tz_name = event.get("timezone")
    event_tz = default_tz
    if tz_name:
        try:
            event_tz = ZoneInfo(tz_name)
        except Exception:
            logging.warning("Invalid timezone for event %s: %s", event_name, tz_name)
            event_tz = default_tz

    try:
        hour, minute = map(int, time_str.split(":"))
    except (ValueError, AttributeError):
        logging.warning("Invalid time format for event %s: %s", event_name, time_str)
        return None

    event_now = now.astimezone(event_tz)

    if recurrence == "once":
        # One-time event: requires date in YYYY-MM-DD format
        date_str = event.get("date")
        if not date_str:
            logging.warning("One-time event %s missing 'date' field", event_name)
            return None
        try:
            year, month, day = map(int, date_str.split("-"))
            return datetime(year, month, day, hour, minute, tzinfo=event_tz)
        except (ValueError, AttributeError):
            logging.warning("Invalid date format for event %s: %s", event_name, date_str)
            return None

    elif recurrence == "yearly":
        # Yearly event: requires month and day
        month = event.get("month")
        day = event.get("day")
        if not month or not day:
            logging.warning("Yearly event %s missing 'month' or 'day'", event_name)
            return None
        # Check this year and next year
        for year in (event_now.year, event_now.year + 1):
            try:
                candidate = datetime(year, month, day, hour, minute, tzinfo=event_tz)
            except ValueError:
                continue
            if candidate >= event_now - timedelta(minutes=5):
                return candidate
        return None

    elif recurrence == "monthly":
        # Monthly event: requires day of month
        day = event.get("day")
        if not day:
            logging.warning("Monthly event %s missing 'day'", event_name)
            return None
        # Check this month and next month
        try:
            this_month = datetime(event_now.year, event_now.month, day, hour, minute, tzinfo=event_tz)
        except ValueError:
            # Day doesn't exist this month (e.g., Feb 30)
            this_month = None

        next_month = event_now.month + 1
        next_year = event_now.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        try:
            next_month_dt = datetime(next_year, next_month, day, hour, minute, tzinfo=event_tz)
        except ValueError:
            next_month_dt = None

        if this_month and this_month >= event_now - timedelta(minutes=5):
            return this_month
        return next_month_dt

    elif recurrence == "weekly":
        # Weekly event: requires weekday (0=Monday, 6=Sunday)
        weekday = parse_weekday(event.get("weekday"), event_name)
        if weekday is None:
            return None
        # Find next occurrence of this weekday
        days_ahead = weekday - event_now.weekday()
        if days_ahead < 0:
            days_ahead += 7
        next_date = event_now.date() + timedelta(days=days_ahead)
        event_time = datetime(next_date.year, next_date.month, next_date.day,
                              hour, minute, tzinfo=event_tz)
        # If it's today but already passed, get next week
        if event_time < event_now - timedelta(minutes=5):
            next_date = event_now.date() + timedelta(days=days_ahead + 7)
            event_time = datetime(next_date.year, next_date.month, next_date.day,
                                  hour, minute, tzinfo=event_tz)
        return event_time

    elif recurrence == "daily":
        # Daily event: just needs time
        today = datetime(event_now.year, event_now.month, event_now.day, hour, minute,
                         tzinfo=event_tz)
        tomorrow = today + timedelta(days=1)
        if today >= event_now - timedelta(minutes=5):
            return today
        return tomorrow

    else:
        logging.warning("Unknown recurrence type for event %s: %s",
                        event_name, recurrence)
        return None


def should_alert(event: dict, event_time: datetime, now: datetime,
                 state: dict, lookback_minutes: int = 5) -> bool:
    """Determine if we should send an alert for this event."""
    event_id = event.get("id") or event.get("name")
    if not event_id:
        return False

    # Check if already alerted for this occurrence
    sent_alerts = state.get("sent_alerts", {})
    event_key = f"{event_id}_{event_time.isoformat()}"
    if event_key in sent_alerts:
        return False

    # Get reminder offset (minutes before event to alert)
    reminder_minutes = event.get("reminder_minutes", 0)
    alert_time = event_time - timedelta(minutes=reminder_minutes)

    # Check if we're within the alert window
    # Alert if: alert_time <= now < alert_time + lookback_minutes
    window_start = alert_time - timedelta(minutes=lookback_minutes)
    window_end = alert_time + timedelta(minutes=lookback_minutes)

    return window_start <= now <= window_end


def _format_display_time(event_time: datetime, display_tz: ZoneInfo) -> str:
    """Format event time in the user's display timezone for Telegram messages."""
    return event_time.astimezone(display_tz).strftime("%Y-%m-%d %H:%M %Z")


def format_message(
    event: dict,
    event_time: datetime,
    now: datetime,
    display_tz: ZoneInfo | None = None,
) -> str:
    """Format the alert message."""
    name = event.get("name", "Unnamed Event")
    message = event.get("message", "")
    display_tz = display_tz or now.tzinfo or ZoneInfo("UTC")

    time_diff = event_time - now
    minutes_until = int(time_diff.total_seconds() / 60)

    lines = [f"📅 {name}"]

    if minutes_until > 0:
        if minutes_until >= 60:
            hours = minutes_until // 60
            mins = minutes_until % 60
            if mins > 0:
                lines.append(f"⏰ In {hours}h {mins}m")
            else:
                lines.append(f"⏰ In {hours}h")
        else:
            lines.append(f"⏰ In {minutes_until}m")
    elif minutes_until == 0:
        lines.append("⏰ Now!")
    else:
        lines.append("⏰ Starting now")

    lines.append(f"🕐 {_format_display_time(event_time, display_tz)}")

    if message:
        lines.append(f"📝 {message}")

    return "\n".join(lines)


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Run calendar check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs
        args: Command arguments (e.g., ["7"] for /calendar 7)

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running calendarbot: %s", format_run_info(schedule_context))

    # Parse days argument for manual triggers (default to 7 days)
    days = 7
    if manual_trigger and args:
        for arg in args:
            try:
                days = int(arg)
                if days < 1:
                    days = 1
                elif days > 365:
                    days = 365
                break  # Use first valid integer
            except ValueError:
                continue

    try:
        token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    state_path = Path(os.getenv("CALENDARBOT_STATE", DEFAULT_STATE_PATH))
    ics_path = DEFAULT_ICS_PATH

    # Load state
    state = load_json(state_path, {"sent_alerts": {}, "last_check": None})

    # Use shared location timezone (same source used by weather/rain bots).
    tz_name = load_location().timezone
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logging.warning("Invalid location timezone %s, using UTC", tz_name)
        tz = ZoneInfo("UTC")
        tz_name = "UTC"

    events = load_calendar_events_from_ics(ics_path)

    now = datetime.now(tz)
    logging.info("Checking events at %s (%s)", now.strftime("%Y-%m-%d %H:%M"), tz_name)

    # Derive lookback from schedule context - use interval as the check window
    # For calendar, we use the interval directly (not multiplied) since events
    # have their own reminder_minutes offset
    lookback_minutes = calculate_lookback_minutes(
        schedule_context,
        default_minutes=5,
        buffer_multiplier=1.0,  # Use interval directly for calendar
        max_minutes=60,  # Cap at 60 minutes for calendar alerts
    )

    alerts_sent = 0
    upcoming_events = []

    for event in events:
        if not event.get("enabled", True):
            continue

        event_time = parse_event_time(event, tz, now)
        if not event_time:
            continue

        # For manual triggers, collect upcoming events to show
        if manual_trigger:
            time_diff = event_time - now
            hours_until = time_diff.total_seconds() / 3600
            if 0 <= hours_until < (days * 24):  # Show events within specified days
                upcoming_events.append((event, event_time, now))

        if should_alert(event, event_time, now, state, lookback_minutes):
            message = format_message(event, event_time, now, display_tz=tz)
            event_id = event.get("id") or event.get("name")
            event_key = f"{event_id}_{event_time.isoformat()}"

            try:
                send_telegram_alert(token, tg_chat_id, message)
                logging.info("Sent alert for event: %s", event.get("name"))
                state["sent_alerts"][event_key] = iso_now()
                alerts_sent += 1
            except RuntimeError as exc:
                logging.error("Failed to send alert for %s: %s", event.get("name"), exc)

    # For manual trigger, send a summary of upcoming events
    message = None
    if manual_trigger:
        if upcoming_events:
            lines = [f"📅 Upcoming Events (next {days} days):"]
            for event, event_time, _ in sorted(upcoming_events, key=lambda x: x[1]):
                time_str = _format_display_time(event_time, tz)
                lines.append(f"  • {event.get('name', 'Unnamed')} - {time_str}")
            message = "\n".join(lines)
        else:
            message = f"📅 No upcoming events in the next {days} days"

    # Clean up old alerts (older than 30 days)
    cutoff = (now - timedelta(days=30)).isoformat()
    state["sent_alerts"] = {
        k: v for k, v in state["sent_alerts"].items()
        if v > cutoff
    }

    state["last_check"] = iso_now()
    save_json(state_path, state)

    logging.info("Check complete. Alerts sent: %d", alerts_sent)
    return {
        "success": True,
        "alerts_sent": alerts_sent,
        "message": message,
    }


def main() -> int:
    """Main entry point."""
    setup_logging()
    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
