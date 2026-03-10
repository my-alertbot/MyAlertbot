#!/usr/bin/env python3
"""Alert on newly queued and executed multisig transactions for a Safe (Gnosis Safe)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from alertbot.common import (
    STATE_DIR,
    format_run_info,
    getenv_required,
    iso_now,
    load_env_file,
    load_json,
    request_json,
    save_json,
    send_telegram_alert,
)

DEFAULT_STATE_FILE = STATE_DIR / "gnosismultisigtxbot.state.json"
DEFAULT_ALERT_ON_FIRST_RUN = False
DEFAULT_PAGE_LIMIT = 100
DEFAULT_PAGE_MAX = 20


@dataclass(frozen=True)
class ChainInfo:
    chain_id: int
    name: str
    tx_service_base_url: str
    safe_app_slug: str | None
    native_symbol: str = "ETH"


@dataclass(frozen=True)
class SafeTarget:
    chain_id: int
    safe_address: str
    safe_label: str | None = None
    api_base_url_override: str | None = None


# Common Safe-supported chains. Use GNOSISMULTISIGTXBOT_API_BASE_URL for others.
CHAIN_INFO_BY_ID: dict[int, ChainInfo] = {
    1: ChainInfo(1, "Ethereum", "https://safe-transaction-mainnet.safe.global", "eth", "ETH"),
    10: ChainInfo(10, "Optimism", "https://safe-transaction-optimism.safe.global", "oeth", "ETH"),
    56: ChainInfo(56, "BNB Chain", "https://safe-transaction-bsc.safe.global", "bnb", "BNB"),
    100: ChainInfo(100, "Gnosis", "https://safe-transaction-gnosis-chain.safe.global", "gno", "xDAI"),
    137: ChainInfo(137, "Polygon", "https://safe-transaction-polygon.safe.global", "matic", "POL"),
    42161: ChainInfo(42161, "Arbitrum", "https://safe-transaction-arbitrum.safe.global", "arb1", "ETH"),
    43114: ChainInfo(43114, "Avalanche", "https://safe-transaction-avalanche.safe.global", "avax", "AVAX"),
    8453: ChainInfo(8453, "Base", "https://safe-transaction-base.safe.global", "base", "ETH"),
    747474: ChainInfo(747474, "Katana", "https://safe-transaction-katana.safe.global", "katana", "ETH"),
    11155111: ChainInfo(11155111, "Sepolia", "https://safe-transaction-sepolia.safe.global", "sep", "ETH"),
}


def getenv_int_required(key: str) -> int:
    raw = getenv_required(key)
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {key} must be an integer") from exc


def getenv_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def short_addr(value: str | None, head: int = 6, tail: int = 4) -> str:
    if not value:
        return "unknown"
    if len(value) <= head + tail + 3:
        return value
    return f"{value[:head]}...{value[-tail:]}"


def is_hex_address(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value[2:], 16)
        return True
    except ValueError:
        return False


def normalize_safe_address(value: str) -> str:
    address = value.strip()
    if not is_hex_address(address):
        raise ValueError("GNOSISMULTISIGTXBOT_SAFE_ADDRESS must be a 0x-prefixed 20-byte hex address")
    return address


def _parse_safe_target_item(index: int, raw_item: Any) -> SafeTarget:
    if not isinstance(raw_item, dict):
        raise ValueError(f"GNOSISMULTISIGTXBOT_TARGETS_JSON[{index}] must be an object")

    try:
        chain_id = int(raw_item.get("chain_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"GNOSISMULTISIGTXBOT_TARGETS_JSON[{index}].chain_id must be an integer") from exc

    safe_address_raw = raw_item.get("safe_address")
    if not isinstance(safe_address_raw, str) or not safe_address_raw.strip():
        raise ValueError(f"GNOSISMULTISIGTXBOT_TARGETS_JSON[{index}].safe_address is required")
    try:
        safe_address = normalize_safe_address(safe_address_raw)
    except ValueError as exc:
        raise ValueError(f"GNOSISMULTISIGTXBOT_TARGETS_JSON[{index}].safe_address invalid: {exc}") from exc

    safe_label = raw_item.get("safe_label")
    if safe_label is not None:
        safe_label = str(safe_label)

    api_base_url_override = raw_item.get("api_base_url")
    if api_base_url_override is not None:
        api_base_url_override = str(api_base_url_override)

    return SafeTarget(
        chain_id=chain_id,
        safe_address=safe_address,
        safe_label=safe_label,
        api_base_url_override=api_base_url_override,
    )


def parse_safe_targets_from_env() -> list[SafeTarget]:
    raw_targets = os.getenv("GNOSISMULTISIGTXBOT_TARGETS_JSON")
    if raw_targets and raw_targets.strip():
        try:
            payload = json.loads(raw_targets)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"GNOSISMULTISIGTXBOT_TARGETS_JSON must be valid JSON (line {exc.lineno} col {exc.colno})"
            ) from exc
        if not isinstance(payload, list) or not payload:
            raise ValueError("GNOSISMULTISIGTXBOT_TARGETS_JSON must be a non-empty JSON array")

        targets: list[SafeTarget] = []
        seen: set[tuple[int, str]] = set()
        for index, item in enumerate(payload):
            target = _parse_safe_target_item(index, item)
            key = (target.chain_id, target.safe_address.lower())
            if key in seen:
                raise ValueError(
                    f"GNOSISMULTISIGTXBOT_TARGETS_JSON contains duplicate target "
                    f"{target.chain_id}:{target.safe_address}"
                )
            seen.add(key)
            targets.append(target)
        return targets

    return [
        SafeTarget(
            chain_id=getenv_int_required("GNOSISMULTISIGTXBOT_CHAIN_ID"),
            safe_address=normalize_safe_address(getenv_required("GNOSISMULTISIGTXBOT_SAFE_ADDRESS")),
            safe_label=os.getenv("GNOSISMULTISIGTXBOT_SAFE_LABEL"),
            api_base_url_override=os.getenv("GNOSISMULTISIGTXBOT_API_BASE_URL"),
        )
    ]


def parse_confirmations_count(tx: dict[str, Any]) -> int:
    confirmations = tx.get("confirmations")
    if isinstance(confirmations, list):
        return len(confirmations)
    if isinstance(confirmations, int):
        return confirmations
    if isinstance(confirmations, str) and confirmations.isdigit():
        return int(confirmations)
    return 0


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_trueish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return False


def format_native_value(raw_value: Any, native_symbol: str) -> str:
    try:
        wei = Decimal(str(raw_value or "0"))
    except (InvalidOperation, ValueError):
        return str(raw_value or "0")
    native = wei / Decimal("1000000000000000000")
    if native == 0:
        return f"0 {native_symbol}"
    if native >= Decimal("0.0001"):
        return f"{native.normalize()} {native_symbol}"
    return f"{wei} wei"


def parse_method_name(tx: dict[str, Any]) -> str:
    data_decoded = tx.get("dataDecoded")
    if isinstance(data_decoded, dict):
        method = data_decoded.get("method")
        if isinstance(method, str) and method.strip():
            return method.strip()
    for key in ("methodName", "method"):
        value = tx.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def get_submission_timestamp(tx: dict[str, Any]) -> str:
    for key in ("submissionDate", "created", "modified"):
        value = tx.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return iso_now()


def safe_queue_url(chain_info: ChainInfo | None, safe_address: str) -> str | None:
    if chain_info is None or not chain_info.safe_app_slug:
        return None
    return f"https://app.safe.global/transactions/queue?safe={chain_info.safe_app_slug}:{safe_address}"


def get_chain_info(chain_id: int, api_base_url_override: str | None) -> tuple[ChainInfo | None, str]:
    chain_info = CHAIN_INFO_BY_ID.get(chain_id)
    if api_base_url_override:
        return (chain_info, api_base_url_override.rstrip("/"))
    if chain_info is None:
        raise ValueError(
            "Unsupported GNOSISMULTISIGTXBOT_CHAIN_ID; set GNOSISMULTISIGTXBOT_API_BASE_URL "
            "to the Safe Transaction Service base URL for your chain"
        )
    return (chain_info, chain_info.tx_service_base_url.rstrip("/"))


def fetch_multisig_transactions(
    api_base_url: str,
    safe_address: str,
    *,
    executed: bool,
) -> list[dict[str, Any]]:
    url = f"{api_base_url}/api/v1/safes/{safe_address}/multisig-transactions/"
    params: dict[str, Any] | None = {
        "executed": "true" if executed else "false",
        "limit": str(DEFAULT_PAGE_LIMIT),
        "ordering": "-modified",
    }

    items: list[dict[str, Any]] = []
    page_count = 0

    while url:
        page_count += 1
        if page_count > DEFAULT_PAGE_MAX:
            logging.warning(
                "[gnosismultisigtxbot] reached page limit (%s), truncating pending tx fetch",
                DEFAULT_PAGE_MAX,
            )
            break

        payload = request_json(url, params=params)
        params = None
        if not isinstance(payload, dict):
            raise RuntimeError("Safe Transaction Service returned non-object response")

        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError("Safe Transaction Service response missing results list")

        for tx in results:
            if isinstance(tx, dict):
                items.append(tx)

        next_url = payload.get("next")
        url = next_url if isinstance(next_url, str) and next_url else ""

    return items


def fetch_pending_multisig_transactions(api_base_url: str, safe_address: str) -> list[dict[str, Any]]:
    return fetch_multisig_transactions(api_base_url, safe_address, executed=False)


def fetch_executed_multisig_transactions(api_base_url: str, safe_address: str) -> list[dict[str, Any]]:
    return fetch_multisig_transactions(api_base_url, safe_address, executed=True)


def tx_sort_key(tx: dict[str, Any]) -> tuple[int, str]:
    return (parse_int(tx.get("nonce"), default=0), str(tx.get("submissionDate") or tx.get("modified") or ""))


def format_alert_message(
    tx: dict[str, Any],
    safe_address: str,
    safe_label: str | None,
    chain_id: int,
    chain_info: ChainInfo | None,
) -> str:
    chain_name = chain_info.name if chain_info else f"Chain {chain_id}"
    native_symbol = chain_info.native_symbol if chain_info else "ETH"
    confirmations_required = parse_int(tx.get("confirmationsRequired"), default=0)
    confirmations_count = parse_confirmations_count(tx)
    queue_link = safe_queue_url(chain_info, safe_address)

    safe_display = safe_label.strip() if isinstance(safe_label, str) and safe_label.strip() else safe_address
    if safe_display != safe_address:
        safe_line = f"{safe_display} ({safe_address})"
    else:
        safe_line = safe_display

    lines = [
        f"New Safe tx queued ({chain_name})",
        f"Safe: {safe_line}",
        f"Nonce: {tx.get('nonce', '?')}",
        f"Confirmations: {confirmations_count}/{confirmations_required or '?'}",
        f"Proposer: {short_addr(tx.get('proposer'))}",
        f"To: {short_addr(tx.get('to'))}",
        f"Value: {format_native_value(tx.get('value'), native_symbol)}",
        f"Method: {parse_method_name(tx)}",
        f"Submitted: {get_submission_timestamp(tx)}",
    ]

    safe_tx_hash = tx.get("safeTxHash")
    if isinstance(safe_tx_hash, str) and safe_tx_hash:
        lines.append(f"SafeTxHash: {safe_tx_hash}")
    if queue_link:
        lines.append(f"Queue: {queue_link}")

    return "\n".join(lines)


def get_execution_timestamp(tx: dict[str, Any]) -> str:
    for key in ("executionDate", "executedAt", "modified"):
        value = tx.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return iso_now()


def format_execution_alert_message(
    tx: dict[str, Any],
    safe_address: str,
    safe_label: str | None,
    chain_id: int,
    chain_info: ChainInfo | None,
) -> str:
    chain_name = chain_info.name if chain_info else f"Chain {chain_id}"
    native_symbol = chain_info.native_symbol if chain_info else "ETH"
    queue_link = safe_queue_url(chain_info, safe_address)

    safe_display = safe_label.strip() if isinstance(safe_label, str) and safe_label.strip() else safe_address
    if safe_display != safe_address:
        safe_line = f"{safe_display} ({safe_address})"
    else:
        safe_line = safe_display

    lines = [
        f"Safe tx executed on-chain ({chain_name})",
        f"Safe: {safe_line}",
        f"Nonce: {tx.get('nonce', '?')}",
        f"To: {short_addr(tx.get('to'))}",
        f"Value: {format_native_value(tx.get('value'), native_symbol)}",
        f"Method: {parse_method_name(tx)}",
        f"Executed: {get_execution_timestamp(tx)}",
    ]

    executor = tx.get("executor")
    if isinstance(executor, str) and executor:
        lines.append(f"Executor: {short_addr(executor)}")
    tx_hash = tx.get("transactionHash")
    if isinstance(tx_hash, str) and tx_hash:
        lines.append(f"TxHash: {tx_hash}")
    safe_tx_hash = tx.get("safeTxHash")
    if isinstance(safe_tx_hash, str) and safe_tx_hash:
        lines.append(f"SafeTxHash: {safe_tx_hash}")
    if queue_link:
        lines.append(f"Queue: {queue_link}")

    return "\n".join(lines)


def build_current_pending_state(txs: list[dict[str, Any]]) -> list[str]:
    hashes: list[str] = []
    seen: set[str] = set()
    for tx in txs:
        tx_hash = tx.get("safeTxHash")
        if not isinstance(tx_hash, str) or not tx_hash:
            continue
        if tx_hash in seen:
            continue
        seen.add(tx_hash)
        hashes.append(tx_hash)
    hashes.sort()
    return hashes


def process_safe_target(
    *,
    state: dict[str, Any],
    target: SafeTarget,
    telegram_token: str,
    telegram_chat_id: str,
    alert_on_first_run: bool,
    run_timestamp: str,
) -> dict[str, Any]:
    chain_id = target.chain_id
    safe_address = target.safe_address
    safe_label = target.safe_label

    chain_info, api_base_url = get_chain_info(chain_id, target.api_base_url_override)

    state.setdefault("safes", {})
    safe_key = f"{chain_id}:{safe_address.lower()}"
    safe_state = state["safes"].get(safe_key) or {}
    previous_pending = safe_state.get("pending_safe_tx_hashes")
    previous_pending_set = set(previous_pending) if isinstance(previous_pending, list) else set()
    is_first_run_for_safe = not isinstance(previous_pending, list)

    try:
        txs = fetch_pending_multisig_transactions(api_base_url, safe_address)
    except Exception as exc:
        error_msg = str(exc)
        if "HTTP 422" in error_msg or "HTTP 400" in error_msg:
            error_msg += " (Safe API may require a checksummed safe address)"
        raise RuntimeError(error_msg) from exc

    pending_txs = [tx for tx in txs if not is_trueish(tx.get("isExecuted"))]
    current_pending_hashes = build_current_pending_state(pending_txs)
    current_pending_set = set(current_pending_hashes)
    no_longer_pending_hashes = previous_pending_set - current_pending_set

    new_txs: list[dict[str, Any]] = []
    if is_first_run_for_safe and not alert_on_first_run:
        logging.info(
            "[gnosismultisigtxbot] seeding state for %s on chain %s (%s pending txs, no alerts)",
            safe_address,
            chain_id,
            len(current_pending_hashes),
        )
    else:
        for tx in pending_txs:
            tx_hash = tx.get("safeTxHash")
            if not isinstance(tx_hash, str) or not tx_hash:
                continue
            if tx_hash not in previous_pending_set:
                new_txs.append(tx)
        new_txs.sort(key=tx_sort_key)

    executed_txs: list[dict[str, Any]] = []
    if no_longer_pending_hashes:
        try:
            executed_candidates = fetch_executed_multisig_transactions(api_base_url, safe_address)
        except Exception as exc:
            logging.warning("[gnosismultisigtxbot] failed to fetch executed txs: %s", exc)
            executed_candidates = []

        seen_executed_hashes: set[str] = set()
        for tx in executed_candidates:
            tx_hash = tx.get("safeTxHash")
            if not isinstance(tx_hash, str) or not tx_hash:
                continue
            if tx_hash not in no_longer_pending_hashes:
                continue
            if tx_hash in seen_executed_hashes:
                continue
            seen_executed_hashes.add(tx_hash)
            executed_txs.append(tx)
        executed_txs.sort(key=tx_sort_key)

    alerts_sent = 0
    queued_alerts_sent = 0
    executed_alerts_sent = 0

    for tx in new_txs:
        message = format_alert_message(
            tx=tx,
            safe_address=safe_address,
            safe_label=safe_label,
            chain_id=chain_id,
            chain_info=chain_info,
        )
        try:
            send_telegram_alert(telegram_token, telegram_chat_id, message)
            alerts_sent += 1
            queued_alerts_sent += 1
        except Exception as exc:
            logging.warning("[gnosismultisigtxbot] telegram send failed: %s", exc)

    for tx in executed_txs:
        message = format_execution_alert_message(
            tx=tx,
            safe_address=safe_address,
            safe_label=safe_label,
            chain_id=chain_id,
            chain_info=chain_info,
        )
        try:
            send_telegram_alert(telegram_token, telegram_chat_id, message)
            alerts_sent += 1
            executed_alerts_sent += 1
        except Exception as exc:
            logging.warning("[gnosismultisigtxbot] telegram send failed: %s", exc)

    state["safes"][safe_key] = {
        "safe_address": safe_address,
        "chain_id": chain_id,
        "safe_label": safe_label or "",
        "pending_safe_tx_hashes": current_pending_hashes,
        "pending_count": len(current_pending_hashes),
        "last_synced_at": run_timestamp,
    }

    chain_label = chain_info.name if chain_info else f"chain {chain_id}"
    return {
        "alerts_sent": alerts_sent,
        "queued_alerts_sent": queued_alerts_sent,
        "executed_alerts_sent": executed_alerts_sent,
        "pending_count": len(current_pending_set),
        "chain_label": chain_label,
        "safe_address": safe_address,
        "chain_id": chain_id,
        "api_base_url": api_base_url,
    }


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logging.debug("Running gnosismultisigtxbot: %s", format_run_info(schedule_context))

    try:
        telegram_token = getenv_required("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
        targets = parse_safe_targets_from_env()
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    state_path = Path(os.getenv("GNOSISMULTISIGTXBOT_STATE_FILE", DEFAULT_STATE_FILE))
    alert_on_first_run = getenv_bool("GNOSISMULTISIGTXBOT_ALERT_ON_FIRST_RUN", DEFAULT_ALERT_ON_FIRST_RUN)

    state = load_json(
        state_path,
        {
            "version": 2,
            "safes": {},
            "last_run": None,
        },
        strict=False,
    )
    state.setdefault("safes", {})
    run_timestamp = iso_now()

    alerts_sent = 0
    queued_alerts_sent = 0
    executed_alerts_sent = 0
    target_summaries: list[dict[str, Any]] = []
    last_api_base_url: str | None = None
    last_chain_id: int | None = None

    for target in targets:
        try:
            result = process_safe_target(
                state=state,
                target=target,
                telegram_token=telegram_token,
                telegram_chat_id=telegram_chat_id,
                alert_on_first_run=alert_on_first_run,
                run_timestamp=run_timestamp,
            )
        except Exception as exc:
            return {
                "success": False,
                "error": f"{target.chain_id}:{target.safe_address} - {exc}",
                "alerts_sent": alerts_sent,
            }
        alerts_sent += result["alerts_sent"]
        queued_alerts_sent += result["queued_alerts_sent"]
        executed_alerts_sent += result["executed_alerts_sent"]
        target_summaries.append(result)
        last_api_base_url = result["api_base_url"]
        last_chain_id = result["chain_id"]

    state["version"] = 2 if len(targets) > 1 else 1
    if len(targets) == 1 and last_chain_id is not None and last_api_base_url is not None:
        state["chain_id"] = last_chain_id
        state["api_base_url"] = last_api_base_url
    else:
        state.pop("chain_id", None)
        state.pop("api_base_url", None)
    state["last_run"] = run_timestamp
    save_json(state_path, state)

    manual_message = None
    if manual_trigger:
        if len(target_summaries) == 1:
            summary = target_summaries[0]
            if alerts_sent:
                manual_message = (
                    f"✅ {alerts_sent} Safe transaction alert(s) for "
                    f"{short_addr(summary['safe_address'])} on {summary['chain_label']} "
                    f"(queued: {queued_alerts_sent}, executed: {executed_alerts_sent})"
                )
            else:
                manual_message = (
                    f"✅ No new Safe tx alerts for {short_addr(summary['safe_address'])} "
                    f"on {summary['chain_label']} (pending now: {summary['pending_count']})"
                )
        else:
            header = (
                f"✅ Safe multisig scan complete for {len(target_summaries)} target(s) "
                f"(alerts: {alerts_sent}, queued: {queued_alerts_sent}, executed: {executed_alerts_sent})"
            )
            lines = [header]
            for summary in target_summaries:
                lines.append(
                    f"- {summary['chain_label']} {short_addr(summary['safe_address'])}: "
                    f"pending={summary['pending_count']}"
                )
            manual_message = "\n".join(lines)

    return {
        "success": True,
        "alerts_sent": alerts_sent,
        "message": manual_message,
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
