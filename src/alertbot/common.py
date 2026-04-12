"""Shared helpers for alertbot scripts."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests


# =============================================================================
# Paths
# =============================================================================

APP_NAME = "alertbot"


def _is_repo_root(path: Path) -> bool:
    if not (path / "pyproject.toml").exists():
        return False
    configs_dir = path / "configs"
    if not configs_dir.exists():
        return False
    return (configs_dir / "schedule.example.yaml").exists() or (configs_dir / "schedule.yaml").exists()


def _discover_repo_root() -> Optional[Path]:
    """Find a source checkout root when running from the repository."""
    module_root = Path(__file__).resolve().parents[2]
    cwd = Path.cwd().resolve()

    candidates = [cwd, *cwd.parents, module_root]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_repo_root(candidate):
            return candidate
    return None


def _resolve_paths() -> tuple[Path, Path, Path, Path]:
    """Resolve config/state/log locations for source and installed modes."""
    repo_root = _discover_repo_root()
    if repo_root is not None:
        return (
            repo_root,
            repo_root / "configs",
            repo_root / "state",
            repo_root / "logs",
        )

    config_home = Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    state_home = Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))).expanduser()

    config_dir = config_home / APP_NAME
    state_dir = state_home / APP_NAME
    logs_dir = state_dir / "logs"
    return (config_dir, config_dir, state_dir, logs_dir)


PROJECT_ROOT, CONFIG_DIR, STATE_DIR, LOGS_DIR = _resolve_paths()


# =============================================================================
# HTTP Configuration
# =============================================================================

DEFAULT_TIMEOUT = 20  # seconds
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.0  # seconds (doubles each retry)

# HTTP status codes that trigger retry with backoff
RATE_LIMIT_STATUS_CODES = {429, 503, 502, 504}


# =============================================================================
# Location Configuration
# =============================================================================

@dataclass
class LocationConfig:
    """Shared location configuration for all location-aware bots."""

    city: str
    display_name: str
    latitude: float
    longitude: float
    timezone: str
    country_code: Optional[str] = None

    @property
    def aqi_city(self) -> str:
        """City name formatted for AQI APIs (lowercase, no spaces)."""
        return self.city.lower().replace(" ", "")

    @property
    def aqi_map_url(self) -> str:
        """AQI map URL for the city."""
        return f"https://aqicn.org/map/{self.aqi_city}/"


# Default location: London, UK (fallback if state file doesn't exist)
DEFAULT_LOCATION = LocationConfig(
    city="london",
    display_name="London, United Kingdom",
    latitude=51.5074,
    longitude=-0.1278,
    timezone="Europe/London",
    country_code="GB",
)

DEFAULT_LOCATION_STATE_FILE = STATE_DIR / "location_state.json"


def load_location(state_file: Optional[Path] = None) -> LocationConfig:
    """Load location configuration from state file.

    Falls back to DEFAULT_LOCATION (London) if file doesn't exist.

    Args:
        state_file: Path to state file (default: state/location_state.json)

    Returns:
        LocationConfig with current location settings
    """
    path = state_file or DEFAULT_LOCATION_STATE_FILE

    if not path.exists():
        logging.debug("Location state file not found, using default: %s", DEFAULT_LOCATION.display_name)
        return DEFAULT_LOCATION

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logging.warning("Failed to read location state file %s: %s", path, exc)
        return DEFAULT_LOCATION

    return LocationConfig(
        city=data.get("city", DEFAULT_LOCATION.city),
        display_name=data.get("display_name", DEFAULT_LOCATION.display_name),
        latitude=data.get("latitude", DEFAULT_LOCATION.latitude),
        longitude=data.get("longitude", DEFAULT_LOCATION.longitude),
        timezone=data.get("timezone", DEFAULT_LOCATION.timezone),
        country_code=data.get("country_code", DEFAULT_LOCATION.country_code),
    )


def save_location(location: LocationConfig, state_file: Optional[Path] = None) -> None:
    """Save location configuration to state file.

    Args:
        location: LocationConfig to save
        state_file: Path to state file (default: state/location_state.json)
    """
    path = state_file or DEFAULT_LOCATION_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "city": location.city,
        "display_name": location.display_name,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "timezone": location.timezone,
        "country_code": location.country_code,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)
    logging.info("Location saved: %s", location.display_name)


def geocode_city(city_name: str) -> Optional[LocationConfig]:
    """Geocode a city name to get latitude, longitude, timezone, etc.

    Uses Open-Meteo Geocoding API (free, no key required).

    Args:
        city_name: City name to geocode (e.g., "London", "New York")

    Returns:
        LocationConfig if found, None otherwise
    """
    try:
        url = "https://geocoding-api.open-meteo.com/v1/search"
        params = {
            "name": city_name,
            "count": 1,
            "language": "en",
            "format": "json",
        }
        data = request_json(url, params=params, timeout=DEFAULT_TIMEOUT)
        results = data.get("results", [])
        if not results:
            logging.debug("No geocoding results for: %s", city_name)
            return None

        result = results[0]
        return LocationConfig(
            city=result.get("name", city_name).lower(),
            display_name=f"{result.get('name', city_name)}, {result.get('country', '')}".strip(", "),
            latitude=result.get("latitude", 0),
            longitude=result.get("longitude", 0),
            timezone=result.get("timezone", "UTC"),
            country_code=result.get("country_code"),
        )
    except Exception as exc:
        logging.warning("Geocoding failed for '%s': %s", city_name, exc)
        return None


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )


def _load_env_file_path(path: str) -> None:
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as exc:
        logging.warning("Failed to read env file %s: %s", path, exc)
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


def load_env_file(path: str = ".env") -> None:
    """Load environment variables from .env and optional local overlay.

    Existing environment variables are never overridden.
    When called with the default path, `.env.private` is loaded after `.env`
    as a local, gitignored overlay for private bots/secrets.
    """
    _load_env_file_path(path)
    if path == ".env":
        _load_env_file_path(".env.private")


def get_alert_transport_name() -> str:
    """Return the configured alert transport backend."""
    return (os.getenv("ALERTBOT_TRANSPORT", "telegram") or "telegram").strip().lower()


def resolve_alert_destination(destination_id: str | None = None) -> str:
    """Resolve a transport destination ID (Telegram chat ID / Matrix room ID).

    Args:
        destination_id: Optional explicit destination override passed by a bot.

    Returns:
        Destination ID string for the active transport.
    """
    if destination_id is not None and str(destination_id).strip():
        return str(destination_id).strip()

    transport_name = get_alert_transport_name()
    if transport_name == "matrix":
        return getenv_required("MATRIX_ROOM_ID")
    return getenv_required("TELEGRAM_CHAT_ID")


def get_telegram_compat_token() -> str:
    """Compatibility helper for bots that still store a Telegram token field.

    When the active transport is Telegram, returns the real TELEGRAM_BOT_TOKEN.
    For non-Telegram transports, returns a non-empty placeholder so legacy bot
    config loaders do not fail before the transport layer can handle delivery.
    """
    if get_alert_transport_name() == "telegram":
        return getenv_required("TELEGRAM_BOT_TOKEN")
    return os.getenv("TELEGRAM_BOT_TOKEN", "__transport_managed__") or "__transport_managed__"


def getenv_required(key: str) -> str:
    value = os.getenv(key)
    if not value:
        # Compatibility bridge for bots that still hard-code Telegram env keys.
        # Matrix sends are transport-managed, so map TELEGRAM_CHAT_ID to MATRIX_ROOM_ID
        # and provide a token placeholder to avoid unrelated config failures.
        transport_name = get_alert_transport_name()
        if transport_name == "matrix":
            if key == "TELEGRAM_CHAT_ID":
                value = os.getenv("MATRIX_ROOM_ID")
            elif key == "TELEGRAM_BOT_TOKEN":
                value = "__transport_managed__"
    if not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value


def getenv_required_any(keys: list[str]) -> str:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    raise ValueError(f"Missing required environment variable: {'/'.join(keys)}")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_utc(value: str) -> datetime | None:
    """Parse an ISO 8601 timestamp string to a UTC-aware datetime.

    Handles 'Z' suffix and numeric UTC offsets. Returns None on parse failure.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_json(path: Path, default: dict, strict: bool = False) -> dict:
    if not path.exists():
        if strict:
            raise ValueError(f"config file not found: {path}")
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        if strict:
            raise ValueError(
                f"invalid JSON in {path} at line {exc.lineno} column {exc.colno}: {exc.msg}"
            ) from exc
        return default
    except Exception as exc:
        if strict:
            raise ValueError(f"failed to read JSON from {path}: {exc}") from exc
        return default


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2))
    os.replace(tmp_path, path)


def _should_retry(status_code: int, attempt: int, max_retries: int) -> bool:
    """Check if request should be retried based on status code and attempt count."""
    if attempt >= max_retries:
        return False
    return status_code in RATE_LIMIT_STATUS_CODES


def _get_retry_delay(attempt: int, base_delay: float, resp: requests.Response | None = None) -> float:
    """Calculate retry delay with exponential backoff.

    Respects Retry-After header if present, otherwise uses exponential backoff.
    """
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return base_delay * (2 ** attempt)


def request_with_retry(
    method: str,
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    json_body: dict | None = None,
    data: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
) -> requests.Response:
    """Make HTTP request and return response with retry logic.

    Implements exponential backoff retry for rate limit errors (429, 502, 503, 504).

    Args:
        method: HTTP method (GET, POST, ...)
        url: URL to request
        params: Query parameters
        headers: HTTP headers
        json_body: JSON request payload
        data: Form-encoded request payload
        timeout: Request timeout in seconds
        max_retries: Maximum retry attempts for rate limit errors
        retry_base_delay: Base delay for exponential backoff (doubles each retry)

    Returns:
        `requests.Response` object

    Raises:
        RuntimeError: On request transport failures or timeout exhaustion
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(
                method=method,
                url=url,
                params=params,
                headers=headers,
                json=json_body,
                data=data,
                timeout=timeout,
            )

            if _should_retry(resp.status_code, attempt, max_retries):
                delay = _get_retry_delay(attempt, retry_base_delay, resp)
                logging.warning(
                    "Rate limited (HTTP %s) from %s %s, retrying in %.1fs (attempt %d/%d)",
                    resp.status_code, method, url, delay, attempt + 1, max_retries
                )
                time.sleep(delay)
                continue

            return resp

        except requests.exceptions.Timeout as exc:
            last_error = exc
            if attempt < max_retries:
                delay = _get_retry_delay(attempt, retry_base_delay)
                logging.warning(
                    "Request timeout for %s %s, retrying in %.1fs (attempt %d/%d)",
                    method, url, delay, attempt + 1, max_retries
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"Request timeout for {method} {url}") from exc

        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Request failed for {method} {url}: {exc}") from exc

    if last_error:
        raise RuntimeError(f"Max retries exceeded for {method} {url}") from last_error
    raise RuntimeError(f"Max retries exceeded for {method} {url}")


def request_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
) -> Any:
    """Make HTTP GET request and return JSON response."""
    resp = request_with_retry(
        method="GET",
        url=url,
        params=params,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {url}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from {url}: {exc}") from exc
    return payload


def request_json_post(
    url: str,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
) -> Any:
    """Make HTTP POST request with JSON body and return JSON response."""
    resp = request_with_retry(
        method="POST",
        url=url,
        headers=headers,
        json_body=json_body,
        timeout=timeout,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {url}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from {url}: {exc}") from exc
    return payload


def request_text(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
) -> str:
    """Make HTTP GET request and return response text."""
    resp = request_with_retry(
        method="GET",
        url=url,
        params=params,
        headers=headers,
        timeout=timeout,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {url}")
    return resp.text


def send_alert_message(
    message: str,
    destination_id: str | None = None,
    parse_mode: str | None = None,
) -> str:
    """Send an alert via the active transport using a transport-neutral API."""
    target = resolve_alert_destination(destination_id)
    from alertbot.transport_manager import send_alert

    result = send_alert(message, chat_id=target, parse_mode=parse_mode)
    if result is None:
        raise RuntimeError("Transport send failed (returned None)")
    return result


def send_telegram_alert(
    token: str,
    chat_id: str,
    message: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
) -> None:
    """Send message via Telegram Bot API.

    Uses the shared outbound messaging layer/transport manager when available.
    Falls back to direct Telegram HTTP only when the transport manager is unavailable
    and the active transport is Telegram.

    Args:
        token: Telegram bot token
        chat_id: Target chat ID
        message: Message text to send
        timeout: Request timeout in seconds
        max_retries: Maximum retry attempts for rate limit errors
        retry_base_delay: Base delay for exponential backoff

    Raises:
        RuntimeError: On API errors
    """
    transport_name = os.getenv("ALERTBOT_TRANSPORT", "telegram").lower()
    try:
        send_alert_message(message, destination_id=chat_id)
        return
    except ImportError:
        if transport_name != "telegram":
            raise RuntimeError("Transport manager unavailable for non-telegram transport")
        logging.debug("Transport manager not available, using direct HTTP")

    # Direct HTTP fallback (original implementation)
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                url, data={"chat_id": chat_id, "text": message}, timeout=timeout
            )

            if _should_retry(resp.status_code, attempt, max_retries):
                delay = _get_retry_delay(attempt, retry_base_delay, resp)
                logging.warning(
                    "Telegram rate limited (HTTP %s), retrying in %.1fs (attempt %d/%d)",
                    resp.status_code, delay, attempt + 1, max_retries
                )
                time.sleep(delay)
                continue

            if resp.status_code != 200:
                raise RuntimeError(f"Telegram API error: HTTP {resp.status_code}")

            try:
                payload = resp.json()
            except ValueError as exc:
                raise RuntimeError(f"Telegram API invalid JSON: {exc}") from exc

            if not payload.get("ok"):
                raise RuntimeError(f"Telegram API response not ok: {payload}")

            return  # Success

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                delay = _get_retry_delay(attempt, retry_base_delay)
                logging.warning(
                    "Telegram request timeout, retrying in %.1fs (attempt %d/%d)",
                    delay, attempt + 1, max_retries
                )
                time.sleep(delay)
                continue
            raise RuntimeError("Telegram request timeout")

        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Telegram request failed: {exc}") from exc

    raise RuntimeError("Max retries exceeded for Telegram")


# Schedule context helpers for controller-managed bots

ScheduleContext = dict[str, Any]


def get_interval_from_context(schedule_context: ScheduleContext | None) -> int | None:
    """Get interval_minutes from schedule context."""
    if schedule_context is None:
        return None
    return schedule_context.get("interval_minutes")


def get_last_run_from_context(schedule_context: ScheduleContext | None) -> str | None:
    """Get last_run ISO timestamp from schedule context."""
    if schedule_context is None:
        return None
    return schedule_context.get("last_run")


def calculate_lookback_minutes(
    schedule_context: ScheduleContext | None,
    default_minutes: int = 60,
    buffer_multiplier: float = 1.2,
    max_minutes: int | None = None,
) -> int:
    """Calculate lookback window in minutes from schedule context.

    When running on a schedule, the lookback should match the interval
    to avoid missing data or processing duplicates.

    Args:
        schedule_context: Context passed by controller for scheduled runs
        default_minutes: Default lookback for manual triggers
        buffer_multiplier: Multiply interval by this for safety margin
        max_minutes: Cap lookback at this value (None = no cap)

    Returns:
        Lookback window in minutes
    """
    if schedule_context is not None:
        interval = schedule_context.get("interval_minutes", default_minutes)
        lookback = int(interval * buffer_multiplier) + 1
    else:
        lookback = default_minutes

    if max_minutes is not None:
        lookback = min(lookback, max_minutes)

    return lookback


def is_manual_trigger(schedule_context: ScheduleContext | None) -> bool:
    """Check if this is a manual trigger (no schedule context)."""
    return schedule_context is None


def format_run_info(schedule_context: ScheduleContext | None) -> str:
    """Format schedule context for logging."""
    if schedule_context is None:
        return "manual trigger"
    bot = schedule_context.get("bot_name", "unknown")
    interval = schedule_context.get("interval_minutes", "?")
    return f"scheduled run (bot={bot}, interval={interval}m)"
