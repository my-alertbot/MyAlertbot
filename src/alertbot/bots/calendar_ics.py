"""Minimal iCalendar (.ics) read/write helpers for calendarbot.

Phase 1+ scope:
- Read VEVENT entries with DTSTART (+ optional RRULE) into calendarbot's event shape
- Write VEVENT entries (one-time + basic recurring) for Telegram calendar commands
- Store reminder minutes in a custom X- property and a simple VALARM for interoperability
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alertbot.common import CONFIG_DIR

DEFAULT_ICS_PATH = CONFIG_DIR / "calendarbot.ics"

_ICS_FILE_LOCK = threading.RLock()

_ICS_BYDAY_TO_WEEKDAY = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}

_WEEKDAY_TO_ICS_BYDAY = {v: k for k, v in _ICS_BYDAY_TO_WEEKDAY.items()}

_CAL_TEMPLATE_LINES = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//alertbot//calendarbot//EN",
    "CALSCALE:GREGORIAN",
    "END:VCALENDAR",
]


def _ics_unescape(value: str) -> str:
    return (
        value.replace("\\N", "\n")
        .replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _unfold_ics_lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return [line for line in unfolded if line != ""]


def _parse_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, raw_val = part.split("=", 1)
        params[key.upper()] = raw_val.strip('"')
    return name, params, value


def _parse_rrule(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, raw_val = part.split("=", 1)
        result[key.upper()] = raw_val
    return result


def _parse_dtstart(params: dict[str, str], value: str) -> tuple[datetime, str | None] | None:
    """Parse DTSTART into a datetime and optional timezone name.

    Returns a naive datetime for floating/local times and an aware UTC datetime for Zulu times.
    """
    try:
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt, "UTC"
        if "T" in value:
            fmt = "%Y%m%dT%H%M%S" if len(value) == 15 else "%Y%m%dT%H%M"
            return datetime.strptime(value, fmt), params.get("TZID")
        # DATE values are treated as midnight local/floating time.
        dt = datetime.strptime(value, "%Y%m%d")
        return dt, params.get("TZID")
    except Exception:
        return None


def _parse_int(raw: str | None, default: int | None = None) -> int | None:
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_valarm_trigger_minutes(value: str) -> int | None:
    """Parse VALARM TRIGGER duration subset like -PT15M or -PT1H30M."""
    raw = value.strip().upper()
    if not raw.startswith("-PT"):
        return None
    raw = raw[3:]
    if not raw:
        return None

    hours = 0
    minutes = 0
    num = ""
    for ch in raw:
        if ch.isdigit():
            num += ch
            continue
        if ch == "H":
            if not num:
                return None
            hours = int(num)
            num = ""
            continue
        if ch == "M":
            if not num:
                return None
            minutes = int(num)
            num = ""
            continue
        return None
    if num:
        return None
    total = hours * 60 + minutes
    return total if total >= 0 else None


def _format_valarm_trigger_minutes(minutes: int) -> str:
    mins = max(0, int(minutes))
    hours, rem = divmod(mins, 60)
    if hours and rem:
        return f"-PT{hours}H{rem}M"
    if hours:
        return f"-PT{hours}H"
    return f"-PT{rem}M"


def _vevent_to_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    dtstart = raw.get("dtstart")
    if not dtstart:
        logging.warning("[calendarbot] Skipping VEVENT without DTSTART (uid=%s)", raw.get("uid"))
        return None

    dt_value, tz_name = dtstart
    if dt_value.tzinfo is not None:
        # Preserve UTC explicitly; other aware tz values are not expected in this parser.
        dt_for_fields = dt_value.astimezone(timezone.utc).replace(tzinfo=None)
        tz_name = "UTC"
    else:
        dt_for_fields = dt_value

    event: dict[str, Any] = {
        "id": raw.get("uid") or raw.get("summary") or f"event-{dt_for_fields.isoformat()}",
        "name": raw.get("summary") or "Unnamed Event",
        "time": dt_for_fields.strftime("%H:%M"),
        "reminder_minutes": raw.get("reminder_minutes", 0),
        "message": raw.get("description", ""),
        "enabled": raw.get("enabled", True),
    }

    if tz_name and tz_name != "floating":
        event["timezone"] = tz_name

    rrule = raw.get("rrule")
    if not rrule:
        event["recurrence"] = "once"
        event["date"] = dt_for_fields.strftime("%Y-%m-%d")
        return event

    freq = (rrule.get("FREQ") or "").upper()
    if freq == "DAILY":
        event["recurrence"] = "daily"
        return event

    if freq == "WEEKLY":
        byday_raw = (rrule.get("BYDAY") or "").split(",")[0].strip().upper()
        weekday = _ICS_BYDAY_TO_WEEKDAY.get(byday_raw)
        if weekday is None:
            weekday = dt_for_fields.weekday()
        event["recurrence"] = "weekly"
        event["weekday"] = weekday
        return event

    if freq == "MONTHLY":
        month_day_raw = (rrule.get("BYMONTHDAY") or "").split(",")[0].strip()
        month_day = _parse_int(month_day_raw, default=dt_for_fields.day)
        if month_day is None or month_day <= 0:
            logging.warning("[calendarbot] Unsupported MONTHLY BYMONTHDAY=%s for %s", month_day_raw, event["id"])
            return None
        event["recurrence"] = "monthly"
        event["day"] = month_day
        return event

    if freq == "YEARLY":
        month_raw = (rrule.get("BYMONTH") or "").split(",")[0].strip()
        day_raw = (rrule.get("BYMONTHDAY") or "").split(",")[0].strip()
        month = _parse_int(month_raw, default=dt_for_fields.month)
        day = _parse_int(day_raw, default=dt_for_fields.day)
        if month is None or day is None:
            logging.warning("[calendarbot] Unsupported YEARLY RRULE for %s", event["id"])
            return None
        event["recurrence"] = "yearly"
        event["month"] = month
        event["day"] = day
        return event

    logging.warning("[calendarbot] Unsupported RRULE FREQ=%s for %s", freq, event["id"])
    return None


def _parse_vevent_lines(event_lines: list[str], ics_path: Path) -> dict[str, Any]:
    current: dict[str, Any] = {}
    nested_depth = 0
    in_valarm = False

    for line in event_lines:
        upper_line = line.upper()
        if upper_line == "BEGIN:VEVENT" or upper_line == "END:VEVENT":
            continue

        if upper_line.startswith("BEGIN:"):
            nested_depth += 1
            if upper_line == "BEGIN:VALARM":
                in_valarm = True
            continue
        if upper_line.startswith("END:"):
            if upper_line == "END:VALARM":
                in_valarm = False
            if nested_depth > 0:
                nested_depth -= 1
            continue

        parsed = _parse_prop(line)
        if parsed is None:
            continue
        name, params, value = parsed

        if nested_depth == 0:
            if name == "UID":
                current["uid"] = _ics_unescape(value)
            elif name == "SUMMARY":
                current["summary"] = _ics_unescape(value)
            elif name == "DESCRIPTION":
                current["description"] = _ics_unescape(value)
            elif name == "DTSTART":
                parsed_dt = _parse_dtstart(params, value)
                if parsed_dt is None:
                    logging.warning("[calendarbot] Invalid DTSTART in %s: %s", ics_path, line)
                else:
                    current["dtstart"] = parsed_dt
            elif name == "RRULE":
                current["rrule"] = _parse_rrule(value)
            elif name == "X-ALERTBOT-REMINDER-MINUTES":
                current["reminder_minutes"] = _parse_int(value, default=0) or 0
            elif name == "X-ALERTBOT-ENABLED":
                current["enabled"] = value.strip().lower() not in {"0", "false", "no", "off"}
            continue

        if in_valarm and name == "TRIGGER" and "reminder_minutes" not in current:
            parsed_minutes = _parse_valarm_trigger_minutes(value)
            if parsed_minutes is not None:
                current["reminder_minutes"] = parsed_minutes

    return current


def load_calendar_events_from_ics(path: Path | None = None) -> list[dict[str, Any]]:
    ics_path = path or DEFAULT_ICS_PATH
    if not ics_path.exists():
        with _ICS_FILE_LOCK:
            if not ics_path.exists():
                _ensure_calendar_file_exists(ics_path)
                logging.info("[calendarbot] Created empty ICS file: %s", ics_path)
        return []

    with _ICS_FILE_LOCK:
        try:
            text = ics_path.read_text(encoding="utf-8")
        except Exception as exc:
            logging.error("[calendarbot] Failed to read ICS file %s: %s", ics_path, exc)
            return []

    lines = _unfold_ics_lines(text)
    events: list[dict[str, Any]] = []

    current_lines: list[str] | None = None
    nested_depth = 0
    for line in lines:
        upper_line = line.upper()
        if upper_line == "BEGIN:VEVENT":
            current_lines = [line]
            nested_depth = 0
            continue
        if upper_line == "END:VEVENT":
            if current_lines is not None:
                current_lines.append(line)
                current = _parse_vevent_lines(current_lines, ics_path)
                event = _vevent_to_event(current)
                if event and event.get("enabled", True):
                    events.append(event)
            current_lines = None
            nested_depth = 0
            continue
        if current_lines is None:
            continue

        if upper_line.startswith("BEGIN:"):
            nested_depth += 1
        if upper_line.startswith("END:") and nested_depth > 0:
            nested_depth -= 1
        current_lines.append(line)

    return events


def _normalize_calendar_lines(existing_text: str | None) -> list[str]:
    if not existing_text:
        return list(_CAL_TEMPLATE_LINES)
    lines = existing_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line for line in lines if line != ""]
    if not lines:
        return list(_CAL_TEMPLATE_LINES)
    if not any(line.strip().upper() == "END:VCALENDAR" for line in lines):
        logging.warning("[calendarbot] Invalid ICS file, rebuilding VCALENDAR wrapper")
        return list(_CAL_TEMPLATE_LINES)
    return lines


def _write_calendar_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    content = "\r\n".join(lines) + "\r\n"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp_path, path)


def _ensure_calendar_file_exists(path: Path) -> None:
    """Create an empty calendar ICS file if it doesn't exist yet."""
    if path.exists():
        return
    _write_calendar_lines(path, list(_CAL_TEMPLATE_LINES))


def _serialize_dtstart_line(when_local: datetime, tz_name: str | None) -> str:
    if tz_name and tz_name.upper() == "UTC":
        return f"DTSTART:{when_local.strftime('%Y%m%dT%H%M%SZ')}"
    if tz_name:
        return f"DTSTART;TZID={_ics_escape(tz_name)}:{when_local.strftime('%Y%m%dT%H%M%S')}"
    return f"DTSTART:{when_local.strftime('%Y%m%dT%H%M%S')}"


def _build_rrule_for_event(
    *,
    recurrence: str,
    when_local: datetime,
    weekday: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> str | None:
    rec = (recurrence or "once").strip().lower()
    if rec == "once":
        return None
    if rec == "daily":
        return "FREQ=DAILY"
    if rec == "weekly":
        byday = _WEEKDAY_TO_ICS_BYDAY.get(weekday if weekday is not None else when_local.weekday())
        if not byday:
            raise ValueError("Weekly recurrence requires a valid weekday")
        return f"FREQ=WEEKLY;BYDAY={byday}"
    if rec == "monthly":
        month_day = day if day is not None else when_local.day
        if not (1 <= int(month_day) <= 31):
            raise ValueError("Monthly recurrence requires day=1..31")
        return f"FREQ=MONTHLY;BYMONTHDAY={int(month_day)}"
    if rec == "yearly":
        rr_month = month if month is not None else when_local.month
        rr_day = day if day is not None else when_local.day
        if not (1 <= int(rr_month) <= 12):
            raise ValueError("Yearly recurrence requires month=1..12")
        if not (1 <= int(rr_day) <= 31):
            raise ValueError("Yearly recurrence requires day=1..31")
        return f"FREQ=YEARLY;BYMONTH={int(rr_month)};BYMONTHDAY={int(rr_day)}"
    raise ValueError(f"Unsupported recurrence '{recurrence}'")


def _serialize_valarm_lines(reminder_minutes: int, description: str = "") -> list[str]:
    if reminder_minutes <= 0:
        return []
    return [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_ics_escape(description or 'AlertBot reminder')}",
        f"TRIGGER:{_format_valarm_trigger_minutes(reminder_minutes)}",
        "END:VALARM",
    ]


def _serialize_one_time_vevent(
    *,
    uid: str,
    name: str,
    when_local: datetime,
    reminder_minutes: int,
    message: str,
) -> list[str]:
    return _serialize_vevent(
        uid=uid,
        name=name,
        when_local=when_local,
        reminder_minutes=reminder_minutes,
        message=message,
        recurrence="once",
    )


def _serialize_vevent(
    *,
    uid: str,
    name: str,
    when_local: datetime,
    reminder_minutes: int,
    message: str,
    recurrence: str = "once",
    tz_name: str | None = None,
    enabled: bool = True,
    weekday: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> list[str]:
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{_ics_escape(uid)}",
        f"DTSTAMP:{dtstamp}",
        _serialize_dtstart_line(when_local, tz_name),
        f"SUMMARY:{_ics_escape(name)}",
    ]
    rrule = _build_rrule_for_event(
        recurrence=recurrence,
        when_local=when_local,
        weekday=weekday,
        month=month,
        day=day,
    )
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if message:
        lines.append(f"DESCRIPTION:{_ics_escape(message)}")
    lines.append(f"X-ALERTBOT-REMINDER-MINUTES:{int(reminder_minutes)}")
    if not enabled:
        lines.append("X-ALERTBOT-ENABLED:false")
    lines.extend(_serialize_valarm_lines(int(reminder_minutes), description=message))
    lines.append("END:VEVENT")
    return lines


def append_one_time_event_to_ics(
    *,
    name: str,
    when_local: datetime,
    reminder_minutes: int = 0,
    message: str = "",
    path: Path | None = None,
) -> dict[str, Any]:
    """Append a one-time floating-time event to the calendar ICS file."""
    if not name.strip():
        raise ValueError("Event name is required")
    if reminder_minutes < 0:
        raise ValueError("reminder_minutes must be >= 0")

    return append_calendar_event_to_ics(
        name=name,
        when_local=when_local,
        reminder_minutes=reminder_minutes,
        message=message,
        recurrence="once",
        path=path,
    )


def append_calendar_event_to_ics(
    *,
    name: str,
    when_local: datetime,
    recurrence: str = "once",
    reminder_minutes: int = 0,
    message: str = "",
    tz_name: str | None = None,
    path: Path | None = None,
    uid: str | None = None,
    enabled: bool = True,
    weekday: int | None = None,
    month: int | None = None,
    day: int | None = None,
) -> dict[str, Any]:
    """Append a calendar event to the ICS file.

    `when_local` is a naive datetime interpreted as a wall-clock time in `tz_name`
    (or floating/local if `tz_name` is None).
    """
    if not name.strip():
        raise ValueError("Event name is required")
    if reminder_minutes < 0:
        raise ValueError("reminder_minutes must be >= 0")

    ics_path = path or DEFAULT_ICS_PATH
    resolved_uid = uid or f"{uuid.uuid4()}@alertbot.local"

    vevent_lines = _serialize_vevent(
        uid=resolved_uid,
        name=name,
        when_local=when_local,
        reminder_minutes=int(reminder_minutes),
        message=message,
        recurrence=recurrence,
        tz_name=tz_name,
        enabled=enabled,
        weekday=weekday,
        month=month,
        day=day,
    )

    with _ICS_FILE_LOCK:
        existing_text = ics_path.read_text(encoding="utf-8") if ics_path.exists() else None
        lines = _normalize_calendar_lines(existing_text)

        end_idx = next(
            (
                i
                for i, line in reversed(list(enumerate(lines)))
                if line.strip().upper() == "END:VCALENDAR"
            ),
            None,
        )
        if end_idx is None:
            lines = list(_CAL_TEMPLATE_LINES)
            end_idx = len(lines) - 1

        new_lines = [*lines[:end_idx], *vevent_lines, *lines[end_idx:]]
        _write_calendar_lines(ics_path, new_lines)

    result: dict[str, Any] = {
        "uid": resolved_uid,
        "name": name,
        "date": when_local.strftime("%Y-%m-%d"),
        "time": when_local.strftime("%H:%M"),
        "recurrence": recurrence,
        "reminder_minutes": int(reminder_minutes),
        "message": message,
        "path": str(ics_path),
    }
    if tz_name:
        result["timezone"] = tz_name
    return result


def _scan_vevent_blocks(lines: list[str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    start_idx: int | None = None
    nested_depth = 0
    for idx, line in enumerate(lines):
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT" and start_idx is None:
            start_idx = idx
            nested_depth = 0
            continue
        if start_idx is None:
            continue
        if upper.startswith("BEGIN:") and upper != "BEGIN:VEVENT":
            nested_depth += 1
        elif upper.startswith("END:") and upper != "END:VEVENT" and nested_depth > 0:
            nested_depth -= 1
        elif upper == "END:VEVENT" and nested_depth == 0:
            block_lines = lines[start_idx: idx + 1]
            uid = None
            summary = None
            for raw in block_lines:
                parsed = _parse_prop(raw)
                if not parsed:
                    continue
                name, _, value = parsed
                if name == "UID" and uid is None:
                    uid = _ics_unescape(value)
                elif name == "SUMMARY" and summary is None:
                    summary = _ics_unescape(value)
            blocks.append(
                {
                    "start": start_idx,
                    "end": idx,
                    "uid": uid,
                    "summary": summary,
                }
            )
            start_idx = None
            nested_depth = 0
    return blocks


def delete_event_from_ics(query: str, path: Path | None = None) -> dict[str, Any]:
    """Delete a VEVENT by UID (exact/prefix) or unique name match."""
    q = (query or "").strip()
    if not q:
        raise ValueError("Event ID or name is required")

    ics_path = path or DEFAULT_ICS_PATH
    if not ics_path.exists():
        raise ValueError(f"ICS file not found: {ics_path}")

    parsed_events = load_calendar_events_from_ics(ics_path)
    if not parsed_events:
        raise ValueError("No events found")

    lowered = q.lower()
    exact_uid = [e for e in parsed_events if str(e.get("id", "")) == q]
    uid_prefix = [e for e in parsed_events if str(e.get("id", "")).startswith(q)]
    exact_name = [e for e in parsed_events if str(e.get("name", "")).lower() == lowered]
    name_contains = [e for e in parsed_events if lowered in str(e.get("name", "")).lower()]

    matches = exact_uid or (uid_prefix if len(uid_prefix) == 1 else []) or (exact_name if len(exact_name) == 1 else []) or (name_contains if len(name_contains) == 1 else [])
    if not matches:
        # Report ambiguity if it exists
        ambiguous_count = None
        for candidate_set in (uid_prefix, exact_name, name_contains):
            if len(candidate_set) > 1:
                ambiguous_count = len(candidate_set)
                break
        if ambiguous_count:
            raise ValueError(f"Query is ambiguous; matches {ambiguous_count} events. Use the full UID from /listevents.")
        raise ValueError("No matching event found")

    target = matches[0]
    target_uid = str(target.get("id", ""))

    with _ICS_FILE_LOCK:
        existing_text = ics_path.read_text(encoding="utf-8")
        lines = _normalize_calendar_lines(existing_text)
        blocks = _scan_vevent_blocks(lines)
        matching_blocks = [b for b in blocks if (b.get("uid") or "") == target_uid]
        if not matching_blocks:
            raise ValueError("Matching event UID not found in ICS file")
        block = matching_blocks[0]
        new_lines = [*lines[: block["start"]], *lines[block["end"] + 1 :]]
        _write_calendar_lines(ics_path, new_lines)

    return {
        "uid": target_uid,
        "name": target.get("name"),
        "recurrence": target.get("recurrence"),
        "date": target.get("date"),
        "time": target.get("time"),
        "path": str(ics_path),
    }


def recurrence_to_rrule(event: dict[str, Any]) -> str | None:
    """Optional helper for manual migration tooling."""
    recurrence = event.get("recurrence", "once")
    if recurrence == "daily":
        return "FREQ=DAILY"
    if recurrence == "weekly":
        weekday = event.get("weekday")
        byday = _WEEKDAY_TO_ICS_BYDAY.get(weekday)
        return f"FREQ=WEEKLY;BYDAY={byday}" if byday else None
    if recurrence == "monthly":
        day = event.get("day")
        return f"FREQ=MONTHLY;BYMONTHDAY={day}" if isinstance(day, int) else None
    if recurrence == "yearly":
        month = event.get("month")
        day = event.get("day")
        if isinstance(month, int) and isinstance(day, int):
            return f"FREQ=YEARLY;BYMONTH={month};BYMONTHDAY={day}"
    return None
