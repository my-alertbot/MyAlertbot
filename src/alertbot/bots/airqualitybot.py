import asyncio
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import Bot, BotCommand

from alertbot.common import (
    DEFAULT_TIMEOUT,
    STATE_DIR,
    calculate_lookback_minutes,
    format_run_info,
    getenv_required,
    load_env_file,
    load_json,
    load_location,
    parse_iso_utc,
    request_json,
    request_with_retry,
    save_json,
    send_telegram_alert,
)

DEFAULT_STATE_FILE = STATE_DIR / "airqualitybot_state.json"
# Location is now loaded from shared state (state/location_state.json)
DEFAULT_THRESHOLD = 80
DEFAULT_COOLDOWN_HOURS = 6
DEFAULT_PROVIDER = "waqi"
DEFAULT_MAX_AGE_HOURS = 3
DEFAULT_STATION_RADIUS_KM = 30.0


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    aqi_api_token: str
    aqi_city: str
    latitude: float
    longitude: float
    aqi_stations: List[str]
    aqi_threshold: int
    aqi_provider: str
    aqi_max_age_hours: int
    state_file: str
    location_display_name: str  # For formatting messages


@dataclass
class State:
    last_aqi: Optional[int] = None
    last_alert_time: Optional[datetime] = None
    last_update_id: Optional[int] = None


class StationCityMismatchError(RuntimeError):
    """Raised when station list does not align with configured city."""
    pass


def load_config(schedule_context: Optional[Dict[str, Any]] = None) -> Config:
    def getenv_int(key: str, default: int) -> int:
        value = os.getenv(key)
        if value is None or value == "":
            return default
        return int(value)

    def getenv_station_list(key: str) -> List[str]:
        raw = os.getenv(key, "")
        if not raw.strip():
            return []
        items = [item.strip() for item in raw.split(",")]
        return [item for item in items if item]

    # Derive aqi_max_age_hours from schedule_context if available
    # Formula: interval_minutes / 60 + 1 (buffer)
    if schedule_context is not None:
        interval_minutes = schedule_context.get("interval_minutes")
        if interval_minutes is not None:
            aqi_max_age_hours = int(interval_minutes / 60) + 1
        else:
            aqi_max_age_hours = getenv_int("AQI_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS)
    else:
        aqi_max_age_hours = getenv_int("AQI_MAX_AGE_HOURS", DEFAULT_MAX_AGE_HOURS)

    # Load shared location
    location = load_location()

    return Config(
        telegram_bot_token=getenv_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=getenv_required("TELEGRAM_CHAT_ID"),
        aqi_api_token=getenv_required("AQI_API_TOKEN"),
        aqi_city=location.aqi_city,
        latitude=location.latitude,
        longitude=location.longitude,
        aqi_stations=getenv_station_list("AQI_STATIONS"),
        aqi_threshold=getenv_int("AQI_THRESHOLD", DEFAULT_THRESHOLD),
        aqi_provider=os.getenv("AQI_PROVIDER", DEFAULT_PROVIDER).lower(),
        aqi_max_age_hours=aqi_max_age_hours,
        state_file=os.getenv("AQI_STATE_FILE", str(DEFAULT_STATE_FILE)),
        location_display_name=location.display_name,
    )


def load_state(path: str) -> State:
    data = load_json(Path(path), {})
    last_alert_time_raw = data.get("last_alert_time")
    last_alert_time = None
    if last_alert_time_raw:
        last_alert_time = parse_iso_utc(last_alert_time_raw)
        if last_alert_time is None:
            logging.warning("Invalid last_alert_time in state: %s", last_alert_time_raw)
    return State(
        last_aqi=data.get("last_aqi"),
        last_alert_time=last_alert_time,
        last_update_id=data.get("last_update_id"),
    )


def save_state(path: str, state: State) -> None:
    data = {
        "last_aqi": state.last_aqi,
        "last_alert_time": (
            state.last_alert_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if state.last_alert_time
            else None
        ),
        "last_update_id": state.last_update_id,
    }
    save_json(Path(path), data)


def parse_waqi_time(data: Dict[str, Any]) -> datetime:
    time_info = data.get("time") or {}
    iso_time = time_info.get("iso")
    if isinstance(iso_time, str) and iso_time:
        return datetime.fromisoformat(iso_time.replace("Z", "+00:00"))

    timestr = time_info.get("s")
    tz_offset = time_info.get("tz")
    if isinstance(timestr, str) and timestr:
        if isinstance(tz_offset, str) and tz_offset:
            # WAQI commonly returns local wall time + separate tz offset.
            # Use this pair to get an accurate absolute timestamp.
            normalized = timestr.strip().replace(" ", "T")
            return datetime.fromisoformat(f"{normalized}{tz_offset.strip()}")
        parsed = datetime.fromisoformat(timestr.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed

    epoch = time_info.get("v")
    if epoch is not None:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)

    raise RuntimeError("WAQI API response missing timestamp")


def normalize_city_token(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


# Common city name variations (local name -> canonical name)
CITY_ALIASES = {
    "beograd": "belgrade",
    "wien": "vienna",
    "münchen": "munich",
    "munchen": "munich",
    "roma": "rome",
    "milano": "milan",
    "firenze": "florence",
    "venezia": "venice",
    "napoli": "naples",
    "köln": "cologne",
    "koln": "cologne",
    "praha": "prague",
    "warszawa": "warsaw",
    "bucuresti": "bucharest",
    "moskva": "moscow",
    "beijing": "peking",
    "guangzhou": "canton",
}


def normalize_city_for_match(value: str) -> str:
    """Normalize city name for matching, applying aliases."""
    # Extract just the city name (before comma if present)
    city_part = value.split(",")[0].strip()
    normalized = normalize_city_token(city_part)
    # Apply alias if known
    return CITY_ALIASES.get(normalized, normalized)


def city_matches(target_city: str, station_city: str) -> bool:
    """Check if station city matches target city.

    Uses alias normalization to handle local names (e.g., Beograd -> Belgrade).
    """
    target = normalize_city_for_match(target_city)
    station = normalize_city_for_match(station_city)
    if not target or not station:
        return False
    return target in station or station in target


def _parse_station_geo(data: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    city = data.get("city")
    if not isinstance(city, dict):
        return None
    geo = city.get("geo")
    if not isinstance(geo, list) or len(geo) != 2:
        return None
    try:
        return float(geo[0]), float(geo[1])
    except (TypeError, ValueError):
        return None


def _distance_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    # Haversine distance between two WGS84 points.
    earth_radius_km = 6371.0
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_km * c


def fetch_aqi_waqi_payload(city: str, token: str) -> Dict[str, Any]:
    url = f"https://api.waqi.info/feed/{city}/"
    params = {"token": token}
    payload = request_json(url, params=params, timeout=DEFAULT_TIMEOUT)
    if payload.get("status") != "ok":
        raise RuntimeError(f"WAQI API status not ok: {payload.get('status')} {payload.get('data')}")
    return payload


def parse_waqi_aqi(data: Dict[str, Any], max_age_hours: int) -> int:
    aqi = data.get("aqi")
    if aqi is None:
        raise RuntimeError("WAQI API response missing 'aqi'")
    ts = parse_waqi_time(data)
    age = datetime.now(timezone.utc) - ts
    if age > timedelta(hours=max_age_hours):
        raise RuntimeError(f"WAQI data too old ({age})")
    return int(aqi)


def fetch_aqi_waqi(city: str, token: str, max_age_hours: int) -> int:
    payload = fetch_aqi_waqi_payload(city, token)
    data = payload.get("data", {})
    return parse_waqi_aqi(data, max_age_hours)


def fetch_aqi_waqi_geo(latitude: float, longitude: float, token: str, max_age_hours: int) -> int:
    # Use coordinate lookup to avoid brittle city slug formatting (e.g., "new york").
    geo_query = f"geo:{latitude};{longitude}"
    return fetch_aqi_waqi(geo_query, token, max_age_hours)


def fetch_aqi_waqi_average(
    stations: List[str],
    token: str,
    max_age_hours: int,
    target_city: str,
    target_latitude: float,
    target_longitude: float,
) -> Tuple[int, int, int]:
    valid_values: List[int] = []
    mismatch_count = 0
    missing_city_count = 0
    error_count = 0
    for station in stations:
        try:
            payload = fetch_aqi_waqi_payload(station, token)
            data = payload.get("data", {})
            station_city = (data.get("city") or {}).get("name")
            if not isinstance(station_city, str) or not station_city.strip():
                logging.warning("Skipping station %s: missing city in WAQI response", station)
                missing_city_count += 1
                continue
            city_match = city_matches(target_city, station_city)
            if not city_match:
                station_geo = _parse_station_geo(data)
                if station_geo is not None:
                    station_lat, station_lon = station_geo
                    distance_km = _distance_km(
                        target_latitude, target_longitude, station_lat, station_lon
                    )
                    if distance_km <= DEFAULT_STATION_RADIUS_KM:
                        logging.info(
                            "Using station %s by geo proximity (%.1fkm from target)",
                            station,
                            distance_km,
                        )
                        city_match = True
            if not city_match:
                logging.warning(
                    "Skipping station %s: city '%s' does not match '%s'",
                    station,
                    station_city,
                    target_city,
                )
                mismatch_count += 1
                continue
            value = parse_waqi_aqi(data, max_age_hours)
        except Exception as exc:
            logging.warning("Skipping station %s: %s", station, exc)
            error_count += 1
            continue
        valid_values.append(value)

    if not valid_values:
        if error_count == 0 and (mismatch_count + missing_city_count) == len(stations):
            raise StationCityMismatchError("No stations matched configured city")
        raise RuntimeError("No valid AQI values returned from any station")

    avg = sum(valid_values) / len(valid_values)
    return int(round(avg)), len(valid_values), len(stations)


def fetch_aqi(config: Config) -> Tuple[int, int, int]:
    if config.aqi_provider == "waqi":
        if config.aqi_stations:
            try:
                return fetch_aqi_waqi_average(
                    config.aqi_stations,
                    config.aqi_api_token,
                    config.aqi_max_age_hours,
                    config.location_display_name,
                    config.latitude,
                    config.longitude,
                )
            except StationCityMismatchError as exc:
                logging.warning(
                    "Station list doesn't align with city, falling back to coordinate-based AQI: %s",
                    exc,
                )
        return (
            fetch_aqi_waqi_geo(
                config.latitude,
                config.longitude,
                config.aqi_api_token,
                config.aqi_max_age_hours,
            ),
            1,
            1,
        )
    raise ValueError(f"Unsupported AQI provider: {config.aqi_provider}")


ALERT_COOLDOWN_HOURS = 12


def should_alert(aqi: int, state: State, threshold: int, now: datetime) -> bool:
    if aqi <= threshold:
        return False

    last_aqi = state.last_aqi
    if last_aqi is None or last_aqi <= threshold:
        return True  # threshold crossing

    # Still above threshold — re-alert only if cooldown has expired
    last_alert_time = state.last_alert_time
    if last_alert_time is None:
        return True
    return (now - last_alert_time) >= timedelta(hours=ALERT_COOLDOWN_HOURS)


async def register_bot_commands(token: str) -> None:
    bot = Bot(token=token)
    commands = [
        BotCommand("airquality", "Get current air quality"),
        BotCommand("status", "Get AQI with bot uptime"),
    ]
    await bot.set_my_commands(commands)


def fetch_telegram_updates(token: str, offset: Optional[int], poll_timeout_seconds: int) -> List[Dict[str, Any]]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params: Dict[str, Any] = {"timeout": max(int(poll_timeout_seconds), 0)}
    if offset is not None:
        params["offset"] = offset
    try:
        request_timeout = max(min(10, int(poll_timeout_seconds) + 2), 2)
        resp = request_with_retry(
            method="GET",
            url=url,
            params=params,
            timeout=request_timeout,
            max_retries=1,
        )
    except RuntimeError as exc:
        # Poll timeout is expected occasionally for long-poll loop.
        if "timeout" in str(exc).lower():
            return []
        raise
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram API error: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Telegram API invalid JSON: {exc}") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API response not ok: {payload}")
    return payload.get("result", [])


def format_alert_message(
    city: str,
    aqi: int,
    threshold: int,
    now: datetime,
    station_count: int,
    station_total: int,
) -> str:
    ts = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    count_label = f"{station_count} station" if station_count == 1 else f"{station_count} stations"
    severity = ""
    if aqi > 120:
        severity = "🔴 "
    elif aqi > 80:
        severity = "🟡 "
    # Build dynamic map URL from city name
    map_city = city.lower().replace(" ", "")
    return (
        f"{severity}Current AQI (avg of {count_label}): {aqi} (> {threshold})\n"
        f"Stations used: {station_count}/{station_total}\n"
        f"Time: {ts}\n"
        f"Map: https://aqicn.org/map/{map_city}/\n"
    )


def format_query_message(
    aqi: Optional[int],
    now: datetime,
    station_count: Optional[int],
    station_total: Optional[int],
    city: str = "london",
) -> str:
    ts = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Build dynamic map URL from city name
    map_city = city.lower().replace(" ", "")
    if aqi is None or station_count is None or station_total is None:
        return (
            "AQI unavailable right now.\n"
            f"Time: {ts}\n"
            f"Map: https://aqicn.org/map/{map_city}/\n"
        )
    count_label = f"{station_count} station" if station_count == 1 else f"{station_count} stations"
    severity = ""
    if aqi > 120:
        severity = "🔴 "
    elif aqi > 80:
        severity = "🟡 "
    return (
        f"{severity}Current AQI (avg of {count_label}): {aqi}\n"
        f"Stations used: {station_count}/{station_total}\n"
        f"Time: {ts}\n"
        f"Map: https://aqicn.org/map/{map_city}/\n"
    )


def format_uptime_message(startup_time: datetime, now: datetime) -> str:
    elapsed = now - startup_time
    total_seconds = int(elapsed.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return "Uptime: " + " ".join(parts)


def handle_telegram_queries(
    config: Config,
    state: State,
    now: datetime,
    startup_time: datetime,
    last_aqi: Optional[int],
    station_count: Optional[int],
    station_total: Optional[int],
    poll_timeout_seconds: int,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    try:
        updates = fetch_telegram_updates(
            config.telegram_bot_token,
            state.last_update_id + 1 if state.last_update_id is not None else None,
            poll_timeout_seconds,
        )
    except Exception as exc:
        logging.warning("Failed to fetch Telegram updates: %s", exc)
        return last_aqi, station_count, station_total

    authorized_chat_id = str(config.telegram_chat_id).strip()
    now = datetime.now(timezone.utc)
    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None:
            state.last_update_id = update_id

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue
        msg_date = message.get("date")
        if msg_date is not None:
            msg_time = datetime.fromtimestamp(int(msg_date), tz=timezone.utc)
            if msg_time < startup_time:
                continue
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        if str(chat_id).strip() != authorized_chat_id:
            logging.info("Ignoring Telegram message from unauthorized chat %s", chat_id)
            continue
        text = (message.get("text") or "").strip().lower()
        is_aqi_command = text.startswith("/airquality")
        is_status_command = text.startswith("/status") or text.startswith("/uptime")
        if not is_aqi_command and not is_status_command:
            continue
        include_uptime = is_status_command
        query_aqi = last_aqi
        query_station_count = station_count
        query_station_total = station_total
        try:
            query_aqi, query_station_count, query_station_total = fetch_aqi(config)
        except Exception as exc:
            logging.warning("Failed to fetch AQI for query: %s", exc)
        aqi_label = query_aqi if query_aqi is not None else "unavailable"
        logging.info("Received Telegram message from chat %s; replying with AQI %s", chat_id, aqi_label)
        reply = format_query_message(query_aqi, now, query_station_count, query_station_total, config.aqi_city)
        if include_uptime:
            reply = reply + "\n" + format_uptime_message(startup_time, now) + "\n"
        try:
            send_telegram_alert(config.telegram_bot_token, str(chat_id), reply)
        except Exception as exc:
            logging.warning("Failed to send Telegram reply: %s", exc)
        else:
            last_aqi = query_aqi
            station_count = query_station_count
            station_total = query_station_total
    return last_aqi, station_count, station_total


def run_once(config: Config, state: State, startup_time: datetime) -> int:
    now = datetime.now(timezone.utc)

    try:
        aqi, station_count, station_total = fetch_aqi(config)
    except Exception as exc:
        logging.error("Failed to fetch AQI: %s", exc)
        handle_telegram_queries(
            config,
            state,
            now,
            startup_time,
            None,
            None,
            None,
            0,
        )
        return 1

    handle_telegram_queries(
        config,
        state,
        now,
        startup_time,
        aqi,
        station_count,
        station_total,
        0,
    )

    alert = should_alert(aqi, state, config.aqi_threshold, now)

    if aqi <= config.aqi_threshold:
        logging.info("AQI %s is below or equal to threshold %s", aqi, config.aqi_threshold)
    else:
        logging.info("AQI %s is above threshold %s", aqi, config.aqi_threshold)

    if alert:
        message = format_alert_message(
            config.aqi_city,
            aqi,
            config.aqi_threshold,
            now,
            station_count,
            station_total,
        )
        try:
            send_telegram_alert(config.telegram_bot_token, config.telegram_chat_id, message)
            state.last_alert_time = now
            logging.info("Alert sent")
        except Exception as exc:
            logging.error("Failed to send Telegram alert: %s", exc)
            return 2

    state.last_aqi = aqi
    return 0


def run(
    manual_trigger: bool = False,
    chat_id: Optional[str] = None,
    schedule_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run AQI check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running airqualitybot: %s", format_run_info(schedule_context))

    try:
        config = load_config(schedule_context)
    except Exception as exc:
        logging.error("Configuration error: %s", exc)
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    # Use provided chat_id if available
    if chat_id is not None:
        config.telegram_chat_id = chat_id

    startup_time = datetime.now(timezone.utc)
    state = load_state(config.state_file)

    # Reset state on each run for controller-managed execution
    state.last_aqi = None
    state.last_alert_time = None
    state.last_update_id = None

    try:
        save_state(config.state_file, state)
        logging.info("State reset for run: %s", config.state_file)
    except Exception as exc:
        logging.warning("Failed to reset state file %s: %s", config.state_file, exc)

    now = datetime.now(timezone.utc)

    try:
        aqi, station_count, station_total = fetch_aqi(config)
    except Exception as exc:
        logging.error("Failed to fetch AQI: %s", exc)
        return {"success": False, "error": f"Failed to fetch AQI: {exc}", "alerts_sent": 0}

    alert_sent = False
    message = None

    # For manual triggers, always send current AQI status
    if manual_trigger:
        message = format_query_message(aqi, now, station_count, station_total, config.aqi_city)
        try:
            send_telegram_alert(config.telegram_bot_token, config.telegram_chat_id, message)
            alert_sent = True
            logging.info("Manual AQI query: %s", aqi)
        except Exception as exc:
            logging.error("Failed to send Telegram message: %s", exc)
            return {"success": False, "error": f"Failed to send message: {exc}", "alerts_sent": 0}
    elif aqi > config.aqi_threshold:
        # Scheduled run: only alert if above threshold
        message = format_alert_message(
            config.aqi_city,
            aqi,
            config.aqi_threshold,
            now,
            station_count,
            station_total,
        )
        try:
            send_telegram_alert(config.telegram_bot_token, config.telegram_chat_id, message)
            state.last_alert_time = now
            alert_sent = True
            logging.info("Alert sent for AQI %s", aqi)
        except Exception as exc:
            logging.error("Failed to send Telegram alert: %s", exc)
            return {"success": False, "error": f"Failed to send alert: {exc}", "alerts_sent": 0}
    else:
        logging.info("AQI %s is below or equal to threshold %s", aqi, config.aqi_threshold)

    state.last_aqi = aqi
    save_state(config.state_file, state)

    return {
        "success": True,
        "alerts_sent": 1 if alert_sent else 0,
        "message": message,
        "aqi": aqi,
    }


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if sys.version_info < (3, 12):
        logging.error("Python 3.12+ is required.")
        return 2

    try:
        load_env_file()
        config = load_config()
    except Exception as exc:
        logging.error("Configuration error: %s", exc)
        return 2

    startup_time = datetime.now(timezone.utc)

    try:
        asyncio.run(register_bot_commands(config.telegram_bot_token))
        logging.info("Registered bot commands with Telegram")
    except Exception as exc:
        logging.warning("Failed to register bot commands: %s", exc)

    state = load_state(config.state_file)
    state.last_aqi = None
    state.last_alert_time = None
    state.last_update_id = None
    try:
        save_state(config.state_file, state)
        logging.info("State reset on startup (AQI + Telegram offsets): %s", config.state_file)
    except Exception as exc:
        logging.warning("Failed to reset state file %s: %s", config.state_file, exc)

    exit_code = run_once(config, state, startup_time)
    save_state(config.state_file, state)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
