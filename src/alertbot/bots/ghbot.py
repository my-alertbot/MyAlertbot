#!/usr/bin/env python3
"""GitHub Notifications polling bot."""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from alertbot.common import (
    DEFAULT_TIMEOUT,
    STATE_DIR,
    format_run_info,
    getenv_required,
    iso_now,
    load_env_file,
    load_json,
    request_with_retry,
    save_json,
    send_telegram_alert,
    setup_logging,
)

API_URL = "https://api.github.com/notifications"

DEFAULT_MANUAL_LOOKBACK_HOURS = 24


def subject_api_url_to_web_url(subject_api_url: str | None) -> str | None:
    """Convert common GitHub API subject URLs to GitHub web URLs."""
    if not subject_api_url:
        return None
    if "api.github.com" not in subject_api_url:
        return subject_api_url

    parsed = urlparse(subject_api_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 5 or parts[0] != "repos":
        return None

    owner, repo, resource = parts[1], parts[2], parts[3]
    resource_id = parts[4]
    base = f"https://github.com/{owner}/{repo}"

    if resource == "pulls":
        return f"{base}/pull/{resource_id}"
    if resource == "issues":
        return f"{base}/issues/{resource_id}"
    if resource == "commits":
        return f"{base}/commit/{resource_id}"

    return None

def build_headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def backoff_until_reset(headers) -> None:
    reset = headers.get("X-RateLimit-Reset")
    if not reset:
        time.sleep(60)
        return
    try:
        reset_ts = int(reset)
        sleep_for = max(0, reset_ts - int(time.time()) + 5)
        time.sleep(sleep_for)
    except Exception:
        time.sleep(60)


def resolve_subject_html_url(
    subject_api_url: str | None,
    headers: dict[str, str],
    cache: dict[str, str],
) -> str | None:
    """Resolve a GitHub API subject URL to a web URL using `html_url` when available."""
    if not subject_api_url:
        return None
    if "api.github.com" not in subject_api_url:
        return subject_api_url
    if subject_api_url in cache:
        return cache[subject_api_url]

    try:
        resp = request_with_retry(
            method="GET",
            url=subject_api_url,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code != 200:
            logging.debug("[ghalert] subject URL resolution failed HTTP %s: %s", resp.status_code, subject_api_url)
            cache[subject_api_url] = subject_api_url_to_web_url(subject_api_url) or subject_api_url
            return cache[subject_api_url]

        payload = resp.json()
        html_url = payload.get("html_url") if isinstance(payload, dict) else None
        if isinstance(html_url, str) and html_url.strip():
            cache[subject_api_url] = html_url.strip()
            return cache[subject_api_url]
    except Exception as exc:
        logging.debug("[ghalert] failed to resolve subject URL %s: %s", subject_api_url, exc)

    cache[subject_api_url] = subject_api_url_to_web_url(subject_api_url) or subject_api_url
    return cache[subject_api_url]


def alert(
    notification: dict,
    tg_token: str,
    tg_chat_id: str,
    gh_headers: dict[str, str],
    subject_link_cache: dict[str, str],
) -> bool:
    """Send alert for a notification. Returns True if successful."""
    repo = notification.get("repository", {}).get("full_name", "?")
    subject = notification.get("subject", {})
    title = subject.get("title", "(no title)")
    reason = notification.get("reason", "?")
    url = notification.get("subject", {}).get("url")
    html_url = notification.get("repository", {}).get("html_url")
    # Prefer the web URL resolved from the GitHub API subject URL (e.g. PR html_url).
    # Fall back to the original subject URL, then repo URL.
    resolved_url = resolve_subject_html_url(url, gh_headers, subject_link_cache)
    link = resolved_url or url or html_url or ""
    text = f"{repo}\n{reason} | {title}\n{link}"
    try:
        send_telegram_alert(tg_token, tg_chat_id, text)
        return True
    except Exception as exc:
        logging.warning("[ghalert] failed to send Telegram alert: %s", exc)
        return False


def poll(
    token: str,
    state_path: Path,
    tg_token: str,
    tg_chat_id: str,
    last_run: str | None = None,
) -> int:
    """Poll GitHub notifications and send alerts.

    Args:
        token: GitHub API token
        state_path: Path to state file
        tg_token: Telegram bot token
        tg_chat_id: Telegram chat ID
        last_run: Optional ISO timestamp to use as 'since' parameter

    Returns:
        Number of alerts sent
    """
    state = load_json(state_path, {"last_seen_at": None, "recent_ids": []})

    # Use last_run from schedule_context if provided, otherwise from state
    since = last_run if last_run is not None else state.get("last_seen_at")
    recent_ids = set(state.get("recent_ids", []))

    if not since:
        # First run - just record current time and return
        state["last_seen_at"] = iso_now()
        state["recent_ids"] = []
        save_json(state_path, state)
        return 0

    headers = build_headers(token)
    params = {"since": since} if since else None
    try:
        resp = request_with_retry(
            method="GET",
            url=API_URL,
            headers=headers,
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        logging.error("[ghalert] request error: %s", exc)
        return 0

    if resp.status_code == 304:
        return 0
    if resp.status_code in (401, 403):
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            backoff_until_reset(resp.headers)
            return 0
        logging.error("[ghalert] authentication/authorization error")
        raise RuntimeError("GitHub authentication/authorization error")
    if resp.status_code != 200:
        logging.error("[ghalert] HTTP error: %s", resp.status_code)
        return 0

    try:
        notifications = resp.json()
    except ValueError as exc:
        logging.error("[ghalert] invalid JSON: %s", exc)
        return 0
    if not isinstance(notifications, list):
        return 0

    # GitHub returns newest first; reverse for chronological alerts
    alerts_sent = 0
    new_ids = []
    subject_link_cache: dict[str, str] = {}
    for n in reversed(notifications):
        nid = n.get("id")
        if nid and nid not in recent_ids:
            if alert(n, tg_token, tg_chat_id, headers, subject_link_cache):
                alerts_sent += 1
            new_ids.append(nid)

    # Update state
    if notifications:
        # Use the most recent updated_at as the next since
        latest = notifications[0].get("updated_at") or iso_now()
        state["last_seen_at"] = latest
        # Keep small window of recent IDs to avoid duplicates
        keep = (new_ids + list(recent_ids))[:100]
        state["recent_ids"] = keep
        save_json(state_path, state)

    return alerts_sent


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run GitHub notifications check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs
            - interval_minutes: int
            - last_run: str (ISO timestamp or None)
            - this_run: str (ISO timestamp)
            - bot_name: str

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running ghbot: %s", format_run_info(schedule_context))

    try:
        token = getenv_required("GH_TOKEN")
        tg_token = getenv_required("TELEGRAM_BOT_TOKEN")
        tg_chat_id = chat_id or getenv_required("TELEGRAM_CHAT_ID")
    except ValueError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    state_path = Path(os.getenv("GH_ALERT_STATE", str(STATE_DIR / "ghbot.state.json"))).expanduser()

    # Determine 'since' parameter
    last_run: str | None = None
    if schedule_context is not None:
        # Use last_run from schedule_context if available
        last_run = schedule_context.get("last_run")
    elif manual_trigger:
        # Manual trigger without schedule context - use shorter lookback
        since_dt = datetime.now(timezone.utc) - timedelta(hours=DEFAULT_MANUAL_LOOKBACK_HOURS)
        last_run = since_dt.isoformat()

    try:
        alerts_sent = poll(token, state_path, tg_token, tg_chat_id, last_run=last_run)
        return {
            "success": True,
            "alerts_sent": alerts_sent,
            "message": f"Sent {alerts_sent} notification(s)",
        }
    except RuntimeError as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}
    except Exception as exc:
        logging.exception("[ghalert] unexpected error")
        return {"success": False, "error": str(exc), "alerts_sent": 0}


def main() -> int:
    setup_logging()
    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
