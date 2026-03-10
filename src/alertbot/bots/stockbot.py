import logging
import os
import sys
import time
from datetime import datetime, time as clock_time, timezone
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from alertbot.common import (
    DEFAULT_TIMEOUT,
    format_run_info,
    getenv_required,
    getenv_required_any,
    load_env_file,
    send_telegram_alert,
)

DEFAULT_PROVIDER = "finnhub"
DEFAULT_CURRENCY = "USD"
# Twelve Data docs/support: `/quote` costs 1 credit per symbol; Basic plan is 8
# credits/minute. Pace requests by credits to avoid frequent 429/rate-limit errors.
DEFAULT_TWELVEDATA_CREDITS_PER_MINUTE = 8
DEFAULT_TWELVEDATA_RATE_LIMIT_WAIT_SECONDS = 65
# Finnhub pricing page (free tier) advertises 60 API calls/minute.
DEFAULT_FINNHUB_CALLS_PER_MINUTE = 60
DEFAULT_FINNHUB_RATE_LIMIT_WAIT_SECONDS = 65
MAX_RATE_LIMIT_RETRIES_PER_BATCH = 2

PROVIDER_NAMES = {
    "finnhub": "Finnhub",
    "twelvedata": "Twelve Data",
}

NY_TZ = ZoneInfo("America/New_York")
NYSE_OPEN = clock_time(9, 30)
NYSE_CLOSE = clock_time(16, 0)


def resolve_stock_provider() -> str:
    provider = os.getenv("STOCK_PRICE_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDER_NAMES:
        raise ValueError(f"Unsupported provider: {provider}")
    return provider


def resolve_stock_api_key(provider: str) -> str:
    if provider == "finnhub":
        return getenv_required_any(["FINNHUB_API_KEY", "STOCK_PRICE_API_KEY"])
    if provider == "twelvedata":
        return getenv_required("STOCK_PRICE_API_KEY")
    raise ValueError(f"Unsupported provider: {provider}")


def _is_twelvedata_rate_limited(
    status_code: int | None,
    payload: Any | None = None,
) -> bool:
    if status_code in {429, 502, 503, 504}:
        return True
    if not isinstance(payload, dict):
        return False

    code = str(payload.get("code", "")).strip().lower()
    message = str(payload.get("message", "")).strip().lower()
    status = str(payload.get("status", "")).strip().lower()

    if code in {"429", "rate_limit_exceeded"}:
        return True
    if "too many requests" in message or "rate limit" in message:
        return True
    if "api credits" in message or "quota" in message:
        return True
    if status == "error" and "limit" in message:
        return True
    return False


def _twelvedata_wait_seconds(resp: requests.Response | None = None) -> int:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1, int(float(retry_after)))
            except (TypeError, ValueError):
                pass
    return DEFAULT_TWELVEDATA_RATE_LIMIT_WAIT_SECONDS


def parse_ticker_list(raw: str) -> List[str]:
    if not raw.strip():
        return []
    items = [item.strip().upper() for item in raw.split(",")]
    return [item for item in items if item]


def resolve_tickers() -> List[str]:
    raw = getenv_required("STOCK_TICKERS")
    parsed = parse_ticker_list(raw)
    if not parsed:
        raise ValueError("STOCK_TICKERS must include at least one non-empty ticker")
    return parsed


def is_nyse_regular_hours(now_utc: datetime | None = None) -> bool:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    ny_now = now_utc.astimezone(NY_TZ)
    if ny_now.weekday() >= 5:
        return False
    ny_time = ny_now.time()
    return NYSE_OPEN <= ny_time < NYSE_CLOSE


def fetch_twelvedata_nyse_market_state(
    session: requests.Session,
    api_key: str,
) -> Tuple[Optional[bool], Optional[str]]:
    url = "https://api.twelvedata.com/market_state"
    try:
        resp = session.get(url, params={"exchange": "NYSE", "apikey": api_key}, timeout=DEFAULT_TIMEOUT)
    except Exception:
        return None, None

    if resp.status_code != 200:
        return None, None

    try:
        payload = resp.json()
    except Exception:
        return None, None

    states = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    if not states:
        return None, None

    nyse_states = []
    for state in states:
        if not isinstance(state, dict):
            continue
        code = str(state.get("code", "")).upper()
        name = str(state.get("name", "")).upper()
        if code in {"XNYS", "XASE", "ARCX"} or name == "NYSE":
            nyse_states.append(state)
    if not nyse_states:
        return None, None

    is_open_values = [bool(state.get("is_market_open")) for state in nyse_states if "is_market_open" in state]
    if not is_open_values:
        return None, None

    time_to_open = None
    for state in nyse_states:
        raw_value = state.get("time_to_open")
        if isinstance(raw_value, str) and raw_value.strip():
            time_to_open = raw_value.strip()
            break

    return any(is_open_values), time_to_open


def _parse_twelvedata_quote_entry(
    payload: Any,
    status_code: int | None = None,
) -> Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    if status_code is not None and status_code != 200:
        error_type = (
            "rate_limited" if _is_twelvedata_rate_limited(status_code, payload) else "fetch_failed"
        )
        return None, None, None, error_type

    if not isinstance(payload, dict):
        return None, None, None, "fetch_failed"
    if payload.get("status") == "error":
        error_type = "rate_limited" if _is_twelvedata_rate_limited(status_code, payload) else "fetch_failed"
        return None, None, None, error_type

    # Some error payloads include code/message without an explicit `status=error`.
    if "code" in payload and "price" not in payload and "close" not in payload and "previous_close" not in payload:
        error_type = "rate_limited" if _is_twelvedata_rate_limited(status_code, payload) else "fetch_failed"
        return None, None, None, error_type

    price_raw = payload.get("price")
    if price_raw is None:
        price_raw = payload.get("close")
    if price_raw is None:
        price_raw = payload.get("previous_close")
    if price_raw is None:
        return None, None, None, "fetch_failed"
    try:
        price_value = float(price_raw)
    except (TypeError, ValueError):
        return None, None, None, "fetch_failed"

    currency = payload.get("currency")
    percent_change_raw = payload.get("percent_change")
    percent_change = None
    if percent_change_raw is not None:
        try:
            percent_change = float(percent_change_raw)
        except (TypeError, ValueError):
            percent_change = None
    return price_value, currency, percent_change, None


def _extract_twelvedata_quote_entries(payload: Any, requested_symbols: List[str]) -> dict[str, Any]:
    """Normalize `/quote` response shapes to {requested_symbol: entry_payload}."""
    requested_upper = {symbol.upper(): symbol for symbol in requested_symbols}
    results: dict[str, Any] = {}

    if isinstance(payload, dict):
        # Single-symbol quote shape.
        symbol_value = payload.get("symbol")
        if isinstance(symbol_value, str) and symbol_value.strip():
            symbol_key = symbol_value.strip().upper()
            if symbol_key in requested_upper:
                results[requested_upper[symbol_key]] = payload
                return results

        # Some endpoints use {"data": [...]}; support it defensively.
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                symbol_value = item.get("symbol")
                if not isinstance(symbol_value, str):
                    continue
                symbol_key = symbol_value.strip().upper()
                if symbol_key in requested_upper:
                    results[requested_upper[symbol_key]] = item
            if results:
                return results

        # Multi-symbol `/quote` commonly returns a top-level symbol->payload map.
        normalized_map = {
            str(key).strip().upper(): value
            for key, value in payload.items()
            if isinstance(key, str)
        }
        for symbol in requested_symbols:
            value = normalized_map.get(symbol.upper())
            if value is not None:
                results[symbol] = value
        if results:
            return results

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol_value = item.get("symbol")
            if not isinstance(symbol_value, str):
                continue
            symbol_key = symbol_value.strip().upper()
            if symbol_key in requested_upper:
                results[requested_upper[symbol_key]] = item
    return results


def _fetch_twelvedata_quote_batch_once(
    session: requests.Session,
    symbols: List[str],
    api_key: str,
) -> Tuple[
    dict[str, Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]],
    requests.Response | None,
]:
    url = "https://api.twelvedata.com/quote"
    results: dict[str, Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]] = {}

    try:
        resp = session.get(
            url,
            params={"symbol": ",".join(symbols), "apikey": api_key},
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception:
        for symbol in symbols:
            results[symbol] = (None, None, None, "fetch_failed")
        return results, None

    payload: Any | None = None
    try:
        payload = resp.json()
    except Exception:
        payload = None

    # Whole-request failure (HTTP or top-level API error).
    if resp.status_code != 200 or (isinstance(payload, dict) and payload.get("status") == "error"):
        parsed_error = _parse_twelvedata_quote_entry(payload, resp.status_code)
        for symbol in symbols:
            results[symbol] = parsed_error
        return results, resp

    entries = _extract_twelvedata_quote_entries(payload, symbols)
    for symbol in symbols:
        entry = entries.get(symbol)
        if entry is None:
            # Missing entries can happen in partial responses when quota/rate limits are hit.
            error_type = "rate_limited" if _is_twelvedata_rate_limited(resp.status_code, payload) else "fetch_failed"
            results[symbol] = (None, None, None, error_type)
            continue
        results[symbol] = _parse_twelvedata_quote_entry(entry, resp.status_code)
    return results, resp


def fetch_twelvedata_quote(
    session: requests.Session, symbol: str, api_key: str
) -> Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    results, _ = _fetch_twelvedata_quote_batch_once(session, [symbol], api_key)
    return results.get(symbol, (None, None, None, "fetch_failed"))


def fetch_twelvedata_quotes_chunk(
    session: requests.Session,
    symbols: List[str],
    api_key: str,
) -> dict[str, Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]]:
    """Fetch a chunk of quotes with rate-limit retry and partial-result salvage."""
    if not symbols:
        return {}

    pending = list(symbols)
    results: dict[str, Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]] = {}
    rate_limit_retries = 0

    while pending:
        batch_results, resp = _fetch_twelvedata_quote_batch_once(session, pending, api_key)

        retry_symbols: List[str] = []
        fetch_failed_symbols: List[str] = []
        for symbol in pending:
            result = batch_results.get(symbol, (None, None, None, "fetch_failed"))
            _, _, _, error_type = result
            if error_type == "rate_limited":
                retry_symbols.append(symbol)
                continue
            if error_type == "fetch_failed":
                fetch_failed_symbols.append(symbol)
            results[symbol] = result

        # If a batched request has some non-rate-limit failures, retry those symbols
        # individually so a malformed/partial batch response doesn't lose prices.
        if fetch_failed_symbols and len(pending) > 1:
            logging.warning(
                "[stockbot] Retrying %d fetch-failed ticker(s) individually after batch response",
                len(fetch_failed_symbols),
            )
            for symbol in fetch_failed_symbols:
                results[symbol] = fetch_twelvedata_quote(session, symbol, api_key)

        if not retry_symbols:
            break

        if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES_PER_BATCH:
            for symbol in retry_symbols:
                results[symbol] = batch_results.get(symbol, (None, None, None, "rate_limited"))
            break

        wait_seconds = _twelvedata_wait_seconds(resp)
        logging.warning(
            "[stockbot] Twelve Data rate limit hit for %d ticker(s); retrying in %ss",
            len(retry_symbols),
            wait_seconds,
        )
        time.sleep(wait_seconds)
        pending = retry_symbols
        rate_limit_retries += 1

    return results


def _is_finnhub_rate_limited(
    status_code: int | None,
    payload: Any | None = None,
) -> bool:
    if status_code in {429, 502, 503, 504}:
        return True
    if not isinstance(payload, dict):
        return False

    message = str(payload.get("error", "")).strip().lower()
    if not message:
        message = str(payload.get("message", "")).strip().lower()
    if "too many requests" in message or "rate limit" in message:
        return True
    if "api limit" in message or "quota" in message:
        return True
    return False


def _finnhub_wait_seconds(resp: requests.Response | None = None) -> int:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1, int(float(retry_after)))
            except (TypeError, ValueError):
                pass

        reset_at = resp.headers.get("X-Ratelimit-Reset")
        if reset_at:
            try:
                reset_seconds = int(float(reset_at)) - int(time.time()) + 1
                if reset_seconds > 0:
                    return reset_seconds
            except (TypeError, ValueError):
                pass
    return DEFAULT_FINNHUB_RATE_LIMIT_WAIT_SECONDS


def _parse_finnhub_quote_entry(
    payload: Any,
    status_code: int | None = None,
) -> Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    if status_code is not None and status_code != 200:
        error_type = "rate_limited" if _is_finnhub_rate_limited(status_code, payload) else "fetch_failed"
        return None, None, None, error_type

    if not isinstance(payload, dict):
        return None, None, None, "fetch_failed"
    if "error" in payload and "c" not in payload:
        error_type = "rate_limited" if _is_finnhub_rate_limited(status_code, payload) else "fetch_failed"
        return None, None, None, error_type

    current_raw = payload.get("c")
    previous_close_raw = payload.get("pc")
    try:
        current_price = float(current_raw) if current_raw is not None else None
    except (TypeError, ValueError):
        current_price = None
    try:
        previous_close = float(previous_close_raw) if previous_close_raw is not None else None
    except (TypeError, ValueError):
        previous_close = None

    if current_price is None or current_price <= 0:
        if previous_close is not None and previous_close > 0:
            current_price = previous_close
        else:
            return None, None, None, "fetch_failed"

    percent_change_raw = payload.get("dp")
    percent_change = None
    if percent_change_raw is not None:
        try:
            percent_change = float(percent_change_raw)
        except (TypeError, ValueError):
            percent_change = None
    elif previous_close not in (None, 0):
        percent_change = ((current_price - previous_close) / previous_close) * 100.0

    return current_price, None, percent_change, None


def _fetch_finnhub_quote_once(
    session: requests.Session,
    symbol: str,
    api_key: str,
) -> Tuple[Tuple[Optional[float], Optional[str], Optional[float], Optional[str]], requests.Response | None]:
    url = "https://finnhub.io/api/v1/quote"
    try:
        resp = session.get(url, params={"symbol": symbol, "token": api_key}, timeout=DEFAULT_TIMEOUT)
    except Exception:
        return (None, None, None, "fetch_failed"), None

    payload: Any | None = None
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return _parse_finnhub_quote_entry(payload, resp.status_code), resp


def fetch_finnhub_quote(
    session: requests.Session,
    symbol: str,
    api_key: str,
) -> Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    result, _ = _fetch_finnhub_quote_once(session, symbol, api_key)
    return result


def fetch_finnhub_quotes_chunk(
    session: requests.Session,
    symbols: List[str],
    api_key: str,
) -> dict[str, Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]]:
    """Fetch a chunk of Finnhub quotes with retry when minute rate limits are hit."""
    if not symbols:
        return {}

    pending = list(symbols)
    results: dict[str, Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]] = {}
    rate_limit_retries = 0

    while pending:
        retry_symbols: List[str] = []
        rate_limit_resp: requests.Response | None = None
        for index, symbol in enumerate(pending):
            result, resp = _fetch_finnhub_quote_once(session, symbol, api_key)
            _, _, _, error_type = result
            if error_type == "rate_limited":
                retry_symbols = pending[index:]
                rate_limit_resp = resp
                break
            results[symbol] = result

        if not retry_symbols:
            break

        if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES_PER_BATCH:
            for symbol in retry_symbols:
                results[symbol] = (None, None, None, "rate_limited")
            break

        wait_seconds = _finnhub_wait_seconds(rate_limit_resp)
        logging.warning(
            "[stockbot] Finnhub rate limit hit for %d ticker(s); retrying in %ss",
            len(retry_symbols),
            wait_seconds,
        )
        time.sleep(wait_seconds)
        pending = retry_symbols
        rate_limit_retries += 1

    return results


def get_quote_chunk_settings(provider: str) -> Tuple[int, int]:
    if provider == "finnhub":
        return max(1, DEFAULT_FINNHUB_CALLS_PER_MINUTE), DEFAULT_FINNHUB_RATE_LIMIT_WAIT_SECONDS
    if provider == "twelvedata":
        return max(1, DEFAULT_TWELVEDATA_CREDITS_PER_MINUTE), DEFAULT_TWELVEDATA_RATE_LIMIT_WAIT_SECONDS
    raise ValueError(f"Unsupported provider: {provider}")


def fetch_quotes_chunk(
    provider: str,
    session: requests.Session,
    symbols: List[str],
    api_key: str,
) -> dict[str, Tuple[Optional[float], Optional[str], Optional[float], Optional[str]]]:
    if provider == "finnhub":
        return fetch_finnhub_quotes_chunk(session, symbols, api_key)
    if provider == "twelvedata":
        return fetch_twelvedata_quotes_chunk(session, symbols, api_key)
    raise ValueError(f"Unsupported provider: {provider}")


def _chunk_symbols(symbols: List[str], chunk_size: int) -> List[List[str]]:
    return [symbols[i : i + chunk_size] for i in range(0, len(symbols), chunk_size)]


def build_message(
    rows: List[Tuple[str, Optional[float], Optional[str], Optional[float], Optional[str]]],
    default_currency: str,
) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Stock Prices ({timestamp})"]
    sortable_rows = list(rows)
    sortable_rows.sort(
        key=lambda item: (
            item[3] is not None,
            item[3] if item[3] is not None else float("-inf"),
        ),
        reverse=True,
    )

    rate_limited_tickers: List[str] = []
    for ticker, price_value, currency, percent_change, error_type in sortable_rows:
        if price_value is None:
            if error_type == "rate_limited":
                rate_limited_tickers.append(ticker)
                continue
            lines.append(f"{ticker}: ERROR (fetch failed)")
            continue
        price_text = f"{price_value:.2f}"
        currency_text = (currency or default_currency).upper()
        if percent_change is None:
            lines.append(f"{ticker}: {price_text} {currency_text}")
        else:
            lines.append(f"{ticker}: {price_text} {currency_text} ({percent_change:.2f}%)")

    if rate_limited_tickers:
        sample = ", ".join(rate_limited_tickers[:5])
        suffix = "..." if len(rate_limited_tickers) > 5 else ""
        lines.append(
            f"Rate limit reached for {len(rate_limited_tickers)} ticker(s): {sample}{suffix}. "
            "Quotes were skipped."
        )

    return "\n".join(lines)


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run stock price check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running stockbot: %s", format_run_info(schedule_context))

    try:
        telegram_token = getenv_required("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    try:
        provider = resolve_stock_provider()
        api_key = resolve_stock_api_key(provider)
        tickers = resolve_tickers()
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    now_utc = datetime.now(timezone.utc)
    provider_name = PROVIDER_NAMES.get(provider, provider)

    # Use local NYSE-hours gating so scheduled checks avoid off-hours requests.
    market_open = is_nyse_regular_hours(now_utc)
    if not market_open and not manual_trigger:
        logging.info(
            "Skipping stock update outside NYSE trading session: %s",
            now_utc.astimezone(NY_TZ).isoformat(),
        )
        return {
            "success": True,
            "message": "Skipped outside NYSE trading session",
            "alerts_sent": 0,
        }

    default_currency = os.getenv("STOCK_CURRENCY", DEFAULT_CURRENCY).strip().upper() or DEFAULT_CURRENCY
    quote_chunk_size, quote_chunk_wait_seconds = get_quote_chunk_settings(provider)

    rows: List[Tuple[str, Optional[float], Optional[str], Optional[float], Optional[str]]] = []
    with requests.Session() as session:
        for chunk_index, chunk in enumerate(_chunk_symbols(tickers, quote_chunk_size)):
            if chunk_index > 0:
                logging.info(
                    "[stockbot] Waiting %ss before next quote chunk to respect %s minute limits",
                    quote_chunk_wait_seconds,
                    provider_name,
                )
                time.sleep(quote_chunk_wait_seconds)

            chunk_results = fetch_quotes_chunk(provider, session, chunk, api_key)
            for ticker in chunk:
                price_value, currency, percent_change, error_type = chunk_results.get(
                    ticker, (None, None, None, "fetch_failed")
                )
                rows.append((ticker, price_value, currency, percent_change, error_type))

    rate_limited_count = sum(1 for _, _, _, _, error_type in rows if error_type == "rate_limited")
    if rate_limited_count:
        logging.warning(
            "[stockbot] %s rate limit reached for %d/%d ticker(s)",
            provider_name,
            rate_limited_count,
            len(tickers),
        )

    message = build_message(rows, default_currency)
    if not market_open:
        ny_now = now_utc.astimezone(NY_TZ)
        message += (
            "\n\nNote: NYSE is currently closed "
            f"({ny_now.strftime('%Y-%m-%d %H:%M %Z')})."
        )

    try:
        send_telegram_alert(telegram_token, telegram_chat_id, message)
        return {
            "success": True,
            "message": message,
            "alerts_sent": 1,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
