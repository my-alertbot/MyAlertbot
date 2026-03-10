#!/usr/bin/env python3
"""Weather forecast bot - sends daily summary forecasts."""

import logging
import os
import sys
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


def fetch_forecast(latitude: str, longitude: str) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": ",".join(
            [
                "temperature_2m",
                "precipitation_probability",
            ]
        ),
        "daily": ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "precipitation_sum",
            ]
        ),
        "timezone": "auto",
    }
    return request_json(API_URL, params=params)


def _get_value(values: list[Any] | None, idx: int) -> Any | None:
    if not values:
        return None
    try:
        return values[idx]
    except Exception:
        return None


def _format_day_summary(
    day_label: str,
    date_value: str,
    temp_min: float | None,
    temp_max: float | None,
    precip_prob: int | None,
    precip_sum: float | None,
    temp_unit: str,
    precip_unit: str,
) -> str:
    parts = [f"{day_label} ({date_value})"]
    if temp_min is not None and temp_max is not None:
        parts.append(f"Temp: {temp_min:.0f}–{temp_max:.0f}{temp_unit}")
    if precip_prob is not None:
        parts.append(f"Precip: {precip_prob}%")
    if precip_sum is not None:
        parts.append(f"Total: {precip_sum:.1f}{precip_unit}")
    return " | ".join(parts)


def _format_current_summary(
    current_temp: float | None,
    current_precip_prob: int | None,
    temp_unit: str,
    precip_prob_unit: str,
) -> str | None:
    parts = ["Current"]
    if current_temp is not None:
        parts.append(f"Temp: {current_temp:.0f}{temp_unit}")
    if current_precip_prob is not None:
        parts.append(f"Precip: {current_precip_prob}{precip_prob_unit}")
    if len(parts) == 1:
        return None
    return " | ".join(parts)


def format_forecast_message(location_name: str, forecast: dict) -> str:
    current = forecast.get("current", {}) if isinstance(forecast, dict) else {}
    daily = forecast.get("daily", {}) if isinstance(forecast, dict) else {}
    current_units = forecast.get("current_units", {}) if isinstance(forecast, dict) else {}
    units = forecast.get("daily_units", {}) if isinstance(forecast, dict) else {}

    current_temp = current.get("temperature_2m")
    current_precip_prob = current.get("precipitation_probability")
    dates = daily.get("time") or []
    temp_max = daily.get("temperature_2m_max") or []
    temp_min = daily.get("temperature_2m_min") or []
    precip_prob = daily.get("precipitation_probability_max") or []
    precip_sum = daily.get("precipitation_sum") or []

    current_temp_unit = current_units.get("temperature_2m", units.get("temperature_2m_max", "°C"))
    current_precip_prob_unit = current_units.get("precipitation_probability", "%")
    temp_unit = units.get("temperature_2m_max", "°C")
    precip_unit = units.get("precipitation_sum", "mm")
    current_line = _format_current_summary(
        current_temp,
        current_precip_prob,
        current_temp_unit,
        current_precip_prob_unit,
    )

    today_line = _format_day_summary(
        "Today",
        _get_value(dates, 0) or "unknown",
        _get_value(temp_min, 0),
        _get_value(temp_max, 0),
        _get_value(precip_prob, 0),
        _get_value(precip_sum, 0),
        temp_unit,
        precip_unit,
    )

    tomorrow_line = _format_day_summary(
        "Tomorrow",
        _get_value(dates, 1) or "unknown",
        _get_value(temp_min, 1),
        _get_value(temp_max, 1),
        _get_value(precip_prob, 1),
        _get_value(precip_sum, 1),
        temp_unit,
        precip_unit,
    )

    message_lines = [f"🌦️ Weather Forecast for {location_name}", ""]
    if current_line:
        message_lines.append(current_line)
    message_lines.append(today_line)
    message_lines.append(tomorrow_line)
    return "\n".join(message_lines)


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run weather forecast check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running weatherbot: %s", format_run_info(schedule_context))

    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    location = load_location()
    latitude = str(location.latitude)
    longitude = str(location.longitude)
    location_name = location.display_name

    try:
        forecast = fetch_forecast(latitude, longitude)
    except Exception as exc:
        logging.error("[weatherbot] Failed to fetch forecast: %s", exc)
        return {"success": False, "error": f"Failed to fetch forecast: {exc}", "alerts_sent": 0}

    message = format_forecast_message(location_name, forecast)

    try:
        send_telegram_alert(tg_token, tg_chat_id, message)
        logging.info("[weatherbot] Forecast sent")
        return {
            "success": True,
            "message": message,
            "alerts_sent": 1,
        }
    except Exception as exc:
        logging.error("[weatherbot] Failed to send Telegram alert: %s", exc)
        return {"success": False, "error": str(exc), "alerts_sent": 0}


def main() -> int:
    setup_logging()
    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
