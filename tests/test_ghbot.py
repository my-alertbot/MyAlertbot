from __future__ import annotations

import unittest
from unittest.mock import patch

from alertbot.bots import ghbot


class SubjectApiUrlToWebUrlTests(unittest.TestCase):
    def test_converts_pull_request_api_url(self) -> None:
        api_url = "https://api.github.com/repos/yearn/optimization-visualizer/pulls/8"
        self.assertEqual(
            ghbot.subject_api_url_to_web_url(api_url),
            "https://github.com/yearn/optimization-visualizer/pull/8",
        )


class ResolveSubjectHtmlUrlTests(unittest.TestCase):
    def test_falls_back_to_web_pr_url_when_subject_lookup_fails(self) -> None:
        api_url = "https://api.github.com/repos/yearn/optimization-visualizer/pulls/8"
        cache: dict[str, str] = {}

        with patch.object(ghbot, "request_with_retry", side_effect=RuntimeError("boom")):
            resolved = ghbot.resolve_subject_html_url(api_url, headers={}, cache=cache)

        self.assertEqual(resolved, "https://github.com/yearn/optimization-visualizer/pull/8")
        self.assertEqual(cache[api_url], "https://github.com/yearn/optimization-visualizer/pull/8")


if __name__ == "__main__":
    unittest.main()
