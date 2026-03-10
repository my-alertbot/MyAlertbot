#!/usr/bin/env python3
"""Alert on extreme geopolitical shock conditions using multi-signal gates."""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

from alertbot.common import (
    STATE_DIR,
    calculate_lookback_minutes,
    format_run_info,
    getenv_required,
    iso_now,
    load_env_file,
    load_json,
    parse_iso_utc,
    request_json,
    request_text,
    save_json,
    send_telegram_alert,
    setup_logging,
)

DEFAULT_STATE_FILE = STATE_DIR / "geoshockbot.state.json"
DEFAULT_MAX_ITEMS_PER_FEED = 25
DEFAULT_MIN_CONFIRMATIONS = 3
DEFAULT_MIN_HIGH_TRUST = 2
DEFAULT_PERSISTENCE_RUNS = 2
DEFAULT_COOLDOWN_MINUTES = 360
DEFAULT_INFRA_DROP_THRESHOLD_PCT = 5.0
DEFAULT_MARKET_JUMP_THRESHOLD_PCT = 12.0
DEFAULT_VIX_LEVEL_THRESHOLD = 30.0
DEFAULT_OVX_LEVEL_THRESHOLD = 45.0
DEFAULT_MANUAL_LOOKBACK_MINUTES = 180
DEFAULT_MAX_LOOKBACK_MINUTES = 480

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
RIPE_COUNTRY_RESOURCE_STATS_URL = "https://stat.ripe.net/data/country-resource-stats/data.json"


@dataclass(frozen=True)
class NewsSource:
    name: str
    url: str
    region: str  # e.g. "middle_east" | "europe" | "americas" | "asia"
    high_trust: bool


DEFAULT_SOURCES: tuple[NewsSource, ...] = (
    NewsSource(
        name="Al Jazeera",
        url="https://www.aljazeera.com/xml/rss/all.xml",
        region="middle_east",
        high_trust=True,
    ),
    NewsSource(
        name="BBC",
        url="https://feeds.bbci.co.uk/news/world/rss.xml",
        region="europe",
        high_trust=True,
    ),
    NewsSource(
        name="Reuters",
        url="https://feeds.reuters.com/reuters/worldNews",
        region="europe",
        high_trust=True,
    ),
    NewsSource(
        name="Guardian",
        url="https://www.theguardian.com/world/rss",
        region="europe",
        high_trust=True,
    ),
    NewsSource(
        name="NYTimes",
        url="https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        region="americas",
        high_trust=True,
    ),
    NewsSource(
        name="AP",
        url="https://apnews.com/hub/world-news.rss",
        region="americas",
        high_trust=True,
    ),
    NewsSource(
        name="South China Morning Post",
        url="https://www.scmp.com/rss/5/feed",
        region="asia",
        high_trust=False,
    ),
    NewsSource(
        name="The Hindu",
        url="https://www.thehindu.com/news/international/?service=rss",
        region="asia",
        high_trust=False,
    ),
)


ACTOR_PATTERNS: dict[str, tuple[str, ...]] = {
    "iran": (r"\biran\b", r"\biranian\b"),
    "israel": (r"\bisrael\b", r"\bisraeli\b"),
    "united_states": (r"\bunited states\b", r"\bu\.s\.\b", r"\bamerican\b", r"\bwashington\b"),
    "russia": (r"\brussia\b", r"\brussian\b"),
    "ukraine": (r"\bukraine\b", r"\bukrainian\b"),
    "china": (r"\bchina\b", r"\bchinese\b"),
    "taiwan": (r"\btaiwan\b",),
    "india": (r"\bindia\b", r"\bindian\b"),
    "pakistan": (r"\bpakistan\b", r"\bpakistani\b"),
    "north_korea": (r"\bnorth korea\b",),
    "south_korea": (r"\bsouth korea\b",),
    "turkey": (r"\bturkey\b", r"\bturkish\b"),
    "saudi_arabia": (r"\bsaudi arabia\b", r"\bsaudi\b"),
    "qatar": (r"\bqatar\b", r"\bqatari\b"),
    "bahrain": (r"\bbahrain\b", r"\bbahraini\b"),
    "uae": (r"\bu\.a\.e\.\b", r"\buae\b", r"\bunited arab emirates\b"),
    "iraq": (r"\biraq\b", r"\biraqi\b"),
    "syria": (r"\bsyria\b", r"\bsyrian\b"),
    "lebanon": (r"\blebanon\b", r"\blebanese\b"),
}

ACTOR_TO_COUNTRY_CODE: dict[str, str] = {
    "iran": "IR",
    "israel": "IL",
    "russia": "RU",
    "ukraine": "UA",
    "china": "CN",
    "taiwan": "TW",
    "india": "IN",
    "pakistan": "PK",
    "north_korea": "KP",
    "south_korea": "KR",
    "turkey": "TR",
    "saudi_arabia": "SA",
    "qatar": "QA",
    "bahrain": "BH",
    "uae": "AE",
    "iraq": "IQ",
    "syria": "SY",
    "lebanon": "LB",
}

ACTION_TERMS: tuple[str, ...] = (
    "attack",
    "attacks",
    "attacked",
    "strike",
    "strikes",
    "struck",
    "airstrike",
    "bomb",
    "bombing",
    "invasion",
    "retaliation",
    "retaliatory",
    "missile",
    "missiles",
    "barrage",
    "military operation",
    "drone strike",
)
SEVERE_ACTION_TERMS: tuple[str, ...] = (
    "missile",
    "missiles",
    "barrage",
    "airstrike",
    "invasion",
    "bombing",
    "war",
)
SEVERITY_TERMS: tuple[str, ...] = (
    "airspace closed",
    "state of war",
    "declared war",
    "major attack",
    "ballistic",
    "military base",
    "explosions heard",
    "shelter in place",
    "emergency committee",
    "casualties",
    "killed",
    "wounded",
    "evacuation",
)


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    state_file: Path
    lookback_minutes: int
    max_items_per_feed: int
    min_confirmations: int
    min_high_trust: int
    persistence_runs: int
    cooldown_minutes: int
    infra_drop_threshold_pct: float
    market_jump_threshold_pct: float
    vix_level_threshold: float
    ovx_level_threshold: float


@dataclass
class NewsSignal:
    source_name: str
    region: str
    high_trust: bool
    title: str
    link: str
    published_raw: str
    published_at: datetime | None
    actors: set[str]
    action_terms: set[str]
    severity_terms: set[str]

    @property
    def is_strong(self) -> bool:
        severe_action_match = any(term in self.action_terms for term in SEVERE_ACTION_TERMS)
        return severe_action_match or bool(self.severity_terms)


def _get_int_env(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _get_float_env(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def load_config(schedule_context: dict[str, Any] | None = None) -> Config:
    lookback = calculate_lookback_minutes(
        schedule_context,
        default_minutes=DEFAULT_MANUAL_LOOKBACK_MINUTES,
        buffer_multiplier=1.5,
        max_minutes=DEFAULT_MAX_LOOKBACK_MINUTES,
    )
    lookback = _get_int_env("GEOSHOCK_NEWS_LOOKBACK_MINUTES", lookback)

    return Config(
        telegram_bot_token=getenv_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=getenv_required("TELEGRAM_CHAT_ID"),
        state_file=Path(os.getenv("GEOSHOCK_STATE_FILE", str(DEFAULT_STATE_FILE))).expanduser(),
        lookback_minutes=max(10, lookback),
        max_items_per_feed=max(5, _get_int_env("GEOSHOCK_MAX_ITEMS", DEFAULT_MAX_ITEMS_PER_FEED)),
        min_confirmations=max(2, _get_int_env("GEOSHOCK_MIN_CONFIRMATIONS", DEFAULT_MIN_CONFIRMATIONS)),
        min_high_trust=max(1, _get_int_env("GEOSHOCK_MIN_HIGH_TRUST", DEFAULT_MIN_HIGH_TRUST)),
        persistence_runs=max(1, _get_int_env("GEOSHOCK_PERSISTENCE_RUNS", DEFAULT_PERSISTENCE_RUNS)),
        cooldown_minutes=max(10, _get_int_env("GEOSHOCK_COOLDOWN_MINUTES", DEFAULT_COOLDOWN_MINUTES)),
        infra_drop_threshold_pct=max(
            1.0,
            _get_float_env("GEOSHOCK_INFRA_DROP_THRESHOLD_PCT", DEFAULT_INFRA_DROP_THRESHOLD_PCT),
        ),
        market_jump_threshold_pct=max(
            1.0,
            _get_float_env("GEOSHOCK_MARKET_JUMP_THRESHOLD_PCT", DEFAULT_MARKET_JUMP_THRESHOLD_PCT),
        ),
        vix_level_threshold=max(
            10.0,
            _get_float_env("GEOSHOCK_VIX_LEVEL_THRESHOLD", DEFAULT_VIX_LEVEL_THRESHOLD),
        ),
        ovx_level_threshold=max(
            10.0,
            _get_float_env("GEOSHOCK_OVX_LEVEL_THRESHOLD", DEFAULT_OVX_LEVEL_THRESHOLD),
        ),
    )


def _normalize_text(value: str | None) -> str:
    return (value or "").strip()


def _localname(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child_text(elem: ElementTree.Element, name: str) -> str:
    for child in list(elem):
        if _localname(child.tag) == name:
            return _normalize_text(child.text)
    return ""


def _find_child_text_any(elem: ElementTree.Element, names: tuple[str, ...]) -> str:
    for name in names:
        value = _find_child_text(elem, name)
        if value:
            return value
    return ""


def _find_atom_link(entry: ElementTree.Element) -> str:
    links = [child for child in list(entry) if _localname(child.tag) == "link"]
    for link_elem in links:
        rel = _normalize_text(link_elem.attrib.get("rel")).lower()
        href = _normalize_text(link_elem.attrib.get("href"))
        if not href:
            continue
        if rel in ("", "alternate"):
            return href
    for link_elem in links:
        href = _normalize_text(link_elem.attrib.get("href"))
        if href:
            return href
    return ""


def _default_feed_title(feed_url: str) -> str:
    parsed = urlparse(feed_url)
    return parsed.netloc or feed_url


def parse_feed(xml_text: str, fallback_title: str) -> tuple[str, list[dict[str, str]]]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise RuntimeError(f"Failed to parse feed XML: {exc}") from exc

    feed_title = _find_child_text(root, "title") or fallback_title
    for elem in root.iter():
        if _localname(elem.tag) == "channel":
            channel_title = _find_child_text(elem, "title")
            if channel_title:
                feed_title = channel_title
            break

    entries: list[dict[str, str]] = []
    rss_items = [elem for elem in root.iter() if _localname(elem.tag) == "item"]
    if rss_items:
        for item in rss_items:
            title = _find_child_text(item, "title") or "Untitled"
            link = _find_child_text(item, "link")
            guid = _find_child_text(item, "guid")
            published = _find_child_text_any(item, ("pubDate", "published", "updated"))
            summary = _find_child_text_any(item, ("description", "summary"))
            entry_id = guid or link or title
            entries.append(
                {
                    "id": entry_id,
                    "title": title,
                    "link": link,
                    "published": published,
                    "summary": summary,
                }
            )
        return feed_title, entries

    atom_entries = [elem for elem in root.iter() if _localname(elem.tag) == "entry"]
    for entry in atom_entries:
        title = _find_child_text(entry, "title") or "Untitled"
        link = _find_atom_link(entry)
        guid = _find_child_text(entry, "id")
        published = _find_child_text_any(entry, ("published", "updated"))
        summary = _find_child_text_any(entry, ("summary", "content"))
        entry_id = guid or link or title
        entries.append(
            {
                "id": entry_id,
                "title": title,
                "link": link,
                "published": published,
                "summary": summary,
            }
        )
    return feed_title, entries


def parse_feed_datetime(value: str) -> datetime | None:
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

    return parse_iso_utc(value)


def classify_text(text: str) -> tuple[bool, bool, set[str], set[str], set[str]]:
    normalized = text.lower()

    actors: set[str] = set()
    for actor, patterns in ACTOR_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, normalized):
                actors.add(actor)
                break

    action_hits = {term for term in ACTION_TERMS if term in normalized}
    severity_hits = {term for term in SEVERITY_TERMS if term in normalized}

    has_action = bool(action_hits)
    has_actor_pair = len(actors) >= 2
    candidate = has_action and has_actor_pair

    severe_action_match = any(term in action_hits for term in SEVERE_ACTION_TERMS)
    strong_candidate = candidate and (severe_action_match or bool(severity_hits))
    return candidate, strong_candidate, actors, action_hits, severity_hits


def collect_news_signals(
    sources: tuple[NewsSource, ...],
    lookback_minutes: int,
    max_items_per_feed: int,
) -> tuple[list[NewsSignal], list[str]]:
    now = datetime.now(timezone.utc)
    lookback = timedelta(minutes=lookback_minutes)
    dedupe: set[tuple[str, str]] = set()
    signals: list[NewsSignal] = []
    errors: list[str] = []

    for source in sources:
        try:
            xml_text = request_text(source.url)
            _, entries = parse_feed(xml_text, _default_feed_title(source.url))
        except Exception as exc:
            errors.append(f"{source.name}: {exc}")
            continue

        for entry in entries[:max_items_per_feed]:
            title = _normalize_text(entry.get("title"))
            summary = _normalize_text(entry.get("summary"))
            if not title:
                continue
            dedupe_key = (source.name, title.lower())
            if dedupe_key in dedupe:
                continue
            dedupe.add(dedupe_key)

            published_raw = _normalize_text(entry.get("published"))
            published_at = parse_feed_datetime(published_raw)
            if published_at is not None and (now - published_at) > lookback:
                continue

            body = f"{title}. {summary}".strip()
            candidate, _strong_candidate, actors, action_hits, severity_hits = classify_text(body)
            if not candidate:
                continue

            signals.append(
                NewsSignal(
                    source_name=source.name,
                    region=source.region,
                    high_trust=source.high_trust,
                    title=title,
                    link=_normalize_text(entry.get("link")),
                    published_raw=published_raw,
                    published_at=published_at,
                    actors=actors,
                    action_terms=action_hits,
                    severity_terms=severity_hits,
                )
            )

    return signals, errors


def build_news_metrics(
    signals: list[NewsSignal],
    min_confirmations: int,
    min_high_trust: int,
) -> dict[str, Any]:
    sources = sorted({signal.source_name for signal in signals})
    regions = sorted({signal.region for signal in signals})
    high_trust_sources = sorted({signal.source_name for signal in signals if signal.high_trust})
    strong_count = sum(1 for signal in signals if signal.is_strong)

    actor_counts: dict[str, int] = {}
    for signal in signals:
        for actor in signal.actors:
            actor_counts[actor] = actor_counts.get(actor, 0) + 1
    sorted_actors = sorted(actor_counts.items(), key=lambda item: (-item[1], item[0]))

    has_cross_region = len(regions) >= 2
    news_gate = (
        len(sources) >= min_confirmations
        and has_cross_region
        and len(high_trust_sources) >= min_high_trust
    )
    severity_gate = strong_count > 0
    high_news_intensity = (
        len(sources) >= max(min_confirmations + 2, 5)
        and strong_count >= 2
        and len(high_trust_sources) >= min_high_trust
    )

    top_actor_keys = [actor for actor, _count in sorted_actors[:3]]
    fingerprint = f"{','.join(sources)}::{','.join(top_actor_keys)}"

    return {
        "signals": signals,
        "source_count": len(sources),
        "sources": sources,
        "region_count": len(regions),
        "regions": regions,
        "high_trust_count": len(high_trust_sources),
        "high_trust_sources": high_trust_sources,
        "strong_count": strong_count,
        "actor_counts": actor_counts,
        "sorted_actors": sorted_actors,
        "news_gate": news_gate,
        "severity_gate": severity_gate,
        "high_news_intensity": high_news_intensity,
        "fingerprint": fingerprint,
    }


def _country_codes_from_actors(sorted_actors: list[tuple[str, int]]) -> list[str]:
    codes: list[str] = []
    for actor, _count in sorted_actors:
        code = ACTOR_TO_COUNTRY_CODE.get(actor)
        if not code or code in codes:
            continue
        codes.append(code)
    if not codes:
        codes = ["IR", "IL", "AE"]
    return codes[:4]


def _fetch_ripe_drop(country_code: str, now: datetime) -> dict[str, Any] | None:
    start = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M")
    end = now.strftime("%Y-%m-%dT%H:%M")
    payload = request_json(
        RIPE_COUNTRY_RESOURCE_STATS_URL,
        params={
            "resource": country_code.lower(),
            "starttime": start,
            "endtime": end,
            "resolution": "1h",
        },
    )
    raw_stats = payload.get("data", {}).get("stats", [])
    if not isinstance(raw_stats, list):
        return None

    v4_values: list[float] = []
    asn_values: list[float] = []
    for row in raw_stats:
        if not isinstance(row, dict):
            continue
        v4 = row.get("v4_prefixes_ris")
        asn = row.get("asns_ris")
        if isinstance(v4, (int, float)) and v4 > 0:
            v4_values.append(float(v4))
        if isinstance(asn, (int, float)) and asn > 0:
            asn_values.append(float(asn))

    if len(v4_values) < 4:
        return None

    pre_v4 = v4_values[:-2][-6:] if len(v4_values) > 2 else v4_values[:-1]
    post_v4 = v4_values[-2:]
    if not pre_v4 or not post_v4:
        return None

    pre_v4_avg = sum(pre_v4) / len(pre_v4)
    post_v4_avg = sum(post_v4) / len(post_v4)
    if pre_v4_avg <= 0:
        return None

    drop_pct = ((pre_v4_avg - post_v4_avg) / pre_v4_avg) * 100.0

    pre_asn_avg = None
    post_asn_avg = None
    asn_drop_pct = None
    if len(asn_values) >= 4:
        pre_asn = asn_values[:-2][-6:] if len(asn_values) > 2 else asn_values[:-1]
        post_asn = asn_values[-2:]
        if pre_asn and post_asn:
            pre_asn_avg = sum(pre_asn) / len(pre_asn)
            post_asn_avg = sum(post_asn) / len(post_asn)
            if pre_asn_avg > 0:
                asn_drop_pct = ((pre_asn_avg - post_asn_avg) / pre_asn_avg) * 100.0

    return {
        "country_code": country_code,
        "pre_v4_avg": pre_v4_avg,
        "post_v4_avg": post_v4_avg,
        "drop_pct": drop_pct,
        "pre_asn_avg": pre_asn_avg,
        "post_asn_avg": post_asn_avg,
        "asn_drop_pct": asn_drop_pct,
    }


def assess_infrastructure_signal(
    sorted_actors: list[tuple[str, int]],
    drop_threshold_pct: float,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    country_codes = _country_codes_from_actors(sorted_actors)
    details: list[dict[str, Any]] = []
    errors: list[str] = []

    for code in country_codes:
        try:
            result = _fetch_ripe_drop(code, now)
        except Exception as exc:
            errors.append(f"{code}: {exc}")
            continue
        if result is not None:
            details.append(result)

    details.sort(key=lambda item: item["drop_pct"], reverse=True)
    top = details[0] if details else None
    triggered = bool(top and top["drop_pct"] >= drop_threshold_pct)
    return {
        "triggered": triggered,
        "details": details,
        "top": top,
        "country_codes_checked": country_codes,
        "errors": errors,
    }


def _parse_fred_series_change(series_id: str) -> dict[str, float] | None:
    csv_text = request_text(FRED_CSV_URL, params={"id": series_id})
    reader = csv.DictReader(io.StringIO(csv_text))
    values: list[float] = []
    for row in reader:
        raw_value = (row.get(series_id) or "").strip()
        if not raw_value or raw_value == ".":
            continue
        try:
            values.append(float(raw_value))
        except ValueError:
            continue
    if len(values) < 2:
        return None
    latest = values[-1]
    previous = values[-2]
    if previous <= 0:
        return None
    pct_change = ((latest - previous) / previous) * 100.0
    return {"latest": latest, "previous": previous, "pct_change": pct_change}


def assess_market_signal(config: Config) -> dict[str, Any]:
    errors: list[str] = []
    series: dict[str, dict[str, float] | None] = {"VIXCLS": None, "OVXCLS": None}

    for series_id in series:
        try:
            series[series_id] = _parse_fred_series_change(series_id)
        except Exception as exc:
            errors.append(f"{series_id}: {exc}")

    vix = series["VIXCLS"]
    ovx = series["OVXCLS"]

    vix_trigger = bool(
        vix
        and (
            vix["pct_change"] >= config.market_jump_threshold_pct
            or vix["latest"] >= config.vix_level_threshold
        )
    )
    ovx_trigger = bool(
        ovx
        and (
            ovx["pct_change"] >= config.market_jump_threshold_pct
            or ovx["latest"] >= config.ovx_level_threshold
        )
    )

    return {
        "triggered": vix_trigger or ovx_trigger,
        "vix": vix,
        "ovx": ovx,
        "errors": errors,
    }


def evaluate_trigger(
    news_metrics: dict[str, Any],
    infra_signal: dict[str, Any],
    market_signal: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if news_metrics.get("news_gate"):
        reasons.append("news-confirmation")
    if news_metrics.get("severity_gate"):
        reasons.append("severity-confirmation")
    if infra_signal.get("triggered"):
        reasons.append("infrastructure-disruption")
    if market_signal.get("triggered"):
        reasons.append("market-volatility")
    if news_metrics.get("high_news_intensity"):
        reasons.append("high-news-intensity")

    trigger_ready = bool(
        news_metrics.get("news_gate")
        and news_metrics.get("severity_gate")
        and (
            infra_signal.get("triggered")
            or market_signal.get("triggered")
            or news_metrics.get("high_news_intensity")
        )
    )
    return trigger_ready, reasons


def _format_actor(actor: str) -> str:
    return actor.replace("_", " ")


def _format_infra_line(infra_signal: dict[str, Any]) -> str:
    top = infra_signal.get("top")
    if not top:
        return "Infrastructure signal: unavailable or no meaningful drop"
    asn_drop = top.get("asn_drop_pct")
    asn_text = ""
    if isinstance(asn_drop, (int, float)):
        asn_text = f", ASN drop {asn_drop:.1f}%"
    return (
        "Infrastructure signal: "
        f"{top['country_code']} prefix visibility drop {top['drop_pct']:.1f}% "
        f"({top['pre_v4_avg']:.0f} -> {top['post_v4_avg']:.0f}{asn_text})"
    )


def _format_market_line(market_signal: dict[str, Any]) -> str:
    vix = market_signal.get("vix")
    ovx = market_signal.get("ovx")
    if not vix and not ovx:
        return "Market signal: unavailable"

    parts: list[str] = []
    if vix:
        parts.append(f"VIX {vix['latest']:.2f} ({vix['pct_change']:+.1f}% d/d)")
    if ovx:
        parts.append(f"OVX {ovx['latest']:.2f} ({ovx['pct_change']:+.1f}% d/d)")
    return "Market signal: " + ", ".join(parts)


def format_alert_message(
    news_metrics: dict[str, Any],
    infra_signal: dict[str, Any],
    market_signal: dict[str, Any],
    reasons: list[str],
    consecutive_hits: int,
    persistence_runs: int,
) -> str:
    sorted_actors = news_metrics.get("sorted_actors", [])
    top_actors = ", ".join(_format_actor(actor) for actor, _count in sorted_actors[:4]) or "n/a"
    signals: list[NewsSignal] = news_metrics.get("signals", [])

    lines = [
        "🚨 Extreme Geopolitical Shock Signal",
        "",
        f"Gates: {', '.join(reasons) if reasons else 'n/a'}",
        f"Persistence: {consecutive_hits}/{persistence_runs} consecutive runs",
        (
            "News confirmation: "
            f"{news_metrics.get('source_count', 0)} sources, "
            f"regions={','.join(news_metrics.get('regions', [])) or 'n/a'}, "
            f"high-trust={news_metrics.get('high_trust_count', 0)}"
        ),
        f"Top actors: {top_actors}",
        _format_infra_line(infra_signal),
        _format_market_line(market_signal),
        "",
        "Sample headlines:",
    ]

    for signal in signals[:6]:
        lines.append(f"- [{signal.source_name}] {signal.title}")
    lines.append("")
    lines.append(f"Detected at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)


def format_status_message(
    trigger_ready: bool,
    reasons: list[str],
    news_metrics: dict[str, Any],
    infra_signal: dict[str, Any],
    market_signal: dict[str, Any],
    consecutive_hits: int,
    persistence_runs: int,
    feed_errors: list[str],
) -> str:
    status = "TRIGGER READY" if trigger_ready else "No trigger"
    lines = [
        f"🛰️ Geoshock status: {status}",
        (
            "News gate: "
            f"{news_metrics.get('source_count', 0)} sources, "
            f"{news_metrics.get('high_trust_count', 0)} high-trust, "
            f"regions={','.join(news_metrics.get('regions', [])) or 'n/a'}, "
            f"strong_items={news_metrics.get('strong_count', 0)}"
        ),
        f"Infrastructure gate: {'YES' if infra_signal.get('triggered') else 'NO'}",
        f"Market gate: {'YES' if market_signal.get('triggered') else 'NO'}",
        f"Persistence: {consecutive_hits}/{persistence_runs}",
        f"Reasons: {', '.join(reasons) if reasons else 'n/a'}",
        _format_infra_line(infra_signal),
        _format_market_line(market_signal),
    ]
    if feed_errors:
        lines.append(f"Feed errors: {len(feed_errors)} source(s) failed")
    return "\n".join(lines)


def _cooldown_elapsed(last_alert_at: str | None, cooldown_minutes: int) -> bool:
    if not last_alert_at:
        return True
    dt = parse_iso_utc(last_alert_at)
    if dt is None:
        return True
    return datetime.now(timezone.utc) - dt >= timedelta(minutes=cooldown_minutes)


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logging.debug("Running geoshockbot: %s", format_run_info(schedule_context))

    try:
        config = load_config(schedule_context)
    except Exception as exc:
        return {"success": False, "alerts_sent": 0, "error": str(exc)}

    if chat_id:
        config.telegram_chat_id = chat_id

    state = load_json(
        config.state_file,
        {
            "consecutive_hits": 0,
            "last_fingerprint": "",
            "last_alert_at": None,
            "last_alert_fingerprint": "",
            "last_checked_at": None,
        },
    )

    signals, feed_errors = collect_news_signals(
        DEFAULT_SOURCES,
        config.lookback_minutes,
        config.max_items_per_feed,
    )
    news_metrics = build_news_metrics(
        signals,
        min_confirmations=config.min_confirmations,
        min_high_trust=config.min_high_trust,
    )

    infra_signal = assess_infrastructure_signal(
        news_metrics["sorted_actors"],
        drop_threshold_pct=config.infra_drop_threshold_pct,
    )
    market_signal = assess_market_signal(config)

    trigger_ready, reasons = evaluate_trigger(news_metrics, infra_signal, market_signal)
    fingerprint = news_metrics.get("fingerprint", "")

    last_fingerprint = state.get("last_fingerprint", "")
    previous_hits = int(state.get("consecutive_hits", 0) or 0)
    if trigger_ready:
        consecutive_hits = previous_hits + 1 if fingerprint and fingerprint == last_fingerprint else 1
    else:
        consecutive_hits = 0

    state["consecutive_hits"] = consecutive_hits
    state["last_fingerprint"] = fingerprint if trigger_ready else ""
    state["last_checked_at"] = iso_now()
    state["last_evaluation"] = {
        "trigger_ready": trigger_ready,
        "reasons": reasons,
        "source_count": news_metrics.get("source_count", 0),
        "high_trust_count": news_metrics.get("high_trust_count", 0),
        "region_count": news_metrics.get("region_count", 0),
        "strong_count": news_metrics.get("strong_count", 0),
    }

    alerts_sent = 0
    should_send_alert = bool(
        trigger_ready
        and consecutive_hits >= config.persistence_runs
        and (
            state.get("last_alert_fingerprint") != fingerprint
            or _cooldown_elapsed(state.get("last_alert_at"), config.cooldown_minutes)
        )
    )

    if should_send_alert:
        message = format_alert_message(
            news_metrics,
            infra_signal,
            market_signal,
            reasons,
            consecutive_hits=consecutive_hits,
            persistence_runs=config.persistence_runs,
        )
        try:
            send_telegram_alert(config.telegram_bot_token, config.telegram_chat_id, message)
            alerts_sent = 1
            state["last_alert_at"] = iso_now()
            state["last_alert_fingerprint"] = fingerprint
        except Exception as exc:
            logging.warning("[geoshockbot] Failed to send alert: %s", exc)
            save_json(config.state_file, state)
            return {"success": False, "alerts_sent": 0, "error": str(exc)}

    save_json(config.state_file, state)

    status_message = format_status_message(
        trigger_ready=trigger_ready,
        reasons=reasons,
        news_metrics=news_metrics,
        infra_signal=infra_signal,
        market_signal=market_signal,
        consecutive_hits=consecutive_hits,
        persistence_runs=config.persistence_runs,
        feed_errors=feed_errors,
    )
    if manual_trigger:
        if alerts_sent == 0:
            try:
                send_telegram_alert(config.telegram_bot_token, config.telegram_chat_id, status_message)
                alerts_sent = 1
            except Exception as exc:
                return {"success": False, "alerts_sent": 0, "error": str(exc)}
        return {"success": True, "alerts_sent": alerts_sent, "message": status_message}

    return {"success": True, "alerts_sent": alerts_sent}


def main() -> int:
    load_env_file()
    setup_logging()
    result = run()
    if not result.get("success", False):
        logging.error(result.get("error", "geoshockbot failed"))
        return 1
    logging.info("geoshockbot finished (alerts_sent=%s)", result.get("alerts_sent", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
