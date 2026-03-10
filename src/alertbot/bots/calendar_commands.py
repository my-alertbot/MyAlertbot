"""Parsing helpers for calendar-related Telegram commands."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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

SUPPORTED_RECURRENCES = {"once", "daily", "weekly", "monthly", "yearly"}


def addevent_usage() -> str:
    return (
        "Usage:\n"
        "/addevent YYYY-MM-DD HH:MM Event name\n\n"
        "Optional segments (separate with '|'):\n"
        "  recurrence=once|daily|weekly|monthly|yearly\n"
        "  reminder=<minutes>\n"
        "  message=<text>\n"
        "  tz=<IANA timezone>   (example: UTC or America/New_York)\n"
        "  weekday=<mon..sun|0..6>   (optional for weekly override)\n"
        "  day=<1..31> / month=<1..12> (optional recurrence overrides)\n\n"
        "Examples:\n"
        "/addevent 2026-03-10 14:30 Dentist\n"
        "/addevent 2026-03-10 14:30 Standup | recurrence=weekly\n"
        "/addevent 2026-03-10 14:30 Dentist | reminder=60 | message=Leave early"
    )


def _parse_weekday_value(raw: str) -> int:
    value = raw.strip().lower()
    if value.isdigit():
        parsed = int(value)
        if 0 <= parsed <= 6:
            return parsed
    if value in WEEKDAY_ALIASES:
        return WEEKDAY_ALIASES[value]
    raise ValueError("weekday must be 0..6 or a weekday name like 'wednesday'")


def parse_addevent_request(raw_text: str) -> dict[str, Any]:
    body = raw_text.strip()
    if not body:
        raise ValueError(addevent_usage())

    segments = [segment.strip() for segment in body.split("|")]
    segments = [segment for segment in segments if segment]
    if not segments:
        raise ValueError(addevent_usage())

    head_tokens = segments[0].split()
    if len(head_tokens) < 3:
        raise ValueError(addevent_usage())

    date_str, time_str = head_tokens[0], head_tokens[1]
    name = " ".join(head_tokens[2:]).strip()
    if not name:
        raise ValueError("Event name is required")

    try:
        when_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Invalid date/time. Expected YYYY-MM-DD HH:MM") from None

    recurrence = "once"
    reminder_minutes = 0
    message = ""
    tz_name: str | None = None
    weekday: int | None = None
    day: int | None = None
    month: int | None = None

    for segment in segments[1:]:
        if "=" in segment:
            key, value = segment.split("=", 1)
            key = key.strip().lower()
            value = value.strip()

            if key in {"reminder", "reminder_minutes"}:
                try:
                    reminder_minutes = int(value)
                except ValueError:
                    raise ValueError("reminder must be an integer number of minutes") from None
                if reminder_minutes < 0:
                    raise ValueError("reminder must be >= 0")
            elif key in {"recurrence", "repeat"}:
                recurrence = value.strip().lower()
                if recurrence not in SUPPORTED_RECURRENCES:
                    raise ValueError(
                        "recurrence must be one of: once, daily, weekly, monthly, yearly"
                    )
            elif key == "message":
                message = value
            elif key == "tz":
                tz_name = value or None
            elif key == "weekday":
                weekday = _parse_weekday_value(value)
            elif key == "day":
                try:
                    day = int(value)
                except ValueError:
                    raise ValueError("day must be an integer 1..31") from None
                if not (1 <= day <= 31):
                    raise ValueError("day must be in 1..31")
            elif key == "month":
                try:
                    month = int(value)
                except ValueError:
                    raise ValueError("month must be an integer 1..12") from None
                if not (1 <= month <= 12):
                    raise ValueError("month must be in 1..12")
            else:
                raise ValueError(
                    f"Unknown option '{key}'. Supported: recurrence, reminder, message, tz, weekday, day, month"
                )
        elif not message:
            message = segment
        else:
            raise ValueError("Only one free-form message segment is supported")

    return {
        "name": name,
        "when_local": when_local,
        "recurrence": recurrence,
        "reminder_minutes": reminder_minutes,
        "message": message,
        "tz_name": tz_name,
        "weekday": weekday,
        "day": day,
        "month": month,
    }
