#!/usr/bin/env python3
"""Alert when newly discovered subdomains resolve in DNS using subfinder."""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
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
    save_json,
    send_telegram_alert,
)

DEFAULT_STATE_FILE = STATE_DIR / "newsubdomainbot.state.json"
DEFAULT_SUBFINDER_BIN = "subfinder"
DEFAULT_SUBFINDER_TIMEOUT_SECONDS = 60
DEFAULT_ALERT_ON_FIRST_RUN = False


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    domains: list[str]
    state_file: Path
    subfinder_bin: str
    subfinder_timeout_seconds: int
    alert_on_first_run: bool


@dataclass(frozen=True)
class DnsResolution:
    ipv4: list[str]
    ipv6: list[str]
    aliases: list[str]


def getenv_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {key} must be an integer") from exc


def getenv_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if domain.startswith("*."):
        domain = domain[2:]
    if not domain:
        raise ValueError("NEWSUBDOMAINBOT_DOMAIN must not be empty")
    if " " in domain:
        raise ValueError("NEWSUBDOMAINBOT_DOMAIN must be a valid domain name")
    return domain


def parse_domain_list(raw: str) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        if not part.strip():
            continue
        domain = normalize_domain(part)
        if domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    if not domains:
        raise ValueError("NEWSUBDOMAINBOT_DOMAIN must include at least one valid domain")
    return domains


def normalize_hostname(value: str) -> str:
    return value.strip().lower().rstrip(".")


def load_config(_schedule_context: dict[str, Any] | None = None) -> Config:
    return Config(
        telegram_bot_token=getenv_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=getenv_required("TELEGRAM_CHAT_ID"),
        domains=parse_domain_list(os.getenv("NEWSUBDOMAINBOT_DOMAIN", "")),
        state_file=Path(os.getenv("NEWSUBDOMAINBOT_STATE_FILE", str(DEFAULT_STATE_FILE))),
        subfinder_bin=(os.getenv("NEWSUBDOMAINBOT_SUBFINDER_BIN", DEFAULT_SUBFINDER_BIN) or "").strip()
        or DEFAULT_SUBFINDER_BIN,
        subfinder_timeout_seconds=max(
            1,
            getenv_int("NEWSUBDOMAINBOT_SUBFINDER_TIMEOUT_SECONDS", DEFAULT_SUBFINDER_TIMEOUT_SECONDS),
        ),
        alert_on_first_run=getenv_bool("NEWSUBDOMAINBOT_ALERT_ON_FIRST_RUN", DEFAULT_ALERT_ON_FIRST_RUN),
    )


def resolve_subfinder_command(configured_bin: str) -> str:
    if os.path.sep in configured_bin or (os.path.altsep and os.path.altsep in configured_bin):
        path = Path(configured_bin).expanduser()
        if not path.exists():
            raise RuntimeError(
                "subfinder is not installed or not reachable at "
                f"{path}. Install projectdiscovery/subfinder or set NEWSUBDOMAINBOT_SUBFINDER_BIN."
            )
        if not os.access(path, os.X_OK):
            raise RuntimeError(
                f"subfinder exists at {path} but is not executable. "
                "Fix permissions or set NEWSUBDOMAINBOT_SUBFINDER_BIN."
            )
        return str(path)

    resolved = shutil.which(configured_bin)
    if not resolved:
        raise RuntimeError(
            "subfinder is not installed or not in PATH. "
            "Install projectdiscovery/subfinder or set NEWSUBDOMAINBOT_SUBFINDER_BIN."
        )
    return resolved


def run_subfinder(domain: str, subfinder_bin: str, timeout_seconds: int) -> list[str]:
    cmd = [subfinder_bin, "-d", domain, "-silent"]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "subfinder timed out after "
            f"{timeout_seconds}s while scanning {domain}. "
            "Try a longer NEWSUBDOMAINBOT_SUBFINDER_TIMEOUT_SECONDS."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"subfinder could not be started: {exc}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = (stderr or stdout or "no error output").splitlines()[0][:240]
        raise RuntimeError(f"subfinder failed with exit code {proc.returncode}: {detail}")

    names: list[str] = []
    seen: set[str] = set()
    for raw_line in (proc.stdout or "").splitlines():
        host = normalize_hostname(raw_line)
        if not host or host.startswith("*."):
            continue
        if host not in seen:
            seen.add(host)
            names.append(host)
    return names


def resolve_dns(hostname: str) -> DnsResolution | None:
    ipv4: set[str] = set()
    ipv6: set[str] = set()
    aliases: set[str] = set()

    try:
        _canonical, alias_list, v4_list = socket.gethostbyname_ex(hostname)
        for alias in alias_list:
            alias_name = normalize_hostname(alias)
            if alias_name and alias_name != hostname:
                aliases.add(alias_name)
        for ip in v4_list:
            if ip:
                ipv4.add(ip)
    except OSError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        for family, _socktype, _proto, _canonname, sockaddr in infos:
            if family == socket.AF_INET and sockaddr:
                ipv4.add(str(sockaddr[0]))
            elif family == socket.AF_INET6 and sockaddr:
                ipv6.add(str(sockaddr[0]))
    except OSError:
        pass

    if not ipv4 and not ipv6 and not aliases:
        return None
    return DnsResolution(ipv4=sorted(ipv4), ipv6=sorted(ipv6), aliases=sorted(aliases))


def discover_verified_subdomains(
    domain: str,
    subfinder_bin: str,
    timeout_seconds: int,
) -> dict[str, DnsResolution]:
    discovered = run_subfinder(domain, subfinder_bin, timeout_seconds)
    verified: dict[str, DnsResolution] = {}
    suffix = f".{domain}"

    for host in discovered:
        if host == domain or not host.endswith(suffix):
            continue
        resolution = resolve_dns(host)
        if resolution is None:
            continue
        verified[host] = resolution

    return verified


def format_dns_resolution_lines(resolution: DnsResolution) -> list[str]:
    lines: list[str] = []
    if resolution.ipv4:
        lines.append(f"A: {', '.join(resolution.ipv4)}")
    if resolution.ipv6:
        lines.append(f"AAAA: {', '.join(resolution.ipv6)}")
    if resolution.aliases:
        lines.append(f"Aliases: {', '.join(resolution.aliases)}")
    if not lines:
        lines.append("DNS: resolved (record details unavailable)")
    return lines


def format_alert_message(domain: str, hostname: str, resolution: DnsResolution) -> str:
    lines = [
        f"⚠️ New subdomain detected for {domain}",
        f"Host: {hostname}",
        "Verified via DNS lookup",
    ]
    lines.extend(format_dns_resolution_lines(resolution))
    return "\n".join(lines)


def build_state_snapshot(verified: dict[str, DnsResolution]) -> dict[str, dict[str, list[str]]]:
    snapshot: dict[str, dict[str, list[str]]] = {}
    for host in sorted(verified):
        resolution = verified[host]
        snapshot[host] = {
            "a": list(resolution.ipv4),
            "aaaa": list(resolution.ipv6),
            "aliases": list(resolution.aliases),
        }
    return snapshot


def _migrate_legacy_state(state: dict[str, Any]) -> dict[str, Any]:
    """Convert single-domain state schema to per-domain schema in-place."""
    domains_state = state.get("domains")
    if isinstance(domains_state, dict):
        return domains_state

    migrated: dict[str, Any] = {}
    legacy_domain_raw = state.get("domain")
    legacy_known = state.get("known_subdomains")
    if isinstance(legacy_domain_raw, str) and isinstance(legacy_known, list):
        try:
            legacy_domain = normalize_domain(legacy_domain_raw)
        except ValueError:
            legacy_domain = None
        if legacy_domain:
            migrated[legacy_domain] = {
                "known_subdomains": legacy_known,
                "last_snapshot": state.get("last_snapshot") if isinstance(state.get("last_snapshot"), dict) else {},
                "last_count": state.get("last_count"),
                "last_run": state.get("last_run"),
            }

    state["domains"] = migrated
    return migrated


def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logging.debug("Running newsubdomainbot: %s", format_run_info(schedule_context))

    try:
        config = load_config(schedule_context)
    except Exception as exc:
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    if not config.domains:
        logging.info("[newsubdomainbot] skipping: NEWSUBDOMAINBOT_DOMAIN is not configured")
        return {"success": True, "alerts_sent": 0}

    if chat_id:
        config.telegram_chat_id = chat_id

    configured_domains = config.domains
    state_file_exists = config.state_file.exists()
    state = load_json(
        config.state_file,
        {
            "version": 2,
            "domains": {},
            "last_run": None,
        },
        strict=False,
    )
    domains_state = _migrate_legacy_state(state)

    try:
        resolved_subfinder_bin = resolve_subfinder_command(config.subfinder_bin)
    except Exception as exc:
        logging.warning("[newsubdomainbot] %s", exc)
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    alerts_sent = 0
    domains_processed = 0
    total_verified_hosts = 0
    domain_errors: list[str] = []

    for domain in configured_domains:
        domain_state = domains_state.get(domain) if isinstance(domains_state.get(domain), dict) else {}
        previous_known_raw = domain_state.get("known_subdomains")
        previous_known_set = set(previous_known_raw) if isinstance(previous_known_raw, list) else set()
        first_run = (not state_file_exists and len(configured_domains) == 1) or (
            domain not in domains_state
        ) or (not isinstance(previous_known_raw, list))

        try:
            verified = discover_verified_subdomains(
                domain=domain,
                subfinder_bin=resolved_subfinder_bin,
                timeout_seconds=config.subfinder_timeout_seconds,
            )
        except Exception as exc:
            err = f"{domain}: {exc}"
            logging.warning("[newsubdomainbot] %s", err)
            domain_errors.append(err)
            continue

        domains_processed += 1
        current_hosts = sorted(verified)
        total_verified_hosts += len(current_hosts)
        new_hosts: list[str] = []

        if first_run and not config.alert_on_first_run:
            logging.info(
                "[newsubdomainbot] seeding state for %s (%s verified subdomains, no alerts)",
                domain,
                len(current_hosts),
            )
        else:
            new_hosts = [host for host in current_hosts if host not in previous_known_set]

        domain_alerts_sent = 0
        for host in new_hosts:
            try:
                send_telegram_alert(
                    config.telegram_bot_token,
                    config.telegram_chat_id,
                    format_alert_message(domain, host, verified[host]),
                )
                alerts_sent += 1
                domain_alerts_sent += 1
            except Exception as exc:
                logging.warning("[newsubdomainbot] telegram send failed for %s: %s", host, exc)

        domains_state[domain] = {
            "known_subdomains": current_hosts,
            "last_snapshot": build_state_snapshot(verified),
            "last_count": len(current_hosts),
            "last_run": iso_now(),
        }

    if domains_processed == 0 and domain_errors:
        return {
            "success": False,
            "error": "; ".join(domain_errors[:3]),
            "alerts_sent": 0,
        }

    state["version"] = 2
    state["domains"] = domains_state
    state["last_run"] = iso_now()
    save_json(config.state_file, state)

    message = None
    if manual_trigger:
        if len(configured_domains) == 1 and domains_processed == 1 and not domain_errors:
            only_domain = configured_domains[0]
            only_count = total_verified_hosts
            if alerts_sent > 0:
                message = f"⚠️ Detected {alerts_sent} new subdomain(s) for {only_domain}"
            else:
                message = f"✅ No new subdomains for {only_domain} (currently verified: {only_count})"
        else:
            if alerts_sent > 0:
                message = (
                    f"⚠️ Detected {alerts_sent} new subdomain(s) across "
                    f"{domains_processed}/{len(configured_domains)} domain(s)"
                )
            else:
                message = (
                    f"✅ No new subdomains across {domains_processed}/{len(configured_domains)} domain(s) "
                    f"(currently verified: {total_verified_hosts})"
                )
            if domain_errors:
                message += f"\n⚠️ {len(domain_errors)} domain(s) failed"

    return {"success": True, "alerts_sent": alerts_sent, "message": message}


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
