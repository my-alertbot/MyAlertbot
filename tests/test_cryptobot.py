from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from alertbot.bots import cryptobot


class FetchPricesTests(unittest.TestCase):
    @patch.object(cryptobot.time, "time", return_value=1_700_000_000)
    @patch.object(cryptobot, "request_json")
    def test_fetch_prices_normalizes_defillama_current_and_historical(
        self,
        request_json_mock,
        _time_mock,
    ) -> None:
        request_json_mock.side_effect = [
            {
                "coins": {
                    "coingecko:bitcoin": {"price": 110.0},
                    "coingecko:ethereum": {"price": 220.0},
                }
            },
            {
                "coins": {
                    "coingecko:bitcoin": {"price": 100.0},
                    "coingecko:ethereum": {"price": 200.0},
                }
            },
        ]

        prices = cryptobot.fetch_prices(["bitcoin", "ethereum"], "usd", include_24h_change=True)

        self.assertEqual(prices["bitcoin"]["usd"], 110.0)
        self.assertAlmostEqual(prices["bitcoin"]["usd_24h_change"], 10.0)
        self.assertEqual(prices["ethereum"]["usd"], 220.0)
        self.assertAlmostEqual(prices["ethereum"]["usd_24h_change"], 10.0)
        self.assertEqual(request_json_mock.call_count, 2)

        current_call = request_json_mock.call_args_list[0]
        historical_call = request_json_mock.call_args_list[1]
        self.assertIn("/coingecko:bitcoin,coingecko:ethereum", current_call.args[0])
        self.assertEqual(current_call.kwargs["params"]["searchWidth"], cryptobot.DEFILLAMA_SEARCH_WIDTH)
        self.assertIn("/1699913600/coingecko:bitcoin,coingecko:ethereum", historical_call.args[0])
        self.assertEqual(historical_call.kwargs["params"]["searchWidth"], cryptobot.DEFILLAMA_SEARCH_WIDTH)

    @patch.object(cryptobot, "request_json")
    def test_fetch_prices_skips_historical_request_when_change_not_needed(self, request_json_mock) -> None:
        request_json_mock.return_value = {
            "coins": {
                "coingecko:bitcoin": {"price": 100.0},
            }
        }

        prices = cryptobot.fetch_prices(["bitcoin"], "usd", include_24h_change=False)

        self.assertEqual(prices, {"bitcoin": {"usd": 100.0}})
        request_json_mock.assert_called_once()

    @patch.object(cryptobot, "request_json")
    def test_fetch_prices_handles_missing_historical_price(self, request_json_mock) -> None:
        request_json_mock.side_effect = [
            {"coins": {"coingecko:bitcoin": {"price": 100.0}}},
            {"coins": {}},
        ]

        prices = cryptobot.fetch_prices(["bitcoin"], "usd", include_24h_change=True)

        self.assertEqual(prices["bitcoin"]["usd"], 100.0)
        self.assertNotIn("usd_24h_change", prices["bitcoin"])

    def test_fetch_prices_rejects_non_usd_currency(self) -> None:
        with self.assertRaises(ValueError):
            cryptobot.fetch_prices(["bitcoin"], "eur")


class RunValidationTests(unittest.TestCase):
    @patch.object(cryptobot, "getenv_required", side_effect=["token", "chat"])
    @patch.object(cryptobot, "normalize_rules", return_value=[{"id": "bitcoin", "direction": "above", "price": 1.0, "currency": "usd"}])
    @patch.object(cryptobot, "load_json", return_value={"currency": "eur", "rules": [{"id": "bitcoin", "direction": "above", "price": 1}]})
    def test_run_rejects_non_usd_config_currency(
        self,
        _load_json_mock,
        _normalize_rules_mock,
        _getenv_required_mock,
    ) -> None:
        result = cryptobot.run(schedule_context={"bot": "cryptobot"})
        self.assertFalse(result["success"])
        self.assertIn("usd", result["error"].lower())

    @patch.object(cryptobot, "getenv_required", side_effect=["token", "chat"])
    @patch.object(cryptobot, "load_json", return_value={"currency": "usd", "rules": [{"id": "bitcoin", "direction": "above", "price": 1, "currency": "eur"}]})
    def test_run_rejects_rule_currency_override(
        self,
        _load_json_mock,
        _getenv_required_mock,
    ) -> None:
        result = cryptobot.run(schedule_context={"bot": "cryptobot"})
        self.assertFalse(result["success"])
        self.assertIn("rule-level currency", result["error"])


class PollTests(unittest.TestCase):
    @patch.object(cryptobot, "save_json")
    @patch.object(cryptobot, "load_json", return_value={"last_prices": {}})
    @patch.object(cryptobot, "send_telegram_alert")
    @patch.object(cryptobot, "fetch_prices")
    def test_poll_manual_trigger_requests_24h_change(
        self,
        fetch_prices_mock,
        _send_mock,
        _load_mock,
        _save_mock,
    ) -> None:
        fetch_prices_mock.return_value = {"bitcoin": {"usd": 100.0, "usd_24h_change": 1.0}}
        rules = [{"id": "bitcoin", "direction": "above", "price": 101.0, "currency": "usd"}]

        result = cryptobot.poll(
            rules=rules,
            currency="usd",
            tg_token="token",
            tg_chat_id="chat",
            state_path=Path("state/cryptobot.test.json"),
            manual_trigger=True,
        )

        self.assertTrue(result["success"])
        fetch_prices_mock.assert_called_once_with(["bitcoin"], "usd", include_24h_change=True)
        self.assertIn("Crypto Prices", result["message"])


if __name__ == "__main__":
    unittest.main()
