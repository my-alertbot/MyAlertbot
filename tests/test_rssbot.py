from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from alertbot.bots import rssbot


RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <guid>id-2</guid>
      <title>Second</title>
      <link>https://example.com/2</link>
    </item>
    <item>
      <guid>id-1</guid>
      <title>First</title>
      <link>https://example.com/1</link>
    </item>
  </channel>
</rss>
"""


ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <id>atom-1</id>
    <title>Atom Entry</title>
    <updated>2026-02-25T10:00:00Z</updated>
    <link rel="alternate" href="https://example.com/atom-1" />
  </entry>
</feed>
"""


class ParseRssTests(unittest.TestCase):
    def test_parse_atom_feed_extracts_title_link_and_id(self) -> None:
        feed_title, items = rssbot.parse_rss(ATOM_XML, "fallback")

        self.assertEqual(feed_title, "Atom Feed")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "atom-1")
        self.assertEqual(items[0]["link"], "https://example.com/atom-1")
        self.assertEqual(items[0]["published"], "2026-02-25T10:00:00Z")

    def test_detect_feed_order_returns_desc_for_newest_first(self) -> None:
        entries = [
            {"published": "Wed, 26 Feb 2026 10:00:00 GMT"},
            {"published": "Wed, 25 Feb 2026 10:00:00 GMT"},
        ]
        self.assertEqual(rssbot.detect_feed_order(entries), "desc")


class ProcessFeedTests(unittest.TestCase):
    def _config(self) -> rssbot.Config:
        return rssbot.Config(
            telegram_bot_token="token",
            telegram_chat_id="chat",
            rss_feed_urls=["https://example.com/feed.xml"],
            rssstate_file="state.json",
            rss_max_items=20,
            check_interval_minutes=60,
        )

    @patch.object(rssbot, "fetch_feed", return_value=RSS_XML)
    @patch.object(rssbot, "send_telegram_alert")
    def test_process_feed_first_run_initializes_state_without_alerts(
        self,
        send_mock,
        _fetch_mock,
    ) -> None:
        feed_state: dict[str, object] = {}
        alerts_sent, failed_sends = rssbot.process_feed(
            self._config(),
            "https://example.com/feed.xml",
            feed_state,
            now=datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual((alerts_sent, failed_sends), (0, 0))
        self.assertEqual(feed_state["rss_seen_ids"], ["id-2", "id-1"])
        self.assertEqual(feed_state["rss_last_entry_id"], "id-2")
        send_mock.assert_not_called()

    @patch.object(rssbot, "fetch_feed", return_value=RSS_XML)
    @patch.object(rssbot, "send_telegram_alert")
    def test_process_feed_sends_only_new_entries_and_updates_seen_ids(
        self,
        send_mock,
        _fetch_mock,
    ) -> None:
        feed_state: dict[str, object] = {"rss_seen_ids": ["id-1"]}
        alerts_sent, failed_sends = rssbot.process_feed(
            self._config(),
            "https://example.com/feed.xml",
            feed_state,
            now=datetime(2026, 2, 25, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual((alerts_sent, failed_sends), (1, 0))
        send_mock.assert_called_once()
        sent_message = send_mock.call_args.args[2]
        self.assertIn("Second", sent_message)
        self.assertEqual(feed_state["rss_seen_ids"][0], "id-2")
        self.assertIn("id-1", feed_state["rss_seen_ids"])


class ConfigTests(unittest.TestCase):
    def test_load_config_requires_rss_feed_url(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "RSS_FEED_URL"):
                rssbot.load_config()


if __name__ == "__main__":
    unittest.main()
