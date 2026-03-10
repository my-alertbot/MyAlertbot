#!/usr/bin/env python3
"""Crypto price alert bot using DefiLlama price data and Telegram."""

# Config setup:
# - Create a JSON config file with currency and rules.
# - Point CRYPTOBOT_CONFIG to that file (default: configs/cryptobot.config).
# - Each rule requires: id (token id in DefiLlama's `coingecko:<id>` namespace), direction ("above"/"below"), price.
# - Optional: CRYPTOBOT_STATE for state path.
# NOTE: interval_sec is no longer read from config - use configs/schedule.yaml to control frequency.

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from alertbot.common import (
    CONFIG_DIR,
    STATE_DIR,
    format_run_info,
    getenv_required,
    iso_now,
    load_env_file,
    load_json,
    request_json,
    save_json,
    send_telegram_alert,
    setup_logging,
)


DEFILLAMA_CURRENT_URL = "https://coins.llama.fi/prices/current"
DEFILLAMA_HISTORICAL_URL = "https://coins.llama.fi/prices/historical"
DEFILLAMA_ID_NAMESPACE = "coingecko"
DEFILLAMA_SEARCH_WIDTH = "4h"
SECONDS_PER_DAY = 86400


def _defillama_key(token_id: str) -> str:
    return f"{DEFILLAMA_ID_NAMESPACE}:{token_id}"


def _defillama_parse_coins(payload: Any, endpoint_name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"DefiLlama {endpoint_name} returned non-object JSON")
    coins = payload.get("coins")
    if not isinstance(coins, dict):
        raise RuntimeError(f"DefiLlama {endpoint_name} response missing 'coins' object")
    return coins


def _defillama_fetch_current(ids: list[str]) -> dict[str, dict[str, Any]]:
    coin_keys = ",".join(_defillama_key(token_id) for token_id in ids)
    payload = request_json(
        f"{DEFILLAMA_CURRENT_URL}/{coin_keys}",
        params={"searchWidth": DEFILLAMA_SEARCH_WIDTH},
    )
    return _defillama_parse_coins(payload, "current prices")


def _defillama_fetch_historical(ids: list[str], timestamp: int) -> dict[str, dict[str, Any]]:
    coin_keys = ",".join(_defillama_key(token_id) for token_id in ids)
    payload = request_json(
        f"{DEFILLAMA_HISTORICAL_URL}/{timestamp}/{coin_keys}",
        params={"searchWidth": DEFILLAMA_SEARCH_WIDTH},
    )
    return _defillama_parse_coins(payload, "historical prices")


def _compute_percent_change(current_price: float, previous_price: float | None) -> float | None:
    if previous_price is None or previous_price == 0:
        return None
    return ((current_price - previous_price) / previous_price) * 100.0


def fetch_prices(ids: list[str], currency: str, include_24h_change: bool = True) -> dict[str, dict[str, float]]:
    currency = currency.lower()
    if currency != "usd":
        raise ValueError("cryptobot with DefiLlama currently supports only usd currency")
    if not ids:
        return {}

    current_coins = _defillama_fetch_current(ids)
    historical_coins: dict[str, dict[str, Any]] = {}
    if include_24h_change:
        historical_ts = int(time.time()) - SECONDS_PER_DAY
        try:
            historical_coins = _defillama_fetch_historical(ids, historical_ts)
        except Exception as exc:
            logging.warning("[cryptobot] unable to fetch 24h historical prices: %s", exc)

    prices: dict[str, dict[str, float]] = {}
    change_key = f"{currency}_24h_change"
    for token_id in ids:
        key = _defillama_key(token_id)
        price_info = current_coins.get(key, {})
        raw_current = price_info.get("price")

        normalized: dict[str, float] = {}
        current_price: float | None = None
        try:
            if raw_current is not None:
                current_price = float(raw_current)
                normalized[currency] = current_price
        except (TypeError, ValueError):
            current_price = None

        if current_price is not None and include_24h_change:
            historical_info = historical_coins.get(key, {})
            raw_previous = historical_info.get("price")
            previous_price: float | None = None
            try:
                if raw_previous is not None:
                    previous_price = float(raw_previous)
            except (TypeError, ValueError):
                previous_price = None
            change = _compute_percent_change(current_price, previous_price)
            if change is not None:
                normalized[change_key] = change

        prices[token_id] = normalized

    return prices


def normalize_rules(rules: list[dict], currency: str) -> list[dict]:
    norm = []
    for rule in rules:
        token_id = rule.get("id")
        direction = rule.get("direction")
        price = rule.get("price")
        if not token_id or direction not in ("above", "below") or price is None:
            raise ValueError("invalid rule: require id, direction, price")
        rule_currency = str(rule.get("currency", currency)).lower()
        norm.append(
            {
                "id": token_id,
                "direction": direction,
                "price": float(price),
                "currency": rule_currency,
            }
        )
    return norm


def should_trigger(direction: str, last_price: float | None, price: float, threshold: float) -> bool:
    if last_price is None:
        return False
    if direction == "above":
        return last_price <= threshold and price > threshold
    return last_price >= threshold and price < threshold


def format_alert(token_id: str, direction: str, threshold: float, price: float, currency: str) -> str:
    symbol = "$" if currency.lower() == "usd" else f"{currency.upper()} "
    return (
        f"{token_id} crossed {direction} {symbol}{threshold:,.6g}\n"
        f"Current: {symbol}{price:,.6g}\n"
        f"Time: {iso_now()}"
    )


def format_manual_prices(ids: list[str], prices: dict, currency: str) -> str:
    symbol = "$" if currency.lower() == "usd" else f"{currency.upper()} "
    change_key = f"{currency}_24h_change"
    lines = ["Crypto Prices"]
    for token_id in ids:
        price_info = prices.get(token_id, {})
        price = price_info.get(currency)
        if price is None:
            lines.append(f"{token_id}: ERROR (price unavailable)")
            continue
        change_raw = price_info.get(change_key)
        try:
            change_value = float(change_raw)
            change_text = f"{change_value:+.1f}%"
        except (TypeError, ValueError):
            change_text = "n/a"
        lines.append(f"{token_id}: {symbol}{float(price):,.6g} ({change_text})")
    return "\n".join(lines)


def poll(
    rules: list[dict],
    currency: str,
    tg_token: str,
    tg_chat_id: str,
    state_path: Path,
    manual_trigger: bool = False,
) -> dict[str, Any]:
    """Run crypto price check and send alerts.

    Returns:
        dict with success status, alerts_sent count, and message
    """
    state = load_json(state_path, {"last_prices": {}})
    last_prices = state.get("last_prices", {})
    # Snapshot for rule evaluation - prevents in-loop updates from affecting other rules
    last_prices_snapshot = dict(last_prices)

    alerts_sent = 0

    ids = sorted({r["id"] for r in rules})
    try:
        prices = fetch_prices(ids, currency, include_24h_change=manual_trigger)
    except Exception as e:
        logging.error("[cryptobot] fetch error: %s", e)
        return {"success": False, "error": str(e), "alerts_sent": 0}

    manual_message = format_manual_prices(ids, prices, currency) if manual_trigger else None

    for rule in rules:
        token_id = rule["id"]
        rule_currency = rule["currency"]
        price_info = prices.get(token_id, {})
        price = price_info.get(rule_currency)
        if price is None:
            logging.warning(
                "[cryptobot] %s missing price for %s", token_id, rule_currency
            )
            continue
        last_price = last_prices_snapshot.get(token_id)
        relation = "above" if price > rule["price"] else "below"
        will_alert = should_trigger(rule["direction"], last_price, price, rule["price"])

        alert_flag = " alert" if will_alert else ""
        logging.info(
            "[cryptobot] %s %s=%.6g (%s trigger %s %.6g)%s",
            token_id,
            rule_currency,
            price,
            relation,
            rule["direction"],
            rule["price"],
            alert_flag,
        )
        if will_alert and not manual_trigger:
            text = format_alert(token_id, rule["direction"], rule["price"], price, rule_currency)
            try:
                send_telegram_alert(tg_token, tg_chat_id, text)
                alerts_sent += 1
            except Exception as exc:
                logging.warning("[cryptobot] telegram send failed: %s", exc)
            else:
                logging.info(
                    "[cryptobot] alert sent for %s %s %.6g",
                    token_id,
                    rule["direction"],
                    rule["price"],
                )
        last_prices[token_id] = price

    state["last_prices"] = last_prices
    save_json(state_path, state)

    return {
        "success": True,
        "alerts_sent": alerts_sent,
        "message": manual_message,
    }


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run crypto price check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running cryptobot: %s", format_run_info(schedule_context))

    config_path = Path(
        os.getenv("CRYPTOBOT_CONFIG", str(CONFIG_DIR / "cryptobot.config"))
    )
    try:
        # interval_sec removed from config - configs/schedule.yaml controls frequency
        config = load_json(
            config_path,
            {"currency": "usd", "rules": []},
            strict=True,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    currency = str(config.get("currency", "usd")).lower()
    try:
        rules = normalize_rules(config.get("rules", []), currency)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    if not rules:
        return {"success": False, "error": "no rules configured", "alerts_sent": 0}

    if currency != "usd":
        return {
            "success": False,
            "error": "cryptobot currently supports only 'usd' currency with DefiLlama price data",
            "alerts_sent": 0,
        }
    non_usd_rules = [r["id"] for r in rules if str(r.get("currency", "usd")).lower() != "usd"]
    if non_usd_rules:
        return {
            "success": False,
            "error": "cryptobot rule-level currency overrides are not supported with DefiLlama (usd only)",
            "alerts_sent": 0,
        }
    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    state_path = Path(os.getenv("CRYPTOBOT_STATE", str(STATE_DIR / "cryptobot.state.json"))).expanduser()

    return poll(rules, currency, tg_token, tg_chat_id, state_path, manual_trigger)


def main() -> int:
    setup_logging()
    load_env_file()
    result = run()
    if not result.get("success"):
        logging.error("[cryptobot] %s", result.get("error"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
