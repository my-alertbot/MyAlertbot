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


class PollSendsAllParamTests(unittest.TestCase):
    """Regression: GitHub /notifications defaults to unread-only unless all=true.

    Without all=true the bot misses any notification marked read before the
    next poll and the last_seen_at watermark stalls. Assert poll() always
    requests all=true so read+unread notifications are returned.
    """
    def test_poll_passes_all_true_with_since(self) -> None:
        import tempfile
        from pathlib import Path

        captured: dict = {}

        class FakeResp:
            status_code = 200
            headers: dict = {}

            def json(self) -> list:
                return []

        def fake_request(**kwargs):
            captured.update(kwargs)
            return FakeResp()

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text('{"last_seen_at": "2026-07-14T20:29:15Z", "recent_ids": []}')

            with patch.object(ghbot, "request_with_retry", side_effect=fake_request):
                ghbot.poll(
                    token="t",
                    state_path=state_path,
                    tg_token="tg",
                    tg_chat_id="chat",
                    last_run="2026-07-14T20:29:15Z",
                )

        params = captured.get("params") or {}
        self.assertEqual(params.get("all"), "true")
        self.assertEqual(params.get("since"), "2026-07-14T20:29:15Z")


if __name__ == "__main__":
    unittest.main()
