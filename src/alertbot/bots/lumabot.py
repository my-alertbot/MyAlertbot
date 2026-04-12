#!/usr/bin/env python3
"""Monitor Luma event pages for new events."""

from __future__ import annotations

import json
import logging
import os
import re
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
    request_text,
    save_json,
    send_telegram_alert,
    setup_logging,
)

BOT_ID = "luma"
BOT_COMMAND = "luma"

DEFAULT_STATE_FILE = STATE_DIR / "lumabot.state.json"
DEFAULT_MAX_EVENTS = 50
DEFAULT_CHECK_INTERVAL_MINUTES = 60


@dataclass(frozen=True)
class LumaEvent:
    event_id: str
    name: str
    url: str
    start_at: str


def parse_page_urls(raw: str) -> list[str]:
    items = [item.strip() for item in raw.split(",")]
    urls: list[str] = []
    for item in items:
        if not item or item in urls:
            continue
        urls.append(item)
    return urls


def _navigate(data: Any, *keys: str) -> Any:
    """Navigate nested dicts by key sequence, returning None if any key is missing."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _extract_luma_event(entry: Any) -> LumaEvent | None:
    """Extract a LumaEvent from an API-style entry dict."""
    if not isinstance(entry, dict):
        return None

    # Luma API wraps: {"api_id": ..., "event": {...}}
    event_data = entry.get("event")
    if isinstance(event_data, dict):
        event_id = event_data.get("api_id") or event_data.get("id")
        name = event_data.get("name") or event_data.get("title")
        url = event_data.get("url") or ""
        start_at = event_data.get("start_at") or event_data.get("start_time") or ""
    else:
        # Entry itself may be the event
        event_id = entry.get("api_id") or entry.get("id")
        name = entry.get("name") or entry.get("title")
        url = entry.get("url") or ""
        start_at = entry.get("start_at") or entry.get("start_time") or ""

    if not event_id or not isinstance(event_id, str) or not event_id.strip():
        return None
    if not name or not isinstance(name, str):
        name = "Untitled Event"

    return LumaEvent(
        event_id=event_id.strip(),
        name=name.strip(),
        url=str(url).strip(),
        start_at=str(start_at).strip(),
    )


def extract_events_from_next_data(html: str) -> list[LumaEvent]:
    """Extract events from Next.js __NEXT_DATA__ embedded JSON."""
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        logging.debug("[lumabot] Failed to parse __NEXT_DATA__ JSON")
        return []

    page_props = _navigate(data, "props", "pageProps")
    if not isinstance(page_props, dict):
        return []

    # Try common Luma page structures for calendar/community pages
    entries: Any = (
        _navigate(page_props, "initialData", "entries")
        or _navigate(page_props, "initialData", "events")
        or page_props.get("entries")
        or page_props.get("events")
    )
    if not isinstance(entries, list):
        return []

    events: list[LumaEvent] = []
    for entry in entries:
        event = _extract_luma_event(entry)
        if event:
            events.append(event)
    return events


def _parse_json_ld_event(item: dict[str, Any]) -> LumaEvent | None:
    url = str(item.get("url") or "").strip()
    name = str(item.get("name") or "Untitled Event").strip()
    start_at = str(item.get("startDate") or "").strip()
    event_id = str(item.get("identifier") or item.get("@id") or url).strip()
    if not event_id:
        return None
    return LumaEvent(event_id=event_id, name=name, url=url, start_at=start_at)


def extract_events_from_json_ld(html: str) -> list[LumaEvent]:
    """Extract events from JSON-LD schema markup."""
    events: list[LumaEvent] = []
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.DOTALL,
    )
    for match in pattern.finditer(html):
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if item_type == "Event":
                event = _parse_json_ld_event(item)
                if event:
                    events.append(event)
            elif item_type in ("EventSeries", "ItemList"):
                sub_items = item.get("subEvent") or item.get("itemListElement") or []
                for sub in sub_items:
                    if isinstance(sub, dict):
                        event = _parse_json_ld_event(sub)
                        if event:
                            events.append(event)
    return events


def fetch_page_events(page_url: str) -> list[LumaEvent]:
    """Fetch a Luma page and extract its events."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    html = request_text(page_url, headers=headers)

    events = extract_events_from_next_data(html)
    if events:
        logging.debug("[lumabot] Found %d event(s) via __NEXT_DATA__ on %s", len(events), page_url)
        return events

    events = extract_events_from_json_ld(html)
    if events:
        logging.debug("[lumabot] Found %d event(s) via JSON-LD on %s", len(events), page_url)
        return events

    logging.warning("[lumabot] No events found on page: %s", page_url)
    return []


def format_alert_message(new_events: list[LumaEvent], page_url: str) -> str:
    plural = "event" if len(new_events) == 1 else "events"
    lines = [f"\U0001f4c5 {len(new_events)} new Luma {plural} on {page_url}"]
    for event in new_events:
        lines.append("")
        lines.append(event.name)
        if event.start_at:
            lines.append(f"\U0001f550 {event.start_at}")
        if event.url:
            lines.append(event.url)
    return "\n".join(lines)


def process_page(
    page_url: str,
    page_state: dict[str, Any],
    tg_token: str,
    tg_chat_id: str,
    max_events: int,
) -> tuple[int, int]:
    """Check a Luma page for new events. Returns (alerts_sent, failed_sends)."""
    events = fetch_page_events(page_url)
    if not events:
        return 0, 0

    events = events[:max_events]
    current_ids = [e.event_id for e in events]
    seen_ids: set[str] = set(page_state.get("seen_event_ids") or [])

    if not seen_ids:
        # First run — initialize state without alerting
        page_state["seen_event_ids"] = current_ids
        page_state["last_checked_at"] = iso_now()
        logging.info("[lumabot] Initialized %d event(s) for %s", len(events), page_url)
        return 0, 0

    new_events = [e for e in events if e.event_id not in seen_ids]
    page_state["last_checked_at"] = iso_now()

    if not new_events:
        logging.info("[lumabot] No new events for %s", page_url)
        return 0, 0

    message = format_alert_message(new_events, page_url)
    try:
        send_telegram_alert(tg_token, tg_chat_id, message)
    except Exception as exc:
        logging.warning("[lumabot] Failed to send alert: %s", exc)
        return 0, 1

    # Merge new IDs into seen, keeping a reasonable cap
    updated_seen = list(dict.fromkeys([*current_ids, *page_state.get("seen_event_ids", [])]))
    page_state["seen_event_ids"] = updated_seen[: max(50, max_events * 5)]
    return 1, 0


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Luma event monitor.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running lumabot: %s", format_run_info(schedule_context))

    try:
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "alerts_sent": 0, "error": str(exc)}

    raw_urls = os.getenv("LUMA_PAGE_URLS", "")
    page_urls = parse_page_urls(raw_urls)
    if not page_urls:
        logging.info("[lumabot] skipping: LUMA_PAGE_URLS is not configured")
        return {"success": True, "alerts_sent": 0}

    try:
        max_events = max(1, int(os.getenv("LUMA_MAX_EVENTS", str(DEFAULT_MAX_EVENTS))))
    except ValueError:
        max_events = DEFAULT_MAX_EVENTS

    state_path = Path(os.getenv("LUMA_STATE_FILE", str(DEFAULT_STATE_FILE)))
    state = load_json(state_path, {})

    total_sent = 0
    total_failed = 0
    for page_url in page_urls:
        page_state = state.setdefault(page_url, {})
        try:
            sent, failed = process_page(page_url, page_state, tg_token, tg_chat_id, max_events)
        except Exception as exc:
            logging.warning("[lumabot] Error processing %s: %s", page_url, exc)
            continue
        total_sent += sent
        total_failed += failed

    try:
        save_json(state_path, state)
    except Exception as exc:
        logging.warning("[lumabot] Failed to save state: %s", exc)

    message = None
    if manual_trigger:
        if total_sent > 0:
            message = f"\u2705 Sent {total_sent} Luma alert(s)"
        elif total_failed > 0:
            message = f"\u26a0\ufe0f No Luma alerts sent, {total_failed} failed to send"
        else:
            message = "\u2705 No new Luma events found"

    return {"success": True, "alerts_sent": total_sent, "message": message}


def main() -> int:
    load_env_file()
    setup_logging()
    result = run()
    if not result.get("success", False):
        logging.error(result.get("error", "lumabot failed"))
        return 1
    logging.info("lumabot finished (alerts_sent=%s)", result.get("alerts_sent", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
