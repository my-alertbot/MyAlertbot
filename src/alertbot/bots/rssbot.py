import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree

from alertbot.common import (
    STATE_DIR,
    calculate_lookback_minutes,
    format_run_info,
    getenv_required,
    load_env_file,
    load_json,
    parse_iso_utc,
    request_text,
    save_json,
    send_telegram_alert,
)

DEFAULT_STATE_FILE = STATE_DIR / "rssstate.json"
DEFAULT_MAX_ITEMS = 20
DEFAULT_CHECK_INTERVAL_MINUTES = 60


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    rss_feed_urls: List[str]
    rssstate_file: str
    rss_max_items: int
    check_interval_minutes: int


def getenv_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return int(value)


def parse_feed_urls(raw: str) -> List[str]:
    items = [item.strip() for item in raw.split(",")]
    urls: List[str] = []
    for item in items:
        if not item or item in urls:
            continue
        urls.append(item)
    return urls


def load_config(schedule_context: Optional[Dict[str, Any]] = None) -> Config:
    interval = calculate_lookback_minutes(
        schedule_context,
        default_minutes=DEFAULT_CHECK_INTERVAL_MINUTES,
        buffer_multiplier=1.0,
    )

    feed_urls = parse_feed_urls(os.getenv("RSS_FEED_URL", ""))
    if not feed_urls:
        raise ValueError("Missing required environment variable: RSS_FEED_URL")

    return Config(
        telegram_bot_token=getenv_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=getenv_required("TELEGRAM_CHAT_ID"),
        rss_feed_urls=feed_urls,
        rssstate_file=os.getenv("RSS_STATE_FILE", DEFAULT_STATE_FILE),
        rss_max_items=max(1, getenv_int("RSS_MAX_ITEMS", DEFAULT_MAX_ITEMS)),
        check_interval_minutes=interval,
    )


def fetch_feed(feed_url: str) -> str:
    return request_text(feed_url)


def normalize_text(value: Optional[str]) -> str:
    return (value or "").strip()


def localname(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def find_child_text(elem: ElementTree.Element, name: str) -> str:
    for child in list(elem):
        if localname(child.tag) == name:
            return normalize_text(child.text)
    return ""


def find_child_text_any(elem: ElementTree.Element, names: List[str]) -> str:
    for name in names:
        value = find_child_text(elem, name)
        if value:
            return value
    return ""


def find_atom_link(entry: ElementTree.Element) -> str:
    links = [child for child in list(entry) if localname(child.tag) == "link"]
    for link_elem in links:
        rel = normalize_text(link_elem.attrib.get("rel")).lower()
        href = normalize_text(link_elem.attrib.get("href"))
        if not href:
            continue
        if rel in ("", "alternate"):
            return href
    for link_elem in links:
        href = normalize_text(link_elem.attrib.get("href"))
        if href:
            return href
    return ""


def parse_feed_title(root: ElementTree.Element, fallback: str) -> str:
    for elem in root.iter():
        if localname(elem.tag) == "channel":
            channel_title = find_child_text(elem, "title")
            if channel_title:
                return channel_title
            break
    root_title = find_child_text(root, "title")
    if root_title:
        return root_title
    return fallback


def parse_rss(xml_text: str, fallback_title: str) -> tuple[str, List[Dict[str, str]]]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"Failed to parse RSS XML: {exc}") from exc

    feed_title = parse_feed_title(root, fallback_title)
    items: List[Dict[str, str]] = []

    rss_items = [elem for elem in root.iter() if localname(elem.tag) == "item"]
    if rss_items:
        for item in rss_items:
            guid = find_child_text(item, "guid")
            title = find_child_text(item, "title") or "Untitled"
            link = find_child_text(item, "link")
            pub_date = find_child_text(item, "pubDate")
            entry_id = guid or link or title
            items.append(
                {
                    "id": entry_id,
                    "title": title,
                    "link": link,
                    "published": pub_date,
                }
            )
        return feed_title, items

    atom_entries = [elem for elem in root.iter() if localname(elem.tag) == "entry"]
    for entry in atom_entries:
        title = find_child_text(entry, "title") or "Untitled"
        link = find_atom_link(entry)
        guid = find_child_text(entry, "id")
        published = find_child_text_any(entry, ["published", "updated"])
        entry_id = guid or link or title
        items.append(
            {
                "id": entry_id,
                "title": title,
                "link": link,
                "published": published,
            }
        )
    return feed_title, items


def parse_rss_date(value: str) -> Optional[datetime]:
    """Parse RSS pubDate to datetime. Handles common formats."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    dt = parse_iso_utc(value)
    if dt is not None:
        return dt
    # Try common RSS date formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            # Convert to UTC properly - use astimezone for aware datetimes,
            # replace for naive ones (assume UTC)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            else:
                return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def detect_feed_order(entries: List[Dict[str, str]]) -> Optional[str]:
    """Detect feed order (newest-first or oldest-first) using published dates."""
    first_dt = None
    last_dt = None
    for entry in entries:
        first_dt = parse_rss_date(entry.get("published") or "")
        if first_dt:
            break
    for entry in reversed(entries):
        last_dt = parse_rss_date(entry.get("published") or "")
        if last_dt:
            break
    if first_dt and last_dt:
        return "desc" if first_dt >= last_dt else "asc"
    return None


def format_entry_message(entry: Dict[str, str], feed_title: str) -> str:
    published = entry.get("published")
    message = f"📰 New RSS item from {feed_title}\n"
    message += f"{entry.get('title', 'Untitled')}\n"
    if entry.get("link"):
        message += f"{entry['link']}\n"
    if published:
        message += f"Published: {published}\n"
    return message


def normalize_seen_ids(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    normalized: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in normalized:
            continue
        normalized.append(value)
    return normalized


def get_feed_states(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw_feed_states = state.get("rss_feeds")
    if not isinstance(raw_feed_states, dict):
        raw_feed_states = {}
        state["rss_feeds"] = raw_feed_states
    return {k: v for k, v in raw_feed_states.items() if isinstance(k, str) and isinstance(v, dict)}


def get_feed_state(state: Dict[str, Any], feed_url: str) -> Dict[str, Any]:
    feed_states = get_feed_states(state)
    if feed_url in feed_states:
        state["rss_feeds"] = feed_states
        return feed_states[feed_url]

    feed_state: Dict[str, Any] = {}
    if not feed_states:
        # One-time migration from legacy single-feed keys.
        seen_ids = normalize_seen_ids(state.get("rss_seen_ids"))
        if seen_ids:
            feed_state["rss_seen_ids"] = seen_ids
        last_entry_id = state.get("rss_last_entry_id")
        if isinstance(last_entry_id, str) and last_entry_id.strip():
            feed_state["rss_last_entry_id"] = last_entry_id.strip()
        last_entry_title = state.get("rss_last_entry_title")
        if isinstance(last_entry_title, str) and last_entry_title.strip():
            feed_state["rss_last_entry_title"] = last_entry_title.strip()
    feed_states[feed_url] = feed_state
    state["rss_feeds"] = feed_states
    return feed_state


def default_feed_title(feed_url: str) -> str:
    parsed = urlparse(feed_url)
    return parsed.netloc or feed_url


def process_feed(
    config: Config,
    feed_url: str,
    feed_state: Dict[str, Any],
    now: datetime,
) -> tuple[int, int]:
    xml_text = fetch_feed(feed_url)
    fallback_title = default_feed_title(feed_url)
    feed_title, entries = parse_rss(xml_text, fallback_title)
    if not entries:
        logging.info("No RSS entries found for %s.", feed_url)
        return 0, 0

    entries = entries[: config.rss_max_items]

    entry_ids = [entry.get("id") for entry in entries if entry.get("id")]
    if not entry_ids:
        logging.warning("RSS entries missing identifiers for %s; aborting.", feed_url)
        return 0, 0

    seen_ids = normalize_seen_ids(feed_state.get("rss_seen_ids"))
    if not seen_ids:
        last_entry_id = feed_state.get("rss_last_entry_id")
        order = detect_feed_order(entries)
        if last_entry_id and order:
            try:
                idx = entry_ids.index(last_entry_id)
            except ValueError:
                idx = -1
            if idx >= 0:
                if order == "desc":
                    seen_ids = entry_ids[idx:]
                else:
                    seen_ids = entry_ids[: idx + 1]
        if not seen_ids:
            feed_state["rss_seen_ids"] = entry_ids
            feed_state["rss_last_entry_id"] = entry_ids[0]
            feed_state["rss_last_entry_title"] = entries[0].get("title")
            logging.info("Initialized RSS state for %s with %d entries", feed_url, len(entry_ids))
            return 0, 0

    seen_set = set(seen_ids)
    new_entries = [
        entry for entry in entries if entry.get("id") not in seen_set
    ]
    if not new_entries:
        logging.info("No new RSS entries for %s.", feed_url)
        return 0, 0

    max_age = timedelta(minutes=config.check_interval_minutes)
    eligible_entries: List[Dict[str, str]] = []
    for entry in new_entries:
        published_dt = parse_rss_date(entry.get("published") or "")
        if not published_dt:
            logging.debug(
                "RSS entry missing/invalid published time; including: %s",
                entry.get("id"),
            )
            eligible_entries.append(entry)
            continue
        if published_dt > now:
            logging.info(
                "Skipping RSS entry with future published time: %s (published %s)",
                entry.get("id"),
                entry.get("published"),
            )
            continue
        if now - published_dt > max_age:
            logging.info(
                "RSS entry older than lookback; sending anyway: %s (published %s)",
                entry.get("id"),
                entry.get("published"),
            )
        eligible_entries.append(entry)

    sent_ids: List[str] = []
    failed_sends = 0
    for entry in eligible_entries:
        try:
            message = format_entry_message(entry, feed_title)
            send_telegram_alert(
                config.telegram_bot_token, config.telegram_chat_id, message
            )
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id:
                sent_ids.append(entry_id)
        except Exception as exc:
            logging.warning("Failed to send RSS alert: %s", exc)
            failed_sends += 1
            continue

    max_seen = max(50, config.rss_max_items * 5)
    updated_seen: List[str] = []
    for entry_id in sent_ids:
        if entry_id and entry_id not in updated_seen:
            updated_seen.append(entry_id)
    for entry_id in seen_ids:
        if entry_id and entry_id not in updated_seen:
            updated_seen.append(entry_id)
    feed_state["rss_seen_ids"] = updated_seen[:max_seen]
    feed_state["rss_last_entry_id"] = entry_ids[0]
    feed_state["rss_last_entry_title"] = entries[0].get("title")
    return len(sent_ids), failed_sends


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run RSS feed check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running rssbot: %s", format_run_info(schedule_context))

    try:
        config = load_config(schedule_context)
    except Exception as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    if not config.rss_feed_urls:
        logging.info("[rssbot] skipping: RSS_FEED_URL is not configured")
        return {"success": True, "alerts_sent": 0}

    if chat_id:
        config.telegram_chat_id = chat_id

    state_path = Path(config.rssstate_file)
    state = load_json(state_path, {})

    alerts_sent = 0
    failed_sends = 0
    now = datetime.now(timezone.utc)
    for feed_url in config.rss_feed_urls:
        feed_state = get_feed_state(state, feed_url)
        try:
            feed_alerts_sent, feed_failed_sends = process_feed(
                config, feed_url, feed_state, now
            )
        except Exception as exc:
            logging.warning("Failed to process RSS feed %s: %s", feed_url, exc)
            continue
        alerts_sent += feed_alerts_sent
        failed_sends += feed_failed_sends

    try:
        save_json(state_path, state)
    except Exception as exc:
        logging.warning("Failed to save state file %s: %s", state_path, exc)

    message = None
    if manual_trigger:
        if alerts_sent > 0 and failed_sends > 0:
            message = f"⚠️ Sent {alerts_sent} new RSS item(s), {failed_sends} failed to send"
        elif alerts_sent > 0:
            message = f"✅ Sent {alerts_sent} new RSS item(s)"
        elif failed_sends > 0:
            message = f"⚠️ Found RSS updates but {failed_sends} failed to send"
        else:
            message = "✅ No new RSS items found"

    return {
        "success": True,
        "alerts_sent": alerts_sent,
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
    sys.exit(main())
