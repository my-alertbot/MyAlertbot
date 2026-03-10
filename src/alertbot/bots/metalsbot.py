#!/usr/bin/env python3
"""Precious metals price bot - fetches gold and silver prices."""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from alertbot.common import (
    calculate_lookback_minutes,
    format_run_info,
    getenv_required,
    load_env_file,
    parse_iso_utc,
    request_json,
    send_telegram_alert,
    setup_logging,
)

# Metal price API endpoints
METALAPI_BASE_URL = "https://metalapi.com/api/v1/latest"
METALS_API_BASE_URL = "https://metals-api.com/api/latest"
GOLD_API_BASE_URL = "https://api.gold-api.com/price"

# Metals to fetch: symbol -> display name
METALS = {
    "XAU": "Gold",
    "XAG": "Silver",
}


def parse_api_timestamp(payload: dict[str, Any]) -> datetime | None:
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    if isinstance(timestamp, str) and timestamp.isdigit():
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)

    date_value = payload.get("date")
    if isinstance(date_value, str) and date_value:
        try:
            return datetime.fromisoformat(date_value).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def fetch_metal_rates(
    currency: str,
    api_key: str,
    provider: str,
    max_age_hours: int | None,
) -> dict[str, float] | None:
    """Fetch metal rates from MetalAPI.

    Args:
        currency: Currency code (USD, EUR, etc.)
        api_key: MetalAPI access token

    Returns:
        rates dict or None on error
    """
    provider = provider.lower()
    if provider == "metals-api":
        url = METALS_API_BASE_URL
        params = {
            "access_key": api_key,
            "base": currency,
            "symbols": ",".join(METALS.keys()),
        }
    else:
        url = METALAPI_BASE_URL
        params = {
            "api_key": api_key,
            "base": currency,
            "currencies": ",".join(METALS.keys()),
        }

    try:
        data = request_json(url, params=params)
    except Exception as exc:
        logging.warning("Failed to fetch metal rates (%s): %s", provider, exc)
        return None

    if not isinstance(data, dict):
        logging.warning("Metal API (%s) returned non-object JSON", provider)
        return None

    if data.get("success") is False:
        logging.warning("Metal API (%s) error: %s", provider, data.get("error"))
        return None

    rates = data.get("rates")
    if not isinstance(rates, dict):
        logging.warning("Metal API (%s) response missing rates: %s", provider, data)
        return None

    if max_age_hours is not None:
        data_ts = parse_api_timestamp(data)
        if data_ts is None:
            logging.warning("Metal API (%s) response missing timestamp/date", provider)
        else:
            age = datetime.now(timezone.utc) - data_ts
            if age > timedelta(hours=max_age_hours):
                logging.warning(
                    "Metal API (%s) data too old (%s > %sh)",
                    provider,
                    age,
                    max_age_hours,
                )
                return None

    return rates


def fetch_gold_api_prices(
    currency: str,
    max_age_hours: int | None,
) -> dict[str, float] | None:
    if currency.upper() != "USD":
        logging.warning("gold-api only supports USD pricing (requested %s)", currency)
        return None

    prices: dict[str, float] = {}
    now = datetime.now(timezone.utc)

    for symbol in METALS:
        url = f"{GOLD_API_BASE_URL}/{symbol}"
        try:
            data = request_json(url)
        except Exception as exc:
            logging.warning("Failed to fetch %s from gold-api: %s", symbol, exc)
            return None

        if not isinstance(data, dict):
            logging.warning("gold-api returned non-object JSON for %s: %s", symbol, data)
            return None

        price = data.get("price")
        try:
            prices[symbol] = float(price)
        except (TypeError, ValueError):
            logging.warning("gold-api response missing valid price for %s: %s", symbol, data)
            return None

        if max_age_hours is not None:
            updated = parse_iso_utc(data.get("updatedAt") or "")
            if updated is None:
                logging.warning("gold-api response missing updatedAt for %s", symbol)
            else:
                age = now - updated
                if age > timedelta(hours=max_age_hours):
                    logging.warning(
                        "gold-api data too old for %s (%s > %sh)",
                        symbol,
                        age,
                        max_age_hours,
                    )
                    return None

    return prices


def price_from_rates(rates: dict[str, float], currency: str, symbol: str) -> float | None:
    direct_key = f"{currency}{symbol}"
    direct_rate = rates.get(direct_key)
    if direct_rate is not None:
        try:
            direct_value = float(direct_rate)
        except (TypeError, ValueError):
            direct_value = None
        if direct_value is not None and direct_value > 0:
            # Most provider payloads expose CUR+METAL as "price of 1 metal unit in currency".
            return direct_value

    rate = rates.get(symbol)
    if rate is None:
        return None
    try:
        rate_value = float(rate)
    except (TypeError, ValueError):
        return None
    if rate_value <= 0:
        return None
    if rate_value < 1:
        return 1 / rate_value
    return rate_value


def format_price_message(prices: dict[str, float | None], currency: str) -> str:
    """Format prices into a Telegram message."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"💰 Precious Metals Prices ({timestamp})", ""]

    currency_symbol = "$" if currency.upper() == "USD" else f"{currency.upper()} "

    for symbol, price in prices.items():
        name = METALS.get(symbol, symbol)
        if price is None:
            lines.append(f"{name}: Error fetching price")
            continue

        price_str = f"{currency_symbol}{price:,.2f}/oz"
        lines.append(f"{name}: {price_str}")

    return "\n".join(lines)


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run metals price check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running metalsbot: %s", format_run_info(schedule_context))

    provider = os.getenv("METALS_API_PROVIDER", "gold-api").lower()
    currency = os.getenv("METALS_CURRENCY", "USD").upper()
    max_age_minutes = calculate_lookback_minutes(
        schedule_context,
        default_minutes=12 * 60,
        buffer_multiplier=1.2,
    )
    max_age_hours = max(1, (max_age_minutes + 59) // 60)

    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    prices: dict[str, float | None] = {}
    if provider == "gold-api":
        fetched_prices = fetch_gold_api_prices(currency, max_age_hours)
        if fetched_prices is None:
            error_msg = "Failed to fetch metal prices from gold-api"
            logging.error(error_msg)
            return {"success": False, "error": error_msg, "alerts_sent": 0}
        prices = {symbol: fetched_prices.get(symbol) for symbol in METALS}
    elif provider in {"metalapi", "metals-api"}:
        api_key_var = "METALAPI_KEY" if provider == "metalapi" else "METALS_API_KEY"
        try:
            api_key = getenv_required(api_key_var)
        except ValueError:
            # Backward compatibility for older env setups
            if provider == "metals-api":
                try:
                    api_key = getenv_required("METALAPI_KEY")
                except ValueError as exc:
                    return {"success": False, "error": str(exc), "alerts_sent": 0}
            else:
                return {
                    "success": False,
                    "error": f"Missing required environment variable: {api_key_var}",
                    "alerts_sent": 0,
                }

        rates = fetch_metal_rates(currency, api_key, provider, max_age_hours)
        if rates is None:
            error_msg = f"Failed to fetch metal rates from {provider}"
            logging.error(error_msg)
            return {"success": False, "error": error_msg, "alerts_sent": 0}
        for symbol in METALS:
            prices[symbol] = price_from_rates(rates, currency, symbol)
    else:
        return {
            "success": False,
            "error": f"Unsupported metals provider: {provider}",
            "alerts_sent": 0,
        }

    # Check if we got any valid prices
    valid_prices = [p for p in prices.values() if p is not None]
    if not valid_prices:
        error_msg = "Failed to fetch any metal prices"
        logging.error(error_msg)
        return {"success": False, "error": error_msg, "alerts_sent": 0}

    message = format_price_message(prices, currency)

    try:
        send_telegram_alert(tg_token, tg_chat_id, message)
        return {
            "success": True,
            "message": message,
            "alerts_sent": 1,
        }
    except Exception as exc:
        logging.error("Failed to send Telegram message: %s", exc)
        return {"success": False, "error": str(exc), "alerts_sent": 0}


def main() -> int:
    """Main entry point."""
    setup_logging()
    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
