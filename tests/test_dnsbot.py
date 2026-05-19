from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from alertbot.bots import dnsbot


class CertBucketTests(unittest.TestCase):
    def test_ok_when_greater_than_14(self) -> None:
        self.assertEqual(dnsbot._cert_bucket(15), "ok")
        self.assertEqual(dnsbot._cert_bucket(100), "ok")

    def test_bucketing_near_thresholds(self) -> None:
        self.assertEqual(dnsbot._cert_bucket(14), "8-14 days")
        self.assertEqual(dnsbot._cert_bucket(8), "8-14 days")
        self.assertEqual(dnsbot._cert_bucket(7), "4-7 days")
        self.assertEqual(dnsbot._cert_bucket(4), "4-7 days")
        self.assertEqual(dnsbot._cert_bucket(3), "2-3 days")
        self.assertEqual(dnsbot._cert_bucket(2), "2-3 days")
        self.assertEqual(dnsbot._cert_bucket(1), "1 day")
        self.assertEqual(dnsbot._cert_bucket(0), "expires today")
        self.assertEqual(dnsbot._cert_bucket(-1), "expired")


class SeverityTests(unittest.TestCase):
    def test_max_severity_picks_highest(self) -> None:
        items = [
            {"severity": "low"},
            {"severity": "medium"},
            {"severity": "high"},
        ]
        self.assertEqual(dnsbot._max_severity(items), "high")

    def test_max_severity_defaults_to_low(self) -> None:
        self.assertEqual(dnsbot._max_severity([]), "low")
        self.assertEqual(dnsbot._max_severity([{}]), "low")


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable(self) -> None:
        f1 = [{"id": "a", "severity": "low", "title": "t", "details": {}}]
        f2 = [{"id": "a", "severity": "low", "title": "t", "details": {}}]
        self.assertEqual(dnsbot._fingerprint_findings(f1), dnsbot._fingerprint_findings(f2))

    def test_fingerprint_changes_with_content(self) -> None:
        f1 = [{"id": "a", "severity": "low", "title": "t", "details": {}}]
        f2 = [{"id": "a", "severity": "high", "title": "t", "details": {}}]
        self.assertNotEqual(dnsbot._fingerprint_findings(f1), dnsbot._fingerprint_findings(f2))


class DoHCheckTests(unittest.TestCase):
    def _soa_response(self, serial: str = "2024010101") -> dict:
        return {
            "Status": 0,
            "Answer": [
                {
                    "name": "example.com.",
                    "type": 6,
                    "TTL": 300,
                    "data": f"ns1.example.com. admin.example.com. {serial} 3600 600 604800 86400",
                }
            ],
        }

    def _a_response(self, ips: list[str]) -> dict:
        return {
            "Status": 0,
            "Answer": [
                {"name": "example.com.", "type": 1, "TTL": 300, "data": ip} for ip in ips
            ],
        }

    @patch.object(dnsbot, "_doh_query")
    def test_first_run_reports_ip_change(self, mock_doh: MagicMock) -> None:
        # _check_doh reports diffs unconditionally; top-level run() suppresses
        # alerts on first scheduled run to establish baseline.
        mock_doh.side_effect = [self._soa_response(), self._a_response(["1.2.3.4"])]
        events, new_state = dnsbot._check_doh("example.com", {})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "doh_a_changed_example.com")
        self.assertEqual(new_state["state"], "resolved")
        self.assertEqual(new_state["ips"], "1.2.3.4")

    @patch.object(dnsbot, "_doh_query")
    def test_detects_ip_change(self, mock_doh: MagicMock) -> None:
        mock_doh.side_effect = [
            self._soa_response(serial="2024010101"),
            self._a_response(["5.6.7.8"]),
        ]
        prev = {"state": "resolved", "ips": "1.2.3.4", "serial": "2024010101"}
        events, new_state = dnsbot._check_doh("example.com", prev)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "doh_a_changed_example.com")
        self.assertEqual(new_state["ips"], "5.6.7.8")

    @patch.object(dnsbot, "_doh_query")
    def test_detects_soa_serial_change(self, mock_doh: MagicMock) -> None:
        mock_doh.side_effect = [
            self._soa_response(serial="2024010201"),
            self._a_response(["1.2.3.4"]),
        ]
        prev = {"state": "resolved", "ips": "1.2.3.4", "serial": "2024010101"}
        events, new_state = dnsbot._check_doh("example.com", prev)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "doh_soa_serial_changed_example.com")
        self.assertEqual(new_state["serial"], "2024010201")

    @patch.object(dnsbot, "_doh_query")
    def test_detects_no_authority(self, mock_doh: MagicMock) -> None:
        mock_doh.return_value = {
            "Status": 2,
            "Comment": ["No Reachable Authority"],
        }
        prev = {"state": "resolved", "ips": "1.2.3.4", "serial": "2024010101"}
        events, new_state = dnsbot._check_doh("example.com", prev)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "doh_no_authority_example.com")
        self.assertEqual(new_state["state"], "no_authority")


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)

    @patch.object(dnsbot, "load_env_file")
    @patch.object(dnsbot, "setup_logging")
    @patch.object(dnsbot, "send_alert_message")
    def test_run_without_config_returns_error(self, _send_mock, _log_mock, _env_mock) -> None:
        os.environ.pop("DNSBOT_DOMAINS", None)
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "chat"
        result = dnsbot.run(manual_trigger=False)
        self.assertFalse(result["success"])
        self.assertIn("DNSBOT_DOMAINS", result["error"])

    @patch.object(dnsbot, "load_env_file")
    @patch.object(dnsbot, "setup_logging")
    @patch.object(dnsbot, "_check_doh")
    @patch.object(dnsbot, "_build_dns_policy_findings")
    @patch.object(dnsbot, "_run_http_probes")
    @patch.object(dnsbot, "_run_cert_checks")
    @patch.object(dnsbot, "send_alert_message")
    def test_scheduled_first_run_baseline_no_alert(
        self,
        send_mock: MagicMock,
        cert_mock: MagicMock,
        http_mock: MagicMock,
        policy_mock: MagicMock,
        doh_mock: MagicMock,
        _log_mock: MagicMock,
        _env_mock: MagicMock,
    ) -> None:
        os.environ["DNSBOT_DOMAINS"] = "example.com"
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "chat"

        doh_mock.return_value = ([], {"state": "resolved", "ips": "1.2.3.4", "serial": "1"})
        policy_mock.return_value = []
        http_mock.return_value = []
        cert_mock.return_value = []

        with patch.object(dnsbot, "save_json") as save_mock:
            result = dnsbot.run(manual_trigger=False)

        self.assertTrue(result["success"])
        self.assertEqual(result["alerts_sent"], 0)
        self.assertEqual(result["message"], "First run: baseline established")
        send_mock.assert_not_called()
        save_mock.assert_called_once()

    @patch.object(dnsbot, "load_env_file")
    @patch.object(dnsbot, "setup_logging")
    @patch.object(dnsbot, "send_alert_message")
    def test_manual_trigger_sends_summary(self, send_mock: MagicMock, _log_mock: MagicMock, _env_mock: MagicMock) -> None:
        os.environ["DNSBOT_DOMAINS"] = "example.com"
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_CHAT_ID"] = "chat"

        state = {
            "domains": {
                "example.com": {
                    "doh": {"state": "resolved", "ips": "1.2.3.4", "serial": "2024010101"},
                    "dns_policy_hash": "abc",
                }
            },
            "http_hash": "def",
            "cert_hash": "ghi",
            "initialized": True,
        }

        with patch.object(dnsbot, "load_json", return_value=state):
            with patch.object(dnsbot, "save_json"):
                with patch.object(dnsbot, "_check_doh", return_value=([], state["domains"]["example.com"]["doh"])):
                    with patch.object(dnsbot, "_build_dns_policy_findings", return_value=[]):
                        with patch.object(dnsbot, "_run_http_probes", return_value=[]):
                            with patch.object(dnsbot, "_run_cert_checks", return_value=[]):
                                result = dnsbot.run(manual_trigger=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["alerts_sent"], 1)
        send_mock.assert_called_once()
        args, _kwargs = send_mock.call_args
        self.assertIn("Domain Monitor Status", args[0])


if __name__ == "__main__":
    unittest.main()
