#!/usr/bin/env python3
"""Stock price alert bot using Finnhub/Twelve Data and Telegram."""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from alertbot.bots.stockbot import (
    NY_TZ,
    PROVIDER_NAMES,
    fetch_quotes_chunk,
    get_quote_chunk_settings,
    is_nyse_regular_hours,
    resolve_stock_api_key,
    resolve_stock_provider,
)
from alertbot.common import (
    CONFIG_DIR,
    STATE_DIR,
    format_run_info,
    getenv_required,
    iso_now,
    load_env_file,
    load_json,
    save_json,
    send_telegram_alert,
    setup_logging,
)


def _chunk_symbols(symbols: list[str], chunk_size: int) -> list[list[str]]:
    return [symbols[i : i + chunk_size] for i in range(0, len(symbols), chunk_size)]


def normalize_rules(config: dict[str, Any], default_currency: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for alert_type in ("watch", "action"):
        raw_rules = config.get(alert_type, [])
        if not isinstance(raw_rules, list):
            raise ValueError(f"{alert_type} must be a list")

        for rule in raw_rules:
            if not isinstance(rule, dict):
                raise ValueError(f"invalid {alert_type} rule: must be an object")

            ticker = str(rule.get("ticker", "")).strip().upper()
            direction = rule.get("direction")
            price = rule.get("price")
            if not ticker or direction not in ("above", "below") or price is None:
                raise ValueError(
                    f"invalid {alert_type} rule: require ticker, direction, price"
                )

            rule_currency = str(rule.get("currency", default_currency)).upper()
            normalized.append(
                {
                    "ticker": ticker,
                    "direction": direction,
                    "price": float(price),
                    "currency": rule_currency,
                    "alert_type": alert_type,
                }
            )
    return normalized


def should_trigger(direction: str, last_price: float | None, price: float, threshold: float) -> bool:
    if last_price is None:
        return False
    if direction == "above":
        return last_price <= threshold and price > threshold
    return last_price >= threshold and price < threshold


def format_alert(
    ticker: str,
    alert_type: str,
    direction: str,
    threshold: float,
    price: float,
    currency: str,
) -> str:
    symbol = "$" if currency.lower() == "usd" else f"{currency.upper()} "
    return (
        f"{alert_type} alert\n"
        f"{ticker} crossed {direction} {symbol}{threshold:,.6g}\n"
        f"Current: {symbol}{price:,.6g}\n"
        f"Time: {iso_now()}"
    )


def format_manual_prices(
    tickers: list[str],
    prices: dict[str, dict[str, Any]],
    default_currency: str,
    rate_limited_tickers: set[str] | None = None,
) -> str:
    rate_limited = rate_limited_tickers or set()
    skipped_due_to_rate_limit: list[str] = []
    lines = ["Stock Prices"]
    for ticker in tickers:
        if ticker in rate_limited:
            skipped_due_to_rate_limit.append(ticker)
            continue
        quote = prices.get(ticker)
        if not quote:
            lines.append(f"{ticker}: ERROR (price unavailable)")
            continue
        price = quote.get("price")
        currency = quote.get("currency") or default_currency
        symbol = "$" if str(currency).lower() == "usd" else f"{str(currency).upper()} "
        lines.append(f"{ticker}: {symbol}{float(price):,.6g}")

    if skipped_due_to_rate_limit:
        sample = ", ".join(skipped_due_to_rate_limit[:5])
        suffix = "..." if len(skipped_due_to_rate_limit) > 5 else ""
        lines.append(
            f"Rate limit reached for {len(skipped_due_to_rate_limit)} ticker(s): "
            f"{sample}{suffix}. Quotes were skipped."
        )

    return "\n".join(lines)


def poll(
    provider: str,
    rules: list[dict[str, Any]],
    api_key: str,
    tg_token: str,
    tg_chat_id: str,
    state_path: Path,
    default_currency: str,
    manual_trigger: bool = False,
) -> dict[str, Any]:
    state = load_json(state_path, {"last_prices": {}})
    last_prices = state.get("last_prices", {})
    last_prices_snapshot = dict(last_prices)

    tickers = sorted({rule["ticker"] for rule in rules})
    prices: dict[str, dict[str, Any]] = {}
    rate_limited_tickers: list[str] = []
    provider_name = PROVIDER_NAMES.get(provider, provider)
    quote_chunk_size, quote_chunk_wait_seconds = get_quote_chunk_settings(provider)
    with requests.Session() as session:
        for chunk_index, chunk in enumerate(_chunk_symbols(tickers, quote_chunk_size)):
            if chunk_index > 0:
                logging.info(
                    "[stockalertbot] Waiting %ss before next quote chunk to respect %s minute limits",
                    quote_chunk_wait_seconds,
                    provider_name,
                )
                time.sleep(quote_chunk_wait_seconds)

            chunk_results = fetch_quotes_chunk(provider, session, chunk, api_key)
            for ticker in chunk:
                price, currency, _percent_change, error_type = chunk_results.get(
                    ticker, (None, None, None, "fetch_failed")
                )
                if error_type:
                    if error_type == "rate_limited":
                        rate_limited_tickers.append(ticker)
                    else:
                        logging.error("[stockalertbot] failed to fetch %s: %s", ticker, error_type)
                    continue
                if price is None:
                    logging.error("[stockalertbot] failed to fetch %s: missing price", ticker)
                    continue
                try:
                    prices[ticker] = {"price": price, "currency": currency}
                except Exception as exc:
                    logging.error("[stockalertbot] failed to process %s quote: %s", ticker, exc)

    if rate_limited_tickers:
        logging.warning(
            "[stockalertbot] %s rate limit reached for %d/%d ticker(s): %s",
            provider_name,
            len(rate_limited_tickers),
            len(tickers),
            ", ".join(rate_limited_tickers[:5]) + ("..." if len(rate_limited_tickers) > 5 else ""),
        )

    if not prices:
        if rate_limited_tickers:
            return {
                "success": False,
                "error": f"{provider_name} rate limit reached; no quotes available",
                "alerts_sent": 0,
            }
        return {
            "success": False,
            "error": "failed to fetch prices for all configured tickers",
            "alerts_sent": 0,
        }

    manual_message = (
        format_manual_prices(
            tickers,
            prices,
            default_currency,
            rate_limited_tickers=set(rate_limited_tickers),
        )
        if manual_trigger
        else None
    )
    alerts_sent = 0

    for rule in rules:
        ticker = rule["ticker"]
        quote = prices.get(ticker)
        if not quote:
            continue

        current_price = quote["price"]
        quote_currency = quote.get("currency") or rule["currency"]
        last_price = last_prices_snapshot.get(ticker)
        will_alert = should_trigger(
            rule["direction"], last_price, current_price, rule["price"]
        )

        alert_flag = " alert" if will_alert else ""
        logging.info(
            "[stockalertbot] %s price=%.6g (%s trigger %s %.6g) [%s]%s",
            ticker,
            current_price,
            "above" if current_price > rule["price"] else "below",
            rule["direction"],
            rule["price"],
            rule["alert_type"],
            alert_flag,
        )

        if will_alert and not manual_trigger:
            text = format_alert(
                ticker=ticker,
                alert_type=rule["alert_type"],
                direction=rule["direction"],
                threshold=rule["price"],
                price=current_price,
                currency=quote_currency,
            )
            try:
                send_telegram_alert(tg_token, tg_chat_id, text)
                alerts_sent += 1
            except Exception as exc:
                logging.warning("[stockalertbot] telegram send failed: %s", exc)
            else:
                logging.info(
                    "[stockalertbot] %s alert sent for %s %s %.6g",
                    rule["alert_type"],
                    ticker,
                    rule["direction"],
                    rule["price"],
                )

        last_prices[ticker] = current_price

    state["last_prices"] = last_prices
    save_json(state_path, state)

    return {"success": True, "alerts_sent": alerts_sent, "message": manual_message}


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run stock price alert checks.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running stockalertbot: %s", format_run_info(schedule_context))

    config_path = Path(
        os.getenv("STOCKALERT_CONFIG", str(CONFIG_DIR / "stockalert.config"))
    ).expanduser()
    try:
        config = load_json(
            config_path,
            {"currency": "USD", "watch": [], "action": []},
            strict=True,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    default_currency = str(config.get("currency", "USD")).upper()
    try:
        rules = normalize_rules(config, default_currency)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    if not rules:
        return {
            "success": False,
            "error": "no rules configured (watch/action lists are empty)",
            "alerts_sent": 0,
        }

    try:
        provider = resolve_stock_provider()
        api_key = resolve_stock_api_key(provider)
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    if not manual_trigger:
        now_utc = datetime.now(timezone.utc)
        # Use local NYSE-hours gating so scheduled checks avoid off-hours requests.
        market_open = is_nyse_regular_hours(now_utc)

        if not market_open:
            logging.info(
                "Skipping stockalert update outside NYSE trading session: %s",
                now_utc.astimezone(NY_TZ).isoformat(),
            )
            return {
                "success": True,
                "message": "Skipped outside NYSE trading session",
                "alerts_sent": 0,
            }

    state_path = Path(
        os.getenv("STOCKALERT_STATE", str(STATE_DIR / "stockalert.state.json"))
    ).expanduser()

    return poll(
        provider=provider,
        rules=rules,
        api_key=api_key,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        state_path=state_path,
        default_currency=default_currency,
        manual_trigger=manual_trigger,
    )


def main() -> int:
    setup_logging()
    load_env_file()
    result = run()
    if not result.get("success"):
        logging.error("[stockalertbot] %s", result.get("error"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
