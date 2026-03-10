from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from alertbot.bots import geoshockbot


class ClassifyTextTests(unittest.TestCase):
    def test_classify_text_detects_extreme_conflict_pattern(self) -> None:
        text = "US and Israel launched missile strikes on Iran as airspace closed."
        candidate, strong, actors, action_terms, severity_terms = geoshockbot.classify_text(text)

        self.assertTrue(candidate)
        self.assertTrue(strong)
        self.assertIn("israel", actors)
        self.assertIn("iran", actors)
        self.assertIn("missile", action_terms)
        self.assertIn("airspace closed", severity_terms)

    def test_classify_text_rejects_single_actor_story(self) -> None:
        text = "Iran economy update and domestic policy changes."
        candidate, strong, _actors, _actions, _severity = geoshockbot.classify_text(text)

        self.assertFalse(candidate)
        self.assertFalse(strong)


class NewsMetricsTests(unittest.TestCase):
    def _signal(
        self,
        source_name: str,
        region: str,
        high_trust: bool,
        title: str,
    ) -> geoshockbot.NewsSignal:
        return geoshockbot.NewsSignal(
            source_name=source_name,
            region=region,
            high_trust=high_trust,
            title=title,
            link="https://example.com",
            published_raw="Sat, 28 Feb 2026 10:00:00 GMT",
            published_at=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
            actors={"iran", "israel"},
            action_terms={"attack", "missile"},
            severity_terms={"airspace closed"},
        )

    def test_build_news_metrics_requires_cross_region_confirmation(self) -> None:
        signals = [
            self._signal("Al Jazeera", "middle_east", True, "A"),
            self._signal("BBC", "europe", True, "B"),
            self._signal("NYTimes", "americas", True, "C"),
        ]

        metrics = geoshockbot.build_news_metrics(signals, min_confirmations=3, min_high_trust=2)
        self.assertTrue(metrics["news_gate"])
        self.assertTrue(metrics["severity_gate"])
        self.assertEqual(metrics["source_count"], 3)
        self.assertEqual(metrics["high_trust_count"], 3)

    def test_evaluate_trigger_requires_corroboration_or_high_intensity(self) -> None:
        signals = [
            self._signal("Al Jazeera", "middle_east", True, "A"),
            self._signal("BBC", "western", True, "B"),
            self._signal("NYTimes", "western", True, "C"),
        ]
        metrics = geoshockbot.build_news_metrics(signals, min_confirmations=3, min_high_trust=2)

        # news_gate + severity_gate alone are not enough — need infra/market/high-intensity
        trigger_ready, _reasons = geoshockbot.evaluate_trigger(
            metrics,
            infra_signal={"triggered": False},
            market_signal={"triggered": False},
        )
        self.assertFalse(trigger_ready)

        # Adding infra corroboration pushes it over the threshold
        trigger_ready, _reasons = geoshockbot.evaluate_trigger(
            metrics,
            infra_signal={"triggered": True},
            market_signal={"triggered": False},
        )
        self.assertTrue(trigger_ready)


class RunPersistenceTests(unittest.TestCase):
    def test_run_alerts_on_second_consecutive_trigger(self) -> None:
        signal = geoshockbot.NewsSignal(
            source_name="Al Jazeera",
            region="middle_east",
            high_trust=True,
            title="US and Israel attack Iran",
            link="https://example.com/1",
            published_raw="Sat, 28 Feb 2026 10:00:00 GMT",
            published_at=datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
            actors={"iran", "israel", "united_states"},
            action_terms={"attack", "missile"},
            severity_terms={"airspace closed"},
        )
        signal_west_1 = geoshockbot.NewsSignal(
            source_name="BBC",
            region="europe",
            high_trust=True,
            title="Retaliatory strikes reported",
            link="https://example.com/2",
            published_raw="Sat, 28 Feb 2026 10:05:00 GMT",
            published_at=datetime(2026, 2, 28, 10, 5, tzinfo=timezone.utc),
            actors={"iran", "israel"},
            action_terms={"strike", "missile"},
            severity_terms={"explosions heard"},
        )
        signal_west_2 = geoshockbot.NewsSignal(
            source_name="NYTimes",
            region="americas",
            high_trust=True,
            title="Major military operation begins",
            link="https://example.com/3",
            published_raw="Sat, 28 Feb 2026 10:10:00 GMT",
            published_at=datetime(2026, 2, 28, 10, 10, tzinfo=timezone.utc),
            actors={"iran", "israel"},
            action_terms={"military operation", "missile"},
            severity_terms={"major attack"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "geoshock-state.json"
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_CHAT_ID": "chat",
                    "GEOSHOCK_STATE_FILE": str(state_file),
                    "GEOSHOCK_PERSISTENCE_RUNS": "2",
                },
                clear=True,
            ):
                with patch.object(
                    geoshockbot,
                    "collect_news_signals",
                    return_value=([signal, signal_west_1, signal_west_2], []),
                ):
                    with patch.object(
                        geoshockbot,
                        "assess_infrastructure_signal",
                        return_value={
                            "triggered": True,
                            "top": {
                                "country_code": "IR",
                                "drop_pct": 12.5,
                                "pre_v4_avg": 8300.0,
                                "post_v4_avg": 7160.0,
                                "asn_drop_pct": 15.0,
                            },
                            "details": [],
                            "errors": [],
                        },
                    ):
                        with patch.object(
                            geoshockbot,
                            "assess_market_signal",
                            return_value={"triggered": False, "vix": None, "ovx": None, "errors": []},
                        ):
                            with patch.object(geoshockbot, "send_telegram_alert") as send_mock:
                                first = geoshockbot.run()
                                second = geoshockbot.run()

        self.assertTrue(first["success"])
        self.assertEqual(first["alerts_sent"], 0)
        self.assertTrue(second["success"])
        self.assertEqual(second["alerts_sent"], 1)
        self.assertEqual(send_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
