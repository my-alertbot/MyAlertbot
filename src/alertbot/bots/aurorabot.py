#!/usr/bin/env python3
"""Aurora alert bot (Option A): alerts on strong geomagnetic activity."""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alertbot.common import (
    STATE_DIR,
    calculate_lookback_minutes,
    format_run_info,
    getenv_required,
    iso_now,
    load_env_file,
    load_json,
    load_location,
    parse_iso_utc,
    request_json,
    save_json,
    send_telegram_alert,
    setup_logging,
)

KP_API_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
NOAA_SCALES_API_URL = "https://services.swpc.noaa.gov/products/noaa-scales.json"
NOAA_AURORA_LINK = "https://www.swpc.noaa.gov/products/aurora-30-minute-forecast"

DEFAULT_STATE_FILE = STATE_DIR / "aurorabot.state.json"
DEFAULT_SIMPLE_KP_THRESHOLD = 7.0
DEFAULT_ALERT_COOLDOWN_MINUTES = 360
DEFAULT_MANUAL_LOOKBACK_MINUTES = 720
DEFAULT_MAX_LOOKBACK_MINUTES = 1440
RE_ALERT_KP_DELTA = 1.0


def _parse_kp_value(raw_value: Any) -> float | None:
    if isinstance(raw_value, (int, float)):
        return float(raw_value)

    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            match = re.search(r"-?\d+(?:\.\d+)?", stripped)
            if match:
                try:
                    return float(match.group(0))
                except ValueError:
                    return None
    return None


def fetch_kp_points() -> list[tuple[datetime, float]]:
    payload = request_json(KP_API_URL)
    if not isinstance(payload, list):
        raise RuntimeError("Kp API response is not a list")

    points: list[tuple[datetime, float]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        time_tag = item.get("time_tag")
        if not isinstance(time_tag, str) or not time_tag.strip():
            continue

        ts = parse_iso_utc(time_tag)
        if ts is None:
            continue

        kp = _parse_kp_value(item.get("estimated_kp"))
        if kp is None:
            kp = _parse_kp_value(item.get("kp_index"))
        if kp is None:
            kp = _parse_kp_value(item.get("kp"))
        if kp is None:
            continue

        points.append((ts, kp))

    if not points:
        raise RuntimeError("No valid Kp points found in NOAA response")

    points.sort(key=lambda entry: entry[0])
    return points


def fetch_current_g_scale() -> str | None:
    payload = request_json(NOAA_SCALES_API_URL)
    if not isinstance(payload, dict):
        return None

    now_block = payload.get("0")
    if not isinstance(now_block, dict):
        return None

    g_info = now_block.get("G")
    if not isinstance(g_info, dict):
        return None

    scale = g_info.get("Scale")
    if scale is None:
        return None
    return str(scale)


def _fmt_ts(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_status_message(
    location_name: str,
    threshold: float,
    lookback_minutes: int,
    latest_point: tuple[datetime, float],
    peak_point: tuple[datetime, float],
    g_scale: str | None,
) -> str:
    latest_ts, latest_kp = latest_point
    peak_ts, peak_kp = peak_point
    g_text = f"G{g_scale}" if g_scale is not None and g_scale != "None" else "unknown"
    alert_state = "YES" if peak_kp >= threshold else "NO"

    return (
        f"🌌 Aurora Status ({location_name})\n\n"
        f"Mode: Option A (Kp-only)\n"
        f"Strong-alert threshold: Kp >= {threshold:.1f}\n"
        f"Window checked: last {lookback_minutes}m\n"
        f"Latest Kp: {latest_kp:.2f} at {_fmt_ts(latest_ts)}\n"
        f"Peak Kp in window: {peak_kp:.2f} at {_fmt_ts(peak_ts)}\n"
        f"NOAA geomagnetic scale: {g_text}\n"
        f"Threshold met now/window: {alert_state}\n"
        f"More info: {NOAA_AURORA_LINK}"
    )


def _format_alert_message(
    location_name: str,
    threshold: float,
    latest_point: tuple[datetime, float],
    peak_point: tuple[datetime, float],
    g_scale: str | None,
    lookback_minutes: int,
) -> str:
    latest_ts, latest_kp = latest_point
    peak_ts, peak_kp = peak_point
    g_text = f"G{g_scale}" if g_scale is not None and g_scale != "None" else "unknown"

    return (
        f"🌌 Aurora Alert: Strong Geomagnetic Activity\n\n"
        f"Location: {location_name}\n"
        f"Trigger: Kp >= {threshold:.1f}\n"
        f"Peak Kp in last {lookback_minutes}m: {peak_kp:.2f} at {_fmt_ts(peak_ts)}\n"
        f"Latest Kp: {latest_kp:.2f} at {_fmt_ts(latest_ts)}\n"
        f"NOAA geomagnetic scale: {g_text}\n"
        f"More info: {NOAA_AURORA_LINK}"
    )


def _select_points_for_window(
    points: list[tuple[datetime, float]],
    lookback_minutes: int,
) -> tuple[tuple[datetime, float], tuple[datetime, float]]:
    latest_point = points[-1]
    window_start = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    window_points = [entry for entry in points if entry[0] >= window_start]
    if not window_points:
        window_points = [latest_point]
    peak_point = max(window_points, key=lambda entry: (entry[1], entry[0]))
    return latest_point, peak_point


def _should_send_scheduled_alert(
    peak_kp: float,
    threshold: float,
    cooldown_minutes: int,
    state: dict[str, Any],
) -> tuple[bool, str]:
    if peak_kp < threshold:
        return False, f"No strong aurora activity (peak Kp {peak_kp:.2f} < {threshold:.1f})"

    last_alert_raw = state.get("last_alert_time")
    last_alert_kp_raw = state.get("last_alert_kp")

    if not isinstance(last_alert_raw, str) or not last_alert_raw.strip():
        return True, "Threshold met and no previous alert recorded"

    last_alert_time = parse_iso_utc(last_alert_raw)
    if last_alert_time is None:
        return True, "Threshold met and previous alert timestamp is invalid"

    elapsed = datetime.now(timezone.utc) - last_alert_time
    if elapsed >= timedelta(minutes=cooldown_minutes):
        return True, f"Threshold met and cooldown elapsed ({elapsed})"

    try:
        last_alert_kp = float(last_alert_kp_raw)
    except (TypeError, ValueError):
        last_alert_kp = None

    if last_alert_kp is not None and peak_kp >= (last_alert_kp + RE_ALERT_KP_DELTA):
        return True, (
            "Threshold met and Kp increased significantly "
            f"({peak_kp:.2f} vs last alert {last_alert_kp:.2f})"
        )

    remaining = timedelta(minutes=cooldown_minutes) - elapsed
    return False, f"Threshold met but in cooldown ({remaining} remaining)"


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run aurora check using NOAA Kp data (Option A)."""
    logging.debug("Running aurorabot: %s", format_run_info(schedule_context))

    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "alerts_sent": 0, "error": str(exc)}

    threshold = float(os.getenv("AURORA_SIMPLE_KP_THRESHOLD", str(DEFAULT_SIMPLE_KP_THRESHOLD)))
    cooldown_minutes = int(
        os.getenv("AURORA_ALERT_COOLDOWN_MINUTES", str(DEFAULT_ALERT_COOLDOWN_MINUTES))
    )
    state_path = Path(os.getenv("AURORA_STATE_FILE", str(DEFAULT_STATE_FILE))).expanduser()
    location = load_location()

    lookback_minutes = calculate_lookback_minutes(
        schedule_context,
        default_minutes=DEFAULT_MANUAL_LOOKBACK_MINUTES,
        buffer_multiplier=1.2,
        max_minutes=DEFAULT_MAX_LOOKBACK_MINUTES,
    )

    try:
        kp_points = fetch_kp_points()
        g_scale = fetch_current_g_scale()
    except Exception as exc:
        logging.error("[aurorabot] Failed to fetch NOAA data: %s", exc)
        return {
            "success": False,
            "alerts_sent": 0,
            "error": f"Failed to fetch NOAA data: {exc}",
        }

    latest_point, peak_point = _select_points_for_window(kp_points, lookback_minutes)
    peak_kp = peak_point[1]

    if manual_trigger:
        message = _format_status_message(
            location.display_name,
            threshold,
            lookback_minutes,
            latest_point,
            peak_point,
            g_scale,
        )
        try:
            send_telegram_alert(tg_token, tg_chat_id, message)
            return {
                "success": True,
                "alerts_sent": 1,
                "message": message,
                "error": None,
            }
        except Exception as exc:
            logging.error("[aurorabot] Failed to send manual status: %s", exc)
            return {"success": False, "alerts_sent": 0, "error": str(exc)}

    state = load_json(state_path, {"last_alert_time": None, "last_alert_kp": None})
    should_alert, reason = _should_send_scheduled_alert(
        peak_kp=peak_kp,
        threshold=threshold,
        cooldown_minutes=cooldown_minutes,
        state=state,
    )
    logging.info("[aurorabot] Decision: %s", reason)

    if not should_alert:
        return {"success": True, "alerts_sent": 0, "message": reason, "error": None}

    message = _format_alert_message(
        location.display_name,
        threshold,
        latest_point,
        peak_point,
        g_scale,
        lookback_minutes,
    )
    try:
        send_telegram_alert(tg_token, tg_chat_id, message)
    except Exception as exc:
        logging.error("[aurorabot] Failed to send alert: %s", exc)
        return {"success": False, "alerts_sent": 0, "error": str(exc)}

    state["last_alert_time"] = iso_now()
    state["last_alert_kp"] = peak_kp
    save_json(state_path, state)

    return {
        "success": True,
        "alerts_sent": 1,
        "message": message,
        "error": None,
    }


def main() -> int:
    setup_logging()
    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
