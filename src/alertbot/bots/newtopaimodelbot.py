#!/usr/bin/env python3
"""Alert when a new model enters the llm-stats.com homepage top-10 (chat arena)."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
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
    setup_logging,
)

SITE_URL = "https://llm-stats.com/"
DEFAULT_API_BASE_URL = "https://api.zeroeval.com"
DEFAULT_ARENA_NAME = "chat-arena"
DEFAULT_TOP_N = 10
DEFAULT_STATE_FILE = STATE_DIR / "newtopaimodelbot.state.json"


@dataclass(frozen=True)
class TopEntry:
    model_id: str
    name: str
    organization: str
    rank: int

    @property
    def display_name(self) -> str:
        org = self.organization.strip()
        if org:
            return f"{self.name} ({org})"
        return self.name


def _leaderboard_url(
    arena_name: str = DEFAULT_ARENA_NAME,
    limit: int = DEFAULT_TOP_N,
    offset: int = 0,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> str:
    base = api_base_url.rstrip("/")
    return f"{base}/magia/arenas/{arena_name}/leaderboard?limit={limit}&offset={offset}"


def parse_top_entries(payload: Any, top_n: int = DEFAULT_TOP_N) -> list[TopEntry]:
    if not isinstance(payload, dict):
        raise RuntimeError("Leaderboard payload is not an object")

    rows = payload.get("leaderboard")
    if not isinstance(rows, list):
        raise RuntimeError("Leaderboard payload missing list field: leaderboard")

    entries: list[TopEntry] = []
    seen_ids: set[str] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        raw_id = row.get("model_id") or row.get("variant_id")
        raw_name = row.get("model_name") or row.get("variant_key") or raw_id
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue

        model_id = raw_id.strip()
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)

        organization = row.get("organization")
        if not isinstance(organization, str):
            organization = ""

        entries.append(
            TopEntry(
                model_id=model_id,
                name=raw_name.strip(),
                organization=organization.strip(),
                rank=len(entries) + 1,
            )
        )
        if len(entries) >= max(1, top_n):
            break

    if len(entries) < max(1, top_n):
        raise RuntimeError(f"Expected at least {top_n} leaderboard rows, got {len(entries)}")

    return entries


def fetch_top_entries(
    arena_name: str = DEFAULT_ARENA_NAME,
    top_n: int = DEFAULT_TOP_N,
    api_base_url: str = DEFAULT_API_BASE_URL,
) -> list[TopEntry]:
    payload = request_json(_leaderboard_url(arena_name=arena_name, limit=top_n, api_base_url=api_base_url))
    return parse_top_entries(payload, top_n=top_n)


def _state_snapshot(entries: list[TopEntry]) -> list[dict[str, Any]]:
    return [
        {
            "model_id": entry.model_id,
            "name": entry.name,
            "organization": entry.organization,
            "rank": entry.rank,
        }
        for entry in entries
    ]


def find_new_top_entries(previous_state: dict[str, Any], current_entries: list[TopEntry]) -> list[TopEntry]:
    previous = previous_state.get("top_entries")
    previous_ids: set[str] = set()
    if isinstance(previous, list):
        for item in previous:
            if not isinstance(item, dict):
                continue
            model_id = item.get("model_id")
            if isinstance(model_id, str) and model_id.strip():
                previous_ids.add(model_id.strip())

    return [entry for entry in current_entries if entry.model_id not in previous_ids]


def format_alert_message(new_entries: list[TopEntry], current_entries: list[TopEntry]) -> str:
    plural = "entry" if len(new_entries) == 1 else "entries"
    lines = [f"🤖 New llm-stats top 10 {plural} (chat arena)"]
    for entry in new_entries:
        lines.append(f"#{entry.rank}: {entry.display_name}")

    lines.append("")
    lines.append("Current top 10:")
    lines.extend(f"{entry.rank}. {entry.name}" for entry in current_entries[:DEFAULT_TOP_N])
    lines.append(SITE_URL)
    return "\n".join(lines)


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logging.debug("Running newtopaimodelbot: %s", format_run_info(schedule_context))

    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "alerts_sent": 0, "error": str(exc)}

    state_path = DEFAULT_STATE_FILE
    state = load_json(state_path, {"top_entries": [], "last_checked_at": None})

    try:
        current_entries = fetch_top_entries()
    except Exception as exc:
        logging.error("[newtopaimodelbot] failed to fetch leaderboard: %s", exc)
        return {"success": False, "alerts_sent": 0, "error": str(exc)}

    previous_snapshot = state.get("top_entries")
    is_first_run = not isinstance(previous_snapshot, list) or len(previous_snapshot) == 0
    new_entries = [] if is_first_run else find_new_top_entries(state, current_entries)

    alerts_sent = 0
    if new_entries:
        message = format_alert_message(new_entries, current_entries)
        try:
            send_telegram_alert(tg_token, tg_chat_id, message)
            alerts_sent = 1
        except Exception as exc:
            logging.warning("[newtopaimodelbot] failed to send Telegram alert: %s", exc)
            return {"success": False, "alerts_sent": 0, "error": str(exc)}

    state["top_entries"] = _state_snapshot(current_entries)
    state["last_checked_at"] = iso_now()
    save_json(state_path, state)

    if manual_trigger:
        if is_first_run:
            message = "Initialized llm-stats top 10 snapshot (chat arena). No alert sent on first run."
        elif new_entries:
            message = f"Alert sent: {len(new_entries)} new model(s) entered the llm-stats top 10."
        else:
            message = "No new models entered the llm-stats top 10 since the last check."
        return {"success": True, "alerts_sent": alerts_sent, "message": message}

    return {"success": True, "alerts_sent": alerts_sent}


def main() -> int:
    load_env_file()
    setup_logging()
    result = run()
    if not result.get("success", False):
        logging.error(result.get("error", "newtopaimodelbot failed"))
        return 1
    logging.info("newtopaimodelbot finished (alerts_sent=%s)", result.get("alerts_sent", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
