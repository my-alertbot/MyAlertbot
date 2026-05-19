import hashlib
import json
import logging
import os
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from alertbot.common import (
    STATE_DIR,
    format_run_info,
    getenv_required,
    load_env_file,
    load_json,
    request_with_retry,
    save_json,
    send_alert_message,
    send_telegram_alert,
    setup_logging,
)

BOT_ID = "dns"
BOT_COMMAND = "dns"

DEFAULT_STATE_FILE = STATE_DIR / "dnsbot_state.json"
DEFAULT_CERT_DAYS_WARNING = 14


def _make_finding(fid: str, severity: str, title: str, details: Any) -> dict:
    return {"id": fid, "severity": severity, "title": title, "details": details}


def _sev_rank(sev: str) -> int:
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(sev).lower(), 1)


def _max_severity(items: List[dict]) -> str:
    best = "low"
    for it in items or []:
        s = it.get("severity", "low")
        if _sev_rank(s) > _sev_rank(best):
            best = s
    return best


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _hash_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# =============================================================================
# Config
# =============================================================================

class Config:
    def __init__(
        self,
        domains: List[str],
        probe_hosts: List[str],
        cert_days_warning: int,
        telegram_bot_token: str,
        telegram_chat_id: str,
        state_file: Path,
    ):
        self.domains = domains
        self.probe_hosts = probe_hosts
        self.cert_days_warning = cert_days_warning
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.state_file = state_file


def _parse_comma_list(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_config() -> Config:
    raw_domains = os.getenv("DNSBOT_DOMAINS", "")
    domains = _parse_comma_list(raw_domains)
    if not domains:
        raise ValueError("Missing required environment variable: DNSBOT_DOMAINS")

    raw_probe_hosts = os.getenv("DNSBOT_PROBE_HOSTS", "")
    probe_hosts = _parse_comma_list(raw_probe_hosts)
    if not probe_hosts:
        # default to apex + www for each domain
        probe_hosts = []
        for d in domains:
            probe_hosts.append(d)
            probe_hosts.append(f"www.{d}")

    cert_days = int(os.getenv("DNSBOT_CERT_DAYS_WARNING", str(DEFAULT_CERT_DAYS_WARNING)))

    return Config(
        domains=domains,
        probe_hosts=probe_hosts,
        cert_days_warning=cert_days,
        telegram_bot_token=getenv_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=getenv_required("TELEGRAM_CHAT_ID"),
        state_file=Path(os.getenv("DNSBOT_STATE_FILE", DEFAULT_STATE_FILE)),
    )


# =============================================================================
# DoH queries
# =============================================================================

def _doh_query(name: str, rtype: str) -> dict:
    endpoints = [
        ("https://cloudflare-dns.com/dns-query", "Cloudflare"),
        ("https://dns.google/resolve", "Google"),
    ]
    headers = {"Accept": "application/dns-json"}
    last_err: Optional[str] = None
    for url, label in endpoints:
        try:
            resp = request_with_retry(
                "GET",
                url,
                params={"name": name, "type": rtype},
                headers=headers,
                timeout=10,
                max_retries=1,
            )
            if resp.status_code != 200:
                last_err = f"DoH {label} returned HTTP {resp.status_code}"
                continue
            return resp.json()
        except Exception as exc:
            last_err = str(exc)
            continue
    raise RuntimeError(f"DoH query failed for {name}/{rtype}: {last_err}")


# =============================================================================
# External DNS checks (DoH)
# =============================================================================

def _check_doh(domain: str, doh_state: dict) -> Tuple[List[dict], dict]:
    """Run DoH checks for a domain. Returns (events, new_state)."""
    events: List[dict] = []

    # Query SOA first
    soa_data = _doh_query(domain, "SOA")
    comments = soa_data.get("Comment", []) or []
    no_authority = any("No Reachable Authority" in str(c) for c in comments)

    prev_state = doh_state.get("state")
    prev_ips_str = doh_state.get("ips", "")
    prev_serial = doh_state.get("serial", "unknown")
    prev_ips = sorted([x.strip() for x in prev_ips_str.split(",") if x.strip()])

    if no_authority:
        new_state = {"state": "no_authority", "ts": int(datetime.now(timezone.utc).timestamp())}
        if prev_state != "no_authority":
            events.append(
                _make_finding(
                    f"doh_no_authority_{domain}",
                    "high",
                    f"DNS authority unreachable for {domain}",
                    {"status": soa_data.get("Status"), "comments": comments},
                )
            )
        return events, new_state

    # Query A records
    a_data = _doh_query(domain, "A")
    a_answers = [ans for ans in (a_data.get("Answer") or []) if ans.get("type") == 1]
    current_ips = sorted([str(ans["data"]) for ans in a_answers if ans.get("data")])
    ttl = a_answers[0].get("TTL") if a_answers else None

    # Parse SOA record from SOA query response
    soa_answers = [ans for ans in (soa_data.get("Answer") or []) if ans.get("type") == 6]
    serial = "unknown"
    primary_ns = "unknown"
    admin_email = "unknown"
    if soa_answers and soa_answers[0].get("data"):
        parts = str(soa_answers[0]["data"]).split()
        primary_ns = parts[0] if len(parts) > 0 else "unknown"
        admin_email = parts[1] if len(parts) > 1 else "unknown"
        serial = parts[2] if len(parts) > 2 else "unknown"

    # IP change
    if _stable_json(prev_ips) != _stable_json(current_ips):
        events.append(
            _make_finding(
                f"doh_a_changed_{domain}",
                "high",
                f"A records changed for {domain}",
                {
                    "previousIPs": ", ".join(prev_ips) if prev_ips else "none",
                    "newIPs": ", ".join(current_ips) if current_ips else "none",
                    "ttl": ttl if ttl is not None else "N/A",
                },
            )
        )

    # SOA serial change
    if serial != "unknown" and prev_serial != "unknown" and serial != prev_serial:
        events.append(
            _make_finding(
                f"doh_soa_serial_changed_{domain}",
                "medium",
                f"SOA serial changed for {domain}",
                {"previousSerial": prev_serial, "newSerial": serial, "primaryNS": primary_ns, "adminEmail": admin_email},
            )
        )

    new_state = {
        "state": "resolved",
        "ts": int(datetime.now(timezone.utc).timestamp()),
        "ips": ",".join(current_ips),
        "serial": serial,
        "primaryNS": primary_ns,
        "adminEmail": admin_email,
    }
    return events, new_state


# =============================================================================
# DNS policy findings (public DNS only)
# =============================================================================

def _build_dns_policy_findings(domain: str) -> List[dict]:
    findings: List[dict] = []

    # CAA
    try:
        caa_data = _doh_query(domain, "CAA")
        caa_answers = [ans for ans in (caa_data.get("Answer") or []) if ans.get("type") == 257]
        if not caa_answers:
            findings.append(_make_finding("no_caa", "low", f"CAA record missing for {domain} (recommended)", {"domain": domain}))
    except Exception as exc:
        logging.debug("CAA query failed for %s: %s", domain, exc)

    # SPF
    try:
        txt_data = _doh_query(domain, "TXT")
        txt_answers = [ans for ans in (txt_data.get("Answer") or []) if ans.get("type") == 16]
        has_spf = any("v=spf1" in str(ans.get("data", "")) for ans in txt_answers)
        if not has_spf:
            findings.append(_make_finding("no_spf", "low", f"SPF TXT missing for {domain} (v=spf1)", {"domain": domain}))
    except Exception as exc:
        logging.debug("TXT/SPF query failed for %s: %s", domain, exc)

    # DMARC
    try:
        dmarc_data = _doh_query(f"_dmarc.{domain}", "TXT")
        dmarc_answers = [ans for ans in (dmarc_data.get("Answer") or []) if ans.get("type") == 16]
        has_dmarc = any("v=DMARC1" in str(ans.get("data", "")) for ans in dmarc_answers)
        if not has_dmarc:
            findings.append(_make_finding("no_dmarc", "low", f"DMARC TXT missing for {domain} (v=DMARC1)", {"domain": f"_dmarc.{domain}"}))
    except Exception as exc:
        logging.debug("DMARC query failed for %s: %s", domain, exc)

    return findings


# =============================================================================
# HTTP probes
# =============================================================================

def _run_http_probes(hosts: List[str]) -> List[dict]:
    findings: List[dict] = []
    for host in hosts:
        # HTTP -> HTTPS redirect
        try:
            resp = requests.get(f"http://{host}", allow_redirects=False, timeout=10)
            loc = resp.headers.get("location", "")
            ok_redirect = resp.status_code in (301, 302, 307, 308) and loc.startswith("https://")
            if not ok_redirect:
                findings.append(
                    _make_finding(
                        f"no_http_to_https_{host}",
                        "medium",
                        f"No proper HTTP→HTTPS redirect for {host}",
                        {"status": resp.status_code, "location": loc},
                    )
                )
        except Exception as exc:
            findings.append(
                _make_finding(
                    f"http_probe_error_{host}",
                    "medium",
                    f"HTTP probe failed for {host}",
                    {"error": str(exc)},
                )
            )

        # HTTPS headers (HSTS)
        try:
            resp = requests.get(f"https://{host}", allow_redirects=True, timeout=10)
            hsts = resp.headers.get("strict-transport-security")
            if not hsts:
                findings.append(
                    _make_finding(
                        f"no_hsts_{host}",
                        "low",
                        f"HSTS missing on {host}",
                        {},
                    )
                )
        except Exception as exc:
            findings.append(
                _make_finding(
                    f"https_probe_error_{host}",
                    "high",
                    f"HTTPS probe failed for {host}",
                    {"error": str(exc)},
                )
            )

    return findings


# =============================================================================
# Certificate expiry
# =============================================================================

def _cert_bucket(days: int) -> str:
    if days > 14:
        return "ok"
    if days >= 8:
        return "8-14 days"
    if days >= 4:
        return "4-7 days"
    if days >= 2:
        return "2-3 days"
    if days == 1:
        return "1 day"
    if days == 0:
        return "expires today"
    return "expired"


def _get_cert_expiry_finding(host: str, warning_days: int) -> Optional[dict]:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                not_after_str = cert.get("notAfter")
                if not not_after_str:
                    return None
                not_after_ts = ssl.cert_time_to_seconds(not_after_str)
                not_after = datetime.fromtimestamp(not_after_ts, tz=timezone.utc)
                now = datetime.now(timezone.utc)
                days = (not_after - now).days
                bucket = _cert_bucket(days)
                if bucket == "ok":
                    return None
                return _make_finding(
                    "cert_expiring",
                    "high" if days <= 3 else "medium",
                    f"Certificate for {host} expires soon ({bucket})",
                    {"host": host, "expires_on": not_after.isoformat(), "days": days, "bucket": bucket},
                )
    except Exception as exc:
        logging.debug("Cert expiry check failed for %s: %s", host, exc)
        return None


def _run_cert_checks(hosts: List[str], warning_days: int) -> List[dict]:
    findings: List[dict] = []
    seen_hosts = set()
    for host in hosts:
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        finding = _get_cert_expiry_finding(host, warning_days)
        if finding:
            findings.append(finding)
    return findings


# =============================================================================
# Diff / summary helpers
# =============================================================================

def _fingerprint_findings(findings: List[dict]) -> str:
    stable = sorted(
        [
            {
                "id": f["id"],
                "severity": f["severity"],
                "title": f["title"],
                "details": f["details"],
            }
            for f in findings
        ],
        key=lambda x: x["id"],
    )
    return _hash_str(_stable_json(stable))


def _summarize_findings(findings: List[dict], max_lines: int = 10) -> List[str]:
    sorted_f = sorted(findings, key=lambda f: (-_sev_rank(f.get("severity", "low")), f.get("id", "")))
    lines: List[str] = []
    for f in sorted_f[:max_lines]:
        lines.append(f"- [{f['severity']}] {f['title']} (`{f['id']}`)")
    if len(sorted_f) > max_lines:
        lines.append(f"- …and {len(sorted_f) - max_lines} more")
    if not lines:
        lines.append("- (no findings)")
    return lines


# =============================================================================
# Alert formatting
# =============================================================================

def _build_alert(changes: List[dict]) -> str:
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lines = ["🚨 Domain Monitor – Changes Detected", "", f"Time (UTC): `{now_iso}`", ""]
    for change in changes:
        lines.append(f"*{change['title']}* (sev: `{change['severity']}`)")
        for l in change["lines"]:
            lines.append(l)
        lines.append("")
    return "\n".join(lines)


# =============================================================================
# Main run logic
# =============================================================================

def run(
    manual_trigger: bool = False,
    chat_id: Optional[str] = None,
    schedule_context: Optional[Dict[str, Any]] = None,
) -> dict:
    load_env_file()
    setup_logging()
    logging.info("dnsbot %s", format_run_info(schedule_context))

    try:
        config = load_config()
    except ValueError as exc:
        logging.error("dnsbot config error: %s", exc)
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    state = load_json(config.state_file, default={"domains": {}, "initialized": False})
    domain_states = state.setdefault("domains", {})
    changes: List[dict] = []

    for domain in config.domains:
        ds = domain_states.setdefault(domain, {})

        # 1) DoH checks
        doh_events, new_doh_state = _check_doh(domain, ds.get("doh", {}))
        if doh_events:
            changes.append({
                "title": f"External DNS (DoH) changes – {domain}",
                "severity": _max_severity(doh_events),
                "lines": _summarize_findings(doh_events, 12),
            })
        ds["doh"] = new_doh_state

        # 2) DNS policy findings
        policy_findings = _build_dns_policy_findings(domain)
        prev_policy_hash = ds.get("dns_policy_hash")
        cur_policy_hash = _fingerprint_findings(policy_findings)
        if prev_policy_hash is not None and prev_policy_hash != cur_policy_hash:
            changes.append({
                "title": f"DNS policy findings changed – {domain}",
                "severity": _max_severity(policy_findings),
                "lines": _summarize_findings(policy_findings, 10),
            })
        ds["dns_policy_hash"] = cur_policy_hash

    # 3) HTTP probes (shared across all domains' hosts)
    http_findings = _run_http_probes(config.probe_hosts)
    prev_http_hash = state.get("http_hash")
    cur_http_hash = _fingerprint_findings(http_findings)
    if prev_http_hash is not None and prev_http_hash != cur_http_hash:
        changes.append({
            "title": "HTTP probe findings changed",
            "severity": _max_severity(http_findings),
            "lines": _summarize_findings(http_findings, 12),
        })
    state["http_hash"] = cur_http_hash

    # 4) Certificate expiry checks
    cert_findings = _run_cert_checks(config.probe_hosts, config.cert_days_warning)
    prev_cert_hash = state.get("cert_hash")
    cur_cert_hash = _fingerprint_findings(cert_findings)
    if prev_cert_hash is not None and prev_cert_hash != cur_cert_hash:
        changes.append({
            "title": "Certificate status changed",
            "severity": _max_severity(cert_findings),
            "lines": _summarize_findings(cert_findings, 10),
        })
    state["cert_hash"] = cur_cert_hash

    # Capture first-run state before saving
    was_initialized = state.get("initialized", False)

    # Save state
    state["initialized"] = True
    save_json(config.state_file, state)

    # For manual trigger: always return summary
    if manual_trigger:
        summary_lines = ["📋 Domain Monitor Status", ""]
        for domain in config.domains:
            ds = domain_states.get(domain, {})
            doh = ds.get("doh", {})
            summary_lines.append(f"*Domain:* `{domain}`")
            summary_lines.append(f"  DoH state: {doh.get('state', 'unknown')}")
            summary_lines.append(f"  IPs: {doh.get('ips', 'none')}")
            summary_lines.append(f"  SOA serial: {doh.get('serial', 'unknown')}")
        if http_findings:
            summary_lines.append("")
            summary_lines.append("*HTTP findings:*")
            for line in _summarize_findings(http_findings, 20):
                summary_lines.append(line)
        if cert_findings:
            summary_lines.append("")
            summary_lines.append("*Certificate findings:*")
            for line in _summarize_findings(cert_findings, 20):
                summary_lines.append(line)
        message = "\n".join(summary_lines)
        try:
            target_chat = chat_id or config.telegram_chat_id
            send_alert_message(message, destination_id=target_chat)
        except Exception as exc:
            logging.error("Failed to send manual dnsbot alert: %s", exc)
            return {"success": False, "error": str(exc), "alerts_sent": 0}
        return {"success": True, "alerts_sent": 1, "message": message}

    # Scheduled trigger
    # On first run (no previous hashes), do not alert — establish baseline
    if not was_initialized:
        logging.info("dnsbot first run — baseline established, no alerts sent")
        return {"success": True, "alerts_sent": 0, "message": "First run: baseline established"}

    if not changes:
        logging.info("dnsbot: no changes detected")
        return {"success": True, "alerts_sent": 0, "message": "No changes detected"}

    message = _build_alert(changes)
    try:
        target_chat = chat_id or config.telegram_chat_id
        send_alert_message(message, destination_id=target_chat)
    except Exception as exc:
        logging.error("Failed to send dnsbot alert: %s", exc)
        return {"success": False, "error": str(exc), "alerts_sent": 0}

    logging.info("dnsbot sent alert with %d change groups", len(changes))
    return {"success": True, "alerts_sent": 1, "message": f"DNS changes detected ({len(changes)} groups)"}


def main() -> None:
    result = run(manual_trigger=True)
    print(result)


if __name__ == "__main__":
    main()
