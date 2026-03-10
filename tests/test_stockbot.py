from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from alertbot.bots import stockbot


class StockbotConfigTests(unittest.TestCase):
    def test_parse_ticker_list_normalizes_values(self) -> None:
        parsed = stockbot.parse_ticker_list(" aapl, msft ,, googl ")
        self.assertEqual(parsed, ["AAPL", "MSFT", "GOOGL"])

    def test_resolve_tickers_requires_env_var(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "STOCK_TICKERS"):
                stockbot.resolve_tickers()

    def test_resolve_tickers_rejects_empty_values(self) -> None:
        with patch.dict(os.environ, {"STOCK_TICKERS": " , , "}, clear=True):
            with self.assertRaisesRegex(ValueError, "non-empty ticker"):
                stockbot.resolve_tickers()

    def test_run_returns_error_when_stock_tickers_missing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
                "STOCK_PRICE_API_KEY": "api_key",
            },
            clear=True,
        ):
            result = stockbot.run()

        self.assertFalse(result["success"])
        self.assertIn("STOCK_TICKERS", result["error"])

    def test_resolve_stock_provider_defaults_to_finnhub(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(stockbot.resolve_stock_provider(), "finnhub")

    def test_resolve_stock_provider_rejects_unknown_provider(self) -> None:
        with patch.dict(os.environ, {"STOCK_PRICE_PROVIDER": "unknown"}, clear=True):
            with self.assertRaisesRegex(ValueError, "Unsupported provider"):
                stockbot.resolve_stock_provider()

    def test_resolve_stock_api_key_for_finnhub_prefers_finnhub_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FINNHUB_API_KEY": "finnhub_key",
                "STOCK_PRICE_API_KEY": "legacy_key",
            },
            clear=True,
        ):
            self.assertEqual(stockbot.resolve_stock_api_key("finnhub"), "finnhub_key")

    def test_resolve_stock_api_key_for_finnhub_falls_back_to_stock_price_key(self) -> None:
        with patch.dict(
            os.environ,
            {"STOCK_PRICE_API_KEY": "legacy_key"},
            clear=True,
        ):
            self.assertEqual(stockbot.resolve_stock_api_key("finnhub"), "legacy_key")


if __name__ == "__main__":
    unittest.main()
