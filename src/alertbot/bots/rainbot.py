#!/usr/bin/env python3
"""Rain alert bot - sends Telegram alert when rain is forecasted."""

import logging
import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from alertbot.common import (
    format_run_info,
    load_env_file,
    getenv_required,
    request_json,
    send_telegram_alert,
    setup_logging,
    load_location,
)

# Location is now loaded from shared state (state/location_state.json)
# Use /location command or edit the state file to change location
DEFAULT_RAIN_THRESHOLD = 45
DEFAULT_SLEEP_START = "02:00"
DEFAULT_SLEEP_END = "08:00"
API_URL = "https://api.open-meteo.com/v1/forecast"


def is_sleep_time(timezone: str, sleep_start: str, sleep_end: str) -> bool:
    """Check if current time is within the sleep period."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz).time()
    start = datetime.strptime(sleep_start, "%H:%M").time()
    end = datetime.strptime(sleep_end, "%H:%M").time()

    if start <= end:
        return start <= now < end
    else:
        # Sleep period crosses midnight
        return now >= start or now < end


def fetch_forecast(latitude: str, longitude: str) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "precipitation_probability,precipitation",
        "forecast_hours": "24",
        "timezone": "auto",
    }
    return request_json(API_URL, params=params)


def find_rain_hours(forecast: dict, threshold: int) -> list[tuple[str, int]]:
    """Return list of (time, probability) tuples where probability > threshold."""
    hourly = forecast.get("hourly", {})
    times = hourly.get("time", [])
    probabilities = hourly.get("precipitation_probability", [])

    rain_hours = []
    for time_str, prob in zip(times, probabilities):
        if prob is not None and prob > threshold:
            rain_hours.append((time_str, prob))
    return rain_hours


def _parse_forecast_time(time_str: str, timezone: str) -> datetime | None:
    """Parse Open-Meteo hourly time into the configured local timezone."""
    try:
        parsed = datetime.fromisoformat(time_str)
    except ValueError:
        return None

    try:
        tz = ZoneInfo(timezone)
    except Exception:
        return parsed

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _format_eta(seconds_until: int) -> str:
    if seconds_until <= 0:
        return "starting now"

    minutes_until = (seconds_until + 59) // 60
    if minutes_until < 60:
        return f"in {minutes_until}m"

    hours_until, remaining_minutes = divmod(minutes_until, 60)
    if remaining_minutes == 0:
        return f"in {hours_until}h"
    return f"in {hours_until}h {remaining_minutes}m"


def _format_time_with_eta(time_str: str, timezone: str) -> str:
    parsed = _parse_forecast_time(time_str, timezone)
    if parsed is None:
        return time_str

    if parsed.tzinfo is None:
        return parsed.strftime("%a %Y-%m-%d %H:%M")

    now = datetime.now(parsed.tzinfo)
    eta = _format_eta(int((parsed - now).total_seconds()))
    return f"{parsed.strftime('%a %Y-%m-%d %H:%M %Z')} ({eta})"


def format_alert(
    location_name: str,
    rain_hours: list[tuple[str, int]],
    threshold: int,
    timezone: str,
) -> str:
    first_time, first_prob = rain_hours[0]
    peak_time, max_prob = max(rain_hours, key=lambda x: x[1])
    likely_start = _format_time_with_eta(first_time, timezone)
    peak_time_text = _format_time_with_eta(peak_time, timezone)

    return (
        f"Rain Alert for {location_name}\n\n"
        f"Rain is forecasted in the next 24 hours.\n"
        f"Likely start: {likely_start} ({first_prob}% chance)\n"
        f"trigger: >{threshold}% probability\n"
        f"Peak probability: {max_prob}% at {peak_time_text}"
    )


def format_status(
    location_name: str,
    rain_hours: list[tuple[str, int]],
    threshold: int,
    timezone: str,
) -> str:
    """Format current rain status message for manual trigger."""
    if rain_hours:
        return format_alert(location_name, rain_hours, threshold, timezone)
    return (
        f"Rain Status for {location_name}\n\n"
        f"No rain forecasted above {threshold}% threshold in the next 24 hours.\n"
        "Likely start: not expected in the next 24 hours."
    )


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run rain check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running rainbot: %s", format_run_info(schedule_context))

    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    # Load location from shared state
    location = load_location()
    latitude = str(location.latitude)
    longitude = str(location.longitude)
    location_name = location.display_name
    timezone = location.timezone
    threshold = int(os.getenv("RAIN_THRESHOLD", DEFAULT_RAIN_THRESHOLD))
    sleep_start = os.getenv("RAIN_SLEEP_START", DEFAULT_SLEEP_START)
    sleep_end = os.getenv("RAIN_SLEEP_END", DEFAULT_SLEEP_END)

    # Skip sleep check for manual triggers
    if not manual_trigger and is_sleep_time(timezone, sleep_start, sleep_end):
        logging.info(
            "[rainbot] Sleep time (%s-%s), skipping alert", sleep_start, sleep_end
        )
        return {
            "success": True,
            "message": f"Sleep time ({sleep_start}-{sleep_end}), no alert sent",
            "alerts_sent": 0,
        }

    try:
        forecast = fetch_forecast(latitude, longitude)
    except Exception as exc:
        logging.error("[rainbot] Failed to fetch forecast: %s", exc)
        return {"success": False, "error": f"Failed to fetch forecast: {exc}", "alerts_sent": 0}

    rain_hours = find_rain_hours(forecast, threshold)

    # For manual trigger, always report status
    if manual_trigger:
        message = format_status(location_name, rain_hours, threshold, timezone)
        try:
            send_telegram_alert(tg_token, tg_chat_id, message)
            logging.info("[rainbot] Status report sent (manual trigger)")
            return {
                "success": True,
                "message": message,
                "alerts_sent": 1,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc), "alerts_sent": 0}

    # Scheduled run: only alert if rain is forecasted
    if not rain_hours:
        logging.info(
            "[rainbot] No rain forecasted above %d%% threshold", threshold
        )
        return {
            "success": True,
            "message": f"No rain forecasted above {threshold}% threshold",
            "alerts_sent": 0,
        }

    message = format_alert(location_name, rain_hours, threshold, timezone)

    try:
        send_telegram_alert(tg_token, tg_chat_id, message)
        logging.info("[rainbot] Rain alert sent")
        return {
            "success": True,
            "message": message,
            "alerts_sent": 1,
        }
    except Exception as exc:
        logging.error("[rainbot] Failed to send Telegram alert: %s", exc)
        return {"success": False, "error": str(exc), "alerts_sent": 0}


def main() -> int:
    setup_logging()
    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
