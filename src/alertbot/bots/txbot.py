#!/usr/bin/env python3
"""Etherscan v2 + Blockscout fallback transaction alert bot."""

import json
import logging
import os
import sys
import time

import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alertbot.common import (
    CONFIG_DIR,
    STATE_DIR,
    calculate_lookback_minutes,
    format_run_info,
    getenv_required,
    iso_now,
    load_env_file,
    load_json,
    request_json,
    save_json,
    send_telegram_alert,
)

DEFAULT_POLL_WINDOW_BLOCKS = 200
DEFAULT_STATE_PATH = STATE_DIR / "txbot.state.json"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "private" / "txbot.config.json"
LEGACY_DEFAULT_CONFIG_PATH = CONFIG_DIR / "txbot.config.json"
DEFAULT_API_URL = "https://api.etherscan.io/v2/api"
SLEEP_BETWEEN_CALLS = 0.4
ETHERSCAN_SOFT_ERROR_MAX_RETRIES = 3

BLOCKSCOUT_API_URLS = {
    42161: "https://arbitrum.blockscout.com/api/v2",  # Arbitrum
    8453: "https://base.blockscout.com/api/v2",       # Base
    137: "https://polygon.blockscout.com/api/v2",     # Polygon
}
ETHERSCAN_FREE_UNSUPPORTED_CHAIN_IDS = {
    8453,  # Base
}


def parse_iso_from_seconds(seconds: str | int | None) -> str:
    if seconds is None:
        return iso_now()
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc).isoformat()
    except Exception:
        return iso_now()


def resolve_txbot_config_path() -> Path:
    configured = os.environ.get("TXBOT_CONFIG")
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists():
            return configured_path
        if DEFAULT_CONFIG_PATH.exists():
            logging.warning(
                "[txbot] TXBOT_CONFIG points to missing file %s; falling back to %s",
                configured_path,
                DEFAULT_CONFIG_PATH,
            )
            return DEFAULT_CONFIG_PATH
        if LEGACY_DEFAULT_CONFIG_PATH.exists() and configured_path != LEGACY_DEFAULT_CONFIG_PATH:
            logging.warning(
                "[txbot] TXBOT_CONFIG points to missing file %s; falling back to %s",
                configured_path,
                LEGACY_DEFAULT_CONFIG_PATH,
            )
            return LEGACY_DEFAULT_CONFIG_PATH
        return configured_path
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return LEGACY_DEFAULT_CONFIG_PATH


def etherscan_block_number(api_url: str, api_key: str, chain_id: int) -> int:
    payload = request_json(
        api_url,
        {
            "module": "proxy",
            "action": "eth_blockNumber",
            "chainid": str(chain_id),
            "apikey": api_key,
        },
    )
    result = payload.get("result")
    if result is None:
        raise RuntimeError(f"etherscan eth_blockNumber missing result: {payload}")
    return int(result, 16)


def etherscan_txlist(
    api_url: str,
    api_key: str,
    chain_id: int,
    address: str,
    startblock: int,
    page: int,
    offset: int,
) -> list[dict]:
    params = {
        "module": "account",
        "action": "txlist",
        "chainid": str(chain_id),
        "address": address,
        "startblock": str(startblock),
        "endblock": "9999999999",
        "sort": "asc",
        "page": str(page),
        "offset": str(offset),
        "apikey": api_key,
    }

    for attempt in range(ETHERSCAN_SOFT_ERROR_MAX_RETRIES + 1):
        payload = request_json(api_url, params)
        status = payload.get("status")
        message = payload.get("message")
        result = payload.get("result")

        if status == "0" and isinstance(message, str) and "No transactions" in message:
            return []

        message_lc = message.lower() if isinstance(message, str) else ""
        retryable_semantic_error = (
            status == "0"
            and result is None
            and (
                "timeout" in message_lc
                or "too busy" in message_lc
                or "try again later" in message_lc
            )
        )
        if retryable_semantic_error and attempt < ETHERSCAN_SOFT_ERROR_MAX_RETRIES:
            delay = 1.0 * (2 ** attempt)
            logging.warning(
                "[txbot] etherscan txlist transient error for chain %s %s (attempt %d/%d): %s; retrying in %.1fs",
                chain_id,
                address,
                attempt + 1,
                ETHERSCAN_SOFT_ERROR_MAX_RETRIES,
                message,
                delay,
            )
            time.sleep(delay)
            continue

        if not isinstance(result, list):
            raise RuntimeError(f"etherscan txlist invalid result: {payload}")
        return result

    raise RuntimeError("etherscan txlist retry loop exhausted")


def blockscout_latest_block(api_url: str) -> int:
    payload = request_json(f"{api_url}/stats")
    value = payload.get("latest_block_number") or payload.get("total_blocks")
    if value is None:
        raise RuntimeError(f"blockscout stats missing latest_block_number: {payload}")
    return int(value)


def blockscout_tx_page(api_url: str, address: str, params: dict | None = None) -> dict:
    url = f"{api_url}/addresses/{address}/transactions"
    return request_json(url, params=params)


def normalize_address(addr: str) -> str:
    return addr.strip().lower()


def parse_watch_addresses(env_value: str | None, config_value: list | None) -> list[str]:
    addrs: list[str] = []
    if env_value:
        addrs.extend([a for a in env_value.split(",") if a.strip()])
    if config_value:
        addrs.extend([str(a) for a in config_value if str(a).strip()])
    normalized = []
    seen = set()
    for addr in addrs:
        n = normalize_address(addr)
        if n and n not in seen:
            normalized.append(n)
            seen.add(n)
    return normalized


def tx_from_address(tx: dict) -> str | None:
    value = tx.get("from")
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, dict):
        for key in ("hash", "address"):
            if isinstance(value.get(key), str):
                return value[key].lower()
    return None


def tx_hash(tx: dict) -> str:
    for key in ("hash", "txhash", "transaction_hash"):
        if isinstance(tx.get(key), str):
            return tx[key]
    return ""


def tx_block_number(tx: dict) -> int | None:
    for key in ("blockNumber", "block_number"):
        value = tx.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except Exception:
            return None
    return None


def tx_timestamp(tx: dict) -> str:
    if isinstance(tx.get("timeStamp"), str):
        return parse_iso_from_seconds(tx.get("timeStamp"))
    if isinstance(tx.get("timestamp"), str):
        return tx.get("timestamp")
    if isinstance(tx.get("timestamp"), int):
        return parse_iso_from_seconds(tx.get("timestamp"))
    return iso_now()


def tx_age_seconds(tx: dict) -> float | None:
    """Return the age of a transaction in seconds, or None if undetermined."""
    now = datetime.now(tz=timezone.utc)
    ts = tx.get("timeStamp") or tx.get("timestamp")
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            tx_time = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        elif isinstance(ts, str) and ts.isdigit():
            tx_time = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        elif isinstance(ts, str):
            tx_time = datetime.fromisoformat(ts)
        else:
            return None
        return (now - tx_time).total_seconds()
    except Exception:
        return None


def is_spam_tx(
    tx: dict,
    min_native_value: float,
    ignore_zero_value_contract_calls: bool,
    max_tx_age_minutes: int = 0,
) -> bool:
    """Return True if the transaction should be filtered as spam."""
    # Filter transactions older than the max age threshold
    if max_tx_age_minutes > 0:
        age = tx_age_seconds(tx)
        if age is not None and age > max_tx_age_minutes * 60:
            return True

    value_wei_str = tx.get("value") or "0"
    try:
        value_wei = int(value_wei_str)
    except (ValueError, TypeError):
        value_wei = 0
    value_native = value_wei / 1e18

    # Filter zero-value contract interactions (common spam: token transfers, swaps, approvals)
    if ignore_zero_value_contract_calls and value_wei == 0:
        input_data = tx.get("input") or tx.get("raw_input") or "0x"
        if input_data != "0x" and len(input_data) > 2:
            return True

    # Filter transactions below minimum native value threshold
    if min_native_value > 0 and value_native < min_native_value:
        return True

    return False


def format_alert(address: str, chain_name: str, chain_id: int, block: int | None, txid: str, timestamp: str) -> str:
    masked_address = f"**{address[-4:]}" if address else "unknown"
    return (
        f"New tx from {masked_address}\n"
        f"Chain: {chain_name} (chain_id {chain_id})\n"
        f"Block: {block if block is not None else 'unknown'}\n"
        f"Tx: {txid or 'unknown'}\n"
        f"Time: {timestamp}"
    )


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run transaction check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running txbot: %s", format_run_info(schedule_context))

    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
        api_key = getenv_required("ETHERSCAN_API_KEY")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    config_path = resolve_txbot_config_path()
    state_path = Path(os.environ.get("TXBOT_STATE", DEFAULT_STATE_PATH))
    default_api_url = os.environ.get("TXBOT_API_URL", DEFAULT_API_URL)

    config = load_json(config_path, {"chains": [], "watch_addresses": []}, strict=True)
    chains = config.get("chains") or []
    if not isinstance(chains, list) or not chains:
        return {
            "success": False,
            "error": f"config missing chains in {config_path}",
            "alerts_sent": 0,
        }

    watch_addresses = parse_watch_addresses(
        None,
        config.get("watch_addresses") if isinstance(config.get("watch_addresses"), list) else None,
    )
    if not watch_addresses:
        return {"success": False, "error": "No watch addresses provided", "alerts_sent": 0}

    poll_window_blocks = int(config.get("poll_window_blocks", DEFAULT_POLL_WINDOW_BLOCKS))
    min_native_value = float(config.get("min_native_value", 5))
    ignore_zero_value_contract_calls = bool(config.get("ignore_zero_value_contract_calls", True))
    max_tx_age_minutes = int(config.get("max_tx_age_minutes", 60))

    state = load_json(state_path, {"last_run": None, "chains": {}}, strict=False)
    state.setdefault("chains", {})

    total_alerts_sent = 0
    chain_results = []

    for chain in chains:
        name = chain.get("name")
        chain_id = chain.get("chain_id")
        if not name or chain_id is None:
            logging.warning("[txbot] skipping chain with missing name/chain_id")
            continue

        api_url = chain.get("api_url") or default_api_url
        blockscout_api_url = chain.get("blockscout_api_url") or BLOCKSCOUT_API_URLS.get(int(chain_id))

        latest_block = None
        use_blockscout = False
        chain_id_int = int(chain_id)

        if chain_id_int in ETHERSCAN_FREE_UNSUPPORTED_CHAIN_IDS:
            logging.info(
                "[txbot] skipping etherscan for %s (chain_id=%s): free tier unsupported",
                name,
                chain_id_int,
            )
        else:
            try:
                latest_block = etherscan_block_number(api_url, api_key, chain_id_int)
            except Exception as exc:
                logging.warning(
                    "[txbot] etherscan blockNumber failed for %s (%s): %s",
                    name,
                    api_url,
                    exc,
                )
            time.sleep(SLEEP_BETWEEN_CALLS)
        if latest_block is None and blockscout_api_url:
            try:
                latest_block = blockscout_latest_block(blockscout_api_url)
                use_blockscout = True
            except Exception as exc:
                logging.warning("[txbot] blockscout stats failed for %s: %s", name, exc)

        if latest_block is None:
            continue

        chain_state = state["chains"].get(name, {})
        last_block = chain_state.get("last_block")
        if isinstance(last_block, int):
            startblock = last_block + 1
        else:
            startblock = max(int(latest_block) - poll_window_blocks + 1, 0)

        max_block_seen = None
        chain_alerts = 0

        if not use_blockscout:
            for address in watch_addresses:
                page = 1
                offset = 100
                while True:
                    try:
                        txs = etherscan_txlist(api_url, api_key, chain_id_int, address, startblock, page, offset)
                    except Exception as exc:
                        logging.warning(
                            "[txbot] etherscan txlist failed for %s %s: %s",
                            name,
                            address,
                            exc,
                        )
                        txs = None
                    time.sleep(SLEEP_BETWEEN_CALLS)
                    if txs is None:
                        if blockscout_api_url:
                            use_blockscout = True
                        break
                    if not txs:
                        break
                    for tx in txs:
                        from_addr = tx_from_address(tx)
                        if from_addr != address:
                            continue
                        block_num = tx_block_number(tx)
                        if block_num is not None:
                            if max_block_seen is None or block_num > max_block_seen:
                                max_block_seen = block_num
                        if is_spam_tx(tx, min_native_value, ignore_zero_value_contract_calls, max_tx_age_minutes):
                            logging.debug("[txbot] skipping spam tx %s", tx_hash(tx))
                            continue
                        alert = format_alert(
                            address=address,
                            chain_name=name,
                            chain_id=chain_id_int,
                            block=block_num,
                            txid=tx_hash(tx),
                            timestamp=tx_timestamp(tx),
                        )
                        try:
                            send_telegram_alert(tg_token, tg_chat_id, alert)
                            total_alerts_sent += 1
                            chain_alerts += 1
                        except Exception as exc:
                            logging.warning("[txbot] telegram send failed: %s", exc)
                    if len(txs) < offset:
                        break
                    page += 1

        if use_blockscout and blockscout_api_url:
            for address in watch_addresses:
                next_params = {"block_number": str(startblock), "order": "asc"}
                while True:
                    try:
                        payload = blockscout_tx_page(blockscout_api_url, address, params=next_params)
                    except Exception as exc:
                        logging.warning(
                            "[txbot] blockscout tx page failed for %s %s: %s",
                            name,
                            address,
                            exc,
                        )
                        break
                    time.sleep(SLEEP_BETWEEN_CALLS)
                    items = payload.get("items")
                    if not isinstance(items, list):
                        break
                    if not items:
                        break
                    for tx in items:
                        from_addr = tx_from_address(tx)
                        if from_addr != address:
                            continue
                        block_num = tx_block_number(tx)
                        if block_num is not None:
                            if max_block_seen is None or block_num > max_block_seen:
                                max_block_seen = block_num
                        if is_spam_tx(tx, min_native_value, ignore_zero_value_contract_calls, max_tx_age_minutes):
                            logging.debug("[txbot] skipping spam tx %s", tx_hash(tx))
                            continue
                        alert = format_alert(
                            address=address,
                            chain_name=name,
                            chain_id=chain_id_int,
                            block=block_num,
                            txid=tx_hash(tx),
                            timestamp=tx_timestamp(tx),
                        )
                        try:
                            send_telegram_alert(tg_token, tg_chat_id, alert)
                            total_alerts_sent += 1
                            chain_alerts += 1
                        except Exception as exc:
                            logging.warning("[txbot] telegram send failed: %s", exc)
                    next_page = payload.get("next_page_params")
                    if not isinstance(next_page, dict) or not next_page:
                        break
                    next_params = next_page

        if max_block_seen is None:
            max_block_seen = int(latest_block)

        state["chains"][name] = {"last_block": max_block_seen}
        chain_results.append({"chain": name, "alerts": chain_alerts})

    state["last_run"] = iso_now()
    save_json(state_path, state)

    message = None
    if manual_trigger:
        if total_alerts_sent > 0:
            message = f"✅ Found {total_alerts_sent} new transaction(s)"
            if chain_results:
                chain_summary = ", ".join(
                    f"{r['chain']}: {r['alerts']}" for r in chain_results if r["alerts"] > 0
                )
                if chain_summary:
                    message += f" ({chain_summary})"
        else:
            message = "✅ No new transactions found"

    return {
        "success": True,
        "alerts_sent": total_alerts_sent,
        "message": message,
    }


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
