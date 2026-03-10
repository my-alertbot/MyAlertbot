import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

from alertbot.common import (
    STATE_DIR,
    calculate_lookback_minutes,
    format_run_info,
    getenv_required,
    load_env_file,
    request_text,
    send_telegram_alert,
)

DEFAULT_STATE_FILE = STATE_DIR / "ytbot_state.json"
# Default interval now comes from configs/schedule.yaml via schedule_context
DEFAULT_YT_CHECK_INTERVAL_MINUTES = 15
DEFAULT_YTDLP_BIN = "yt-dlp"
DEFAULT_YTDLP_TIMEOUT = 60
DEFAULT_YTDLP_MAX_RESULTS = 15

ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    youtube_channel_ids: List[str]
    youtube_check_interval_minutes: int
    state_file: str
    ytdlp_bin: Optional[str]  # None means not found, fall back to RSS directly


@dataclass
class State:
    youtube_last_video_ids: Dict[str, str] = field(default_factory=dict)
    youtube_last_check_time: Optional[datetime] = None


def load_config(schedule_context: Optional[Dict[str, Any]] = None) -> Config:
    def getenv_station_list(key: str) -> List[str]:
        raw = os.getenv(key, "")
        if not raw.strip():
            return []
        items = [item.strip() for item in raw.split(",")]
        return [item for item in items if item]

    interval = calculate_lookback_minutes(
        schedule_context,
        default_minutes=DEFAULT_YT_CHECK_INTERVAL_MINUTES,
        buffer_multiplier=1.0,  # Use exact interval for YouTube
    )

    configured_bin = (os.getenv("YT_YTDLP_BIN") or "").strip() or DEFAULT_YTDLP_BIN
    ytdlp_bin = shutil.which(configured_bin)
    if ytdlp_bin is None and os.path.isfile(configured_bin) and os.access(configured_bin, os.X_OK):
        ytdlp_bin = configured_bin
    if ytdlp_bin is None:
        logging.info("yt-dlp not found in PATH; YouTube checks will use RSS fallback only")

    return Config(
        telegram_bot_token=getenv_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=getenv_required("TELEGRAM_CHAT_ID"),
        youtube_channel_ids=getenv_station_list("YT_CHANNEL_IDS"),
        youtube_check_interval_minutes=interval,
        state_file=os.getenv("YT_STATE_FILE", str(DEFAULT_STATE_FILE)),
        ytdlp_bin=ytdlp_bin,
    )


def load_state(path: str) -> State:
    if not os.path.exists(path):
        return State()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logging.warning("Failed to read state file %s: %s", path, exc)
        return State()

    youtube_last_video_ids = data.get("youtube_last_video_ids") or {}
    youtube_last_check_raw = data.get("youtube_last_check_time")
    youtube_last_check_time = None
    if youtube_last_check_raw:
        try:
            youtube_last_check_time = datetime.fromisoformat(
                youtube_last_check_raw.replace("Z", "+00:00")
            )
        except Exception:
            logging.warning("Invalid youtube_last_check_time in state: %s", youtube_last_check_raw)
            youtube_last_check_time = None

    return State(
        youtube_last_video_ids=dict(youtube_last_video_ids),
        youtube_last_check_time=youtube_last_check_time,
    )


def save_state(path: str, state: State) -> None:
    data: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                data.update(existing)
        except Exception as exc:
            logging.warning("Failed to read state file for merge %s: %s", path, exc)

    data["youtube_last_video_ids"] = state.youtube_last_video_ids or {}
    data["youtube_last_check_time"] = (
        state.youtube_last_check_time.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        if state.youtube_last_check_time
        else None
    )
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def parse_youtube_feed(xml_text: str) -> Tuple[Optional[str], List[Dict[str, str]]]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"Failed to parse YouTube feed XML: {exc}") from exc

    channel_title = root.findtext(f"{{{ATOM_NS}}}title")
    entries: List[Dict[str, str]] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        video_id = entry.findtext(f"{{{YT_NS}}}videoId")
        if not video_id:
            continue
        title = entry.findtext(f"{{{ATOM_NS}}}title") or "Untitled"
        published = entry.findtext(f"{{{ATOM_NS}}}published") or ""
        link_elem = entry.find(f"{{{ATOM_NS}}}link[@rel='alternate']")
        link = None
        if link_elem is not None:
            link = link_elem.attrib.get("href")
        if not link:
            link = f"https://www.youtube.com/watch?v={video_id}"
        entries.append(
            {
                "video_id": video_id,
                "title": title,
                "published": published,
                "link": link,
            }
        )
    return channel_title, entries


def parse_youtube_published(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_youtube_feed_ytdlp(
    channel_id: str,
    ytdlp_bin: str,
    timeout: int = DEFAULT_YTDLP_TIMEOUT,
    max_results: int = DEFAULT_YTDLP_MAX_RESULTS,
) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Fetch YouTube channel feed using yt-dlp."""
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    cmd = [
        ytdlp_bin,
        "--flat-playlist",
        "--playlist-end", str(max_results),
        "--no-warnings",
        "--quiet",
        "-J",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"yt-dlp timed out after {timeout}s for channel {channel_id}") from exc
    except OSError as exc:
        raise RuntimeError(f"yt-dlp could not be started: {exc}") from exc

    if proc.returncode != 0:
        detail = ((proc.stderr or proc.stdout or "").strip().splitlines() or ["no output"])[0][:200]
        raise RuntimeError(f"yt-dlp exited {proc.returncode}: {detail}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp returned invalid JSON: {exc}") from exc

    channel_title = data.get("channel") or data.get("uploader") or data.get("title")
    entries: List[Dict[str, str]] = []
    for item in (data.get("entries") or []):
        if not item:
            continue
        video_id = item.get("id")
        if not video_id:
            continue
        title = item.get("title") or "Untitled"

        # Prefer unix timestamp, fall back to YYYYMMDD upload_date
        published = ""
        ts = item.get("timestamp")
        if ts:
            try:
                published = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            except Exception:
                pass
        if not published:
            upload_date = item.get("upload_date") or ""
            if len(upload_date) == 8 and upload_date.isdigit():
                published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T12:00:00+00:00"

        link = item.get("webpage_url") or item.get("url") or ""
        if not link.startswith("http"):
            link = f"https://www.youtube.com/watch?v={video_id}"

        entries.append({"video_id": video_id, "title": title, "published": published, "link": link})

    return channel_title, entries


def fetch_youtube_feed_rss(channel_id: str) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Fetch YouTube channel feed via RSS (fallback)."""
    url = "https://www.youtube.com/feeds/videos.xml"
    xml_text = request_text(url, params={"channel_id": channel_id})
    return parse_youtube_feed(xml_text)


def fetch_youtube_feed(
    channel_id: str,
    ytdlp_bin: Optional[str] = None,
) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Fetch YouTube channel feed, trying yt-dlp first then falling back to RSS."""
    if ytdlp_bin:
        try:
            return fetch_youtube_feed_ytdlp(channel_id, ytdlp_bin)
        except Exception as exc:
            logging.warning(
                "yt-dlp failed for channel %s, falling back to RSS: %s", channel_id, exc
            )
    return fetch_youtube_feed_rss(channel_id)


def format_youtube_alert(channel_label: str, entry: Dict[str, str]) -> str:
    published = entry.get("published") or "unknown time"
    return (
        f"📺 New YouTube video on {channel_label}\n"
        f"{entry.get('title', 'Untitled')}\n"
        f"{entry.get('link', '')}\n"
        f"Published: {published}\n"
    )


def check_youtube_channels(config: Config, state: State, now: datetime) -> int:
    """Check YouTube channels and return number of alerts sent."""
    if not config.youtube_channel_ids:
        logging.info("No YouTube channels configured; skipping.")
        return 0
    if config.youtube_check_interval_minutes <= 0:
        logging.info("YouTube interval <= 0; skipping.")
        return 0

    alerts_sent = 0

    for channel_id in config.youtube_channel_ids:
        channel_label = channel_id
        try:
            channel_title, entries = fetch_youtube_feed(channel_id, config.ytdlp_bin)
            if channel_title:
                channel_label = channel_title
        except Exception as exc:
            logging.warning("Failed to fetch YouTube feed for %s: %s", channel_id, exc)
            continue

        if not entries:
            logging.info("No YouTube entries found for channel %s", channel_id)
            continue

        last_video_id = (state.youtube_last_video_ids or {}).get(channel_id)
        newest_video_id = entries[0]["video_id"]

        if not last_video_id:
            state.youtube_last_video_ids[channel_id] = newest_video_id
            logging.info("Initialized YouTube channel %s with latest video %s", channel_id, newest_video_id)
            continue

        if newest_video_id == last_video_id:
            logging.debug("No new YouTube videos for channel %s", channel_id)
            continue

        new_entries: List[Dict[str, str]] = []
        for entry in entries:
            if entry["video_id"] == last_video_id:
                break
            new_entries.append(entry)

        if not new_entries:
            new_entries = [entries[0]]

        max_age = timedelta(minutes=config.youtube_check_interval_minutes)
        eligible_entries: List[Dict[str, str]] = []
        for entry in reversed(new_entries):
            published_dt = parse_youtube_published(entry.get("published") or "")
            if not published_dt:
                # No date available (yt-dlp flat-playlist may omit it); include the entry
                # and rely on video ID comparison to prevent duplicates
                eligible_entries.append(entry)
                continue
            if published_dt > now or now - published_dt > max_age:
                logging.info(
                    "Skipping YouTube entry outside interval for channel %s: %s (published %s)",
                    channel_id,
                    entry.get("video_id"),
                    entry.get("published"),
                )
                continue
            eligible_entries.append(entry)

        for entry in eligible_entries:
            try:
                message = format_youtube_alert(channel_label, entry)
                send_telegram_alert(config.telegram_bot_token, config.telegram_chat_id, message)
                alerts_sent += 1
            except Exception as exc:
                logging.warning(
                    "Failed to send YouTube alert for channel %s: %s",
                    channel_id,
                    exc,
                )
                break

        state.youtube_last_video_ids[channel_id] = newest_video_id
        logging.info("Updated YouTube channel %s last video to %s", channel_id, newest_video_id)

    state.youtube_last_check_time = now
    return alerts_sent


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run YouTube check.

    Args:
        manual_trigger: True if triggered via Telegram command
        chat_id: Override chat ID for response
        schedule_context: Context passed by controller for scheduled runs

    Returns:
        dict with success status, message, and alerts_sent count
    """
    logging.debug("Running ytbot: %s", format_run_info(schedule_context))

    try:
        config = load_config(schedule_context)
    except Exception as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    # Override chat_id if provided
    if chat_id:
        config.telegram_chat_id = chat_id

    state = load_state(config.state_file)
    try:
        alerts_sent = check_youtube_channels(config, state, datetime.now(timezone.utc))
    except Exception as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    try:
        save_state(config.state_file, state)
    except Exception as exc:
        logging.warning("Failed to save state file %s: %s", config.state_file, exc)

    message = None
    if manual_trigger:
        if alerts_sent > 0:
            message = f"✅ Found {alerts_sent} new video(s)"
        else:
            message = "✅ No new videos found"

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
    if sys.version_info < (3, 12):
        logging.error("Python 3.12+ is required.")
        return 2

    load_env_file()
    result = run()
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
