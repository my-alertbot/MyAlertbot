from __future__ import annotations

import unittest

from alertbot.bots import newtopaimodelbot


class ParseTopEntriesTests(unittest.TestCase):
    def test_parses_top_entries_and_skips_invalid_rows(self) -> None:
        payload = {
            "leaderboard": [
                {"model_id": "m1", "model_name": "Model 1", "organization": "org1"},
                {"model_id": "", "model_name": "bad"},
                {"variant_id": "m2", "variant_key": "variant-2", "organization": "org2"},
                {"model_id": "m1", "model_name": "dup"},
                {"model_id": "m3", "model_name": "Model 3"},
            ]
        }

        entries = newtopaimodelbot.parse_top_entries(payload, top_n=3)

        self.assertEqual([entry.model_id for entry in entries], ["m1", "m2", "m3"])
        self.assertEqual([entry.rank for entry in entries], [1, 2, 3])
        self.assertEqual(entries[1].name, "variant-2")

    def test_raises_when_not_enough_rows(self) -> None:
        with self.assertRaises(RuntimeError):
            newtopaimodelbot.parse_top_entries({"leaderboard": []}, top_n=1)


class FindNewTopEntriesTests(unittest.TestCase):
    def test_detects_models_not_in_previous_top_ten(self) -> None:
        previous_state = {
            "top_entries": [
                {"model_id": "m1"},
                {"model_id": "m2"},
                {"model_id": "m3"},
            ]
        }
        current_entries = [
            newtopaimodelbot.TopEntry(model_id="m2", name="Model 2", organization="", rank=1),
            newtopaimodelbot.TopEntry(model_id="m4", name="Model 4", organization="org", rank=2),
            newtopaimodelbot.TopEntry(model_id="m1", name="Model 1", organization="", rank=3),
        ]

        new_entries = newtopaimodelbot.find_new_top_entries(previous_state, current_entries)

        self.assertEqual([entry.model_id for entry in new_entries], ["m4"])
        self.assertEqual(new_entries[0].rank, 2)

    def test_formats_alert_message_with_current_top_ten_summary(self) -> None:
        current_entries = [
            newtopaimodelbot.TopEntry(model_id=f"m{i}", name=f"Model {i}", organization="", rank=i)
            for i in range(1, 11)
        ]
        new_entries = [current_entries[4]]

        message = newtopaimodelbot.format_alert_message(new_entries, current_entries)

        self.assertIn("#5: Model 5", message)
        self.assertIn("Current top 10:", message)
        self.assertIn("10. Model 10", message)
        self.assertIn("https://llm-stats.com/", message)


if __name__ == "__main__":
    unittest.main()
