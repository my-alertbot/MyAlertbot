#!/usr/bin/env python3
"""Sunrise/sunset bot - manual-only daily sun times for the shared location."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Any

from alertbot.common import (
    format_run_info,
    getenv_required,
    load_env_file,
    load_location,
    request_json,
    send_telegram_alert,
    setup_logging,
)

API_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_sun_times(latitude: str, longitude: str, timezone_name: str) -> dict[str, Any]:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "sunrise,sunset",
        "forecast_days": 1,
        "timezone": timezone_name or "auto",
    }
    return request_json(API_URL, params=params)


def _format_time(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except Exception:
        # Open-Meteo usually returns ISO-8601 local timestamps; preserve raw tail if parsing fails.
        return value.replace("T", " ")


def format_sun_times_message(location_name: str, timezone_name: str, payload: dict[str, Any]) -> str:
    daily = payload.get("daily", {}) if isinstance(payload, dict) else {}
    dates = daily.get("time") or []
    sunrises = daily.get("sunrise") or []
    sunsets = daily.get("sunset") or []

    date_value = dates[0] if dates else "unknown"
    sunrise_value = sunrises[0] if sunrises else None
    sunset_value = sunsets[0] if sunsets else None
    if sunrise_value is None or sunset_value is None:
        raise ValueError("sunrise/sunset data missing from weather response")

    lines = [
        f"🌅 Sun Times for {location_name}",
        "",
        f"Date: {date_value}",
        f"Sunrise: {_format_time(sunrise_value)}",
        f"Sunset: {_format_time(sunset_value)}",
        f"Timezone: {timezone_name or 'local'}",
    ]
    return "\n".join(lines)


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run sunrise/sunset lookup.

    This bot is intended to be manual-only.
    """
    logging.debug("Running sunrisebot: %s", format_run_info(schedule_context))

    if not manual_trigger:
        msg = "sunrisebot is manual-only; skipping scheduled run"
        logging.info("[sunrisebot] %s", msg)
        return {"success": True, "alerts_sent": 0, "message": msg}

    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    location = load_location()
    latitude = str(location.latitude)
    longitude = str(location.longitude)
    timezone_name = location.timezone or "auto"

    try:
        payload = fetch_sun_times(latitude, longitude, timezone_name)
        message = format_sun_times_message(location.display_name, timezone_name, payload)
    except Exception as exc:
        logging.error("[sunrisebot] Failed to fetch/format sun times: %s", exc)
        return {"success": False, "error": f"Failed to get sun times: {exc}", "alerts_sent": 0}

    try:
        send_telegram_alert(tg_token, tg_chat_id, message)
        logging.info("[sunrisebot] Sun times sent")
        return {"success": True, "message": message, "alerts_sent": 1}
    except Exception as exc:
        logging.error("[sunrisebot] Failed to send Telegram alert: %s", exc)
        return {"success": False, "error": str(exc), "alerts_sent": 0}


def main() -> int:
    setup_logging()
    load_env_file()
    result = run(manual_trigger=True)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
