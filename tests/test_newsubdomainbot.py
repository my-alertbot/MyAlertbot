from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from alertbot.bots import newsubdomainbot


class DomainParsingTests(unittest.TestCase):
    def test_parse_domain_list_normalizes_deduplicates_and_strips_wildcard(self) -> None:
        domains = newsubdomainbot.parse_domain_list("*.Example.com, example.com., foo.com")
        self.assertEqual(domains, ["example.com", "foo.com"])

    def test_parse_domain_list_requires_at_least_one_valid_domain(self) -> None:
        with self.assertRaises(ValueError):
            newsubdomainbot.parse_domain_list(" , ")


class RunSubfinderTests(unittest.TestCase):
    @patch.object(newsubdomainbot.subprocess, "run")
    def test_run_subfinder_parses_output_dedupes_and_filters_wildcards(self, run_mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=["subfinder"],
            returncode=0,
            stdout="A.Example.com\n*.ignored.example.com\nA.example.com\nb.example.com.\n",
            stderr="",
        )

        names = newsubdomainbot.run_subfinder("example.com", "subfinder", 10)

        self.assertEqual(names, ["a.example.com", "b.example.com"])


class DiscoverVerifiedSubdomainsTests(unittest.TestCase):
    @patch.object(newsubdomainbot, "resolve_dns")
    @patch.object(newsubdomainbot, "run_subfinder")
    def test_discover_verified_subdomains_filters_domain_and_unresolved_hosts(
        self,
        run_subfinder_mock,
        resolve_dns_mock,
    ) -> None:
        run_subfinder_mock.return_value = [
            "example.com",
            "a.example.com",
            "other.test",
            "b.example.com",
        ]
        resolve_dns_mock.side_effect = [
            newsubdomainbot.DnsResolution(ipv4=["1.1.1.1"], ipv6=[], aliases=[]),
            None,
        ]

        result = newsubdomainbot.discover_verified_subdomains("example.com", "subfinder", 10)

        self.assertEqual(list(result.keys()), ["a.example.com"])
        self.assertEqual(result["a.example.com"].ipv4, ["1.1.1.1"])


if __name__ == "__main__":
    unittest.main()
