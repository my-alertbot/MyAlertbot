from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alertbot.bots import txbot


class ParseWatchAddressesTests(unittest.TestCase):
    def test_parse_watch_addresses_merges_dedupes_and_normalizes(self) -> None:
        result = txbot.parse_watch_addresses(
            "0xABC,0xdef",
            ["0xabc", " 0x123 "],
        )
        self.assertEqual(result, ["0xabc", "0xdef", "0x123"])


class ResolveConfigPathTests(unittest.TestCase):
    def test_falls_back_to_default_private_when_env_path_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            default_private = tmp_path / "configs" / "private" / "txbot.config.json"
            default_private.parent.mkdir(parents=True, exist_ok=True)
            default_private.write_text("{}", encoding="utf-8")
            legacy = tmp_path / "configs" / "txbot.config.json"

            with (
                patch.dict("os.environ", {"TXBOT_CONFIG": str(tmp_path / "missing.json")}, clear=True),
                patch.object(txbot, "DEFAULT_CONFIG_PATH", default_private),
                patch.object(txbot, "LEGACY_DEFAULT_CONFIG_PATH", legacy),
            ):
                self.assertEqual(txbot.resolve_txbot_config_path(), default_private)


class TransactionHelperTests(unittest.TestCase):
    def test_tx_from_address_supports_nested_blockscout_shape(self) -> None:
        tx = {"from": {"hash": "0xAAA"}}
        self.assertEqual(txbot.tx_from_address(tx), "0xaaa")

    def test_parse_iso_from_seconds_falls_back_to_iso_now_on_invalid_input(self) -> None:
        with patch.object(txbot, "iso_now", return_value="fallback"):
            self.assertEqual(txbot.parse_iso_from_seconds("not-a-number"), "fallback")


class EtherscanTxlistTests(unittest.TestCase):
    @patch.object(txbot.time, "sleep")
    @patch.object(txbot, "request_json")
    def test_etherscan_txlist_retries_transient_busy_payload_and_recovers(
        self,
        request_json_mock,
        sleep_mock,
    ) -> None:
        request_json_mock.side_effect = [
            {
                "status": "0",
                "message": "Unexpected error, timeout or server too busy. Please try again later",
                "result": None,
            },
            {
                "status": "1",
                "message": "OK",
                "result": [{"hash": "0x1"}],
            },
        ]

        txs = txbot.etherscan_txlist(
            api_url="https://api.etherscan.io/v2/api",
            api_key="key",
            chain_id=1,
            address="0xabc",
            startblock=0,
            page=1,
            offset=100,
        )

        self.assertEqual(txs, [{"hash": "0x1"}])
        self.assertEqual(request_json_mock.call_count, 2)
        sleep_mock.assert_called_once()

    @patch.object(txbot.time, "sleep")
    @patch.object(txbot, "request_json")
    def test_etherscan_txlist_raises_after_retries_for_transient_busy_payload(
        self,
        request_json_mock,
        sleep_mock,
    ) -> None:
        request_json_mock.return_value = {
            "status": "0",
            "message": "Unexpected error, timeout or server too busy. Please try again later",
            "result": None,
        }

        with self.assertRaises(RuntimeError) as ctx:
            txbot.etherscan_txlist(
                api_url="https://api.etherscan.io/v2/api",
                api_key="key",
                chain_id=1,
                address="0xabc",
                startblock=0,
                page=1,
                offset=100,
            )

        self.assertIn("etherscan txlist invalid result", str(ctx.exception))
        self.assertEqual(request_json_mock.call_count, txbot.ETHERSCAN_SOFT_ERROR_MAX_RETRIES + 1)
        self.assertEqual(sleep_mock.call_count, txbot.ETHERSCAN_SOFT_ERROR_MAX_RETRIES)


class SpamFilterTests(unittest.TestCase):
    def test_filters_zero_value_contract_calls_when_enabled(self) -> None:
        tx = {"value": "0", "input": "0xa9059cbb"}
        self.assertTrue(
            txbot.is_spam_tx(
                tx,
                min_native_value=0,
                ignore_zero_value_contract_calls=True,
                max_tx_age_minutes=0,
            )
        )

    def test_filters_below_min_native_value(self) -> None:
        tx = {"value": str(10**17), "input": "0x"}
        self.assertTrue(
            txbot.is_spam_tx(
                tx,
                min_native_value=1.0,
                ignore_zero_value_contract_calls=False,
                max_tx_age_minutes=0,
            )
        )

    def test_filters_old_transactions_when_age_limit_enabled(self) -> None:
        tx = {"timeStamp": "1", "value": str(10**18), "input": "0x"}
        self.assertTrue(
            txbot.is_spam_tx(
                tx,
                min_native_value=0,
                ignore_zero_value_contract_calls=False,
                max_tx_age_minutes=1,
            )
        )

    def test_allows_recent_non_spam_value_transfer(self) -> None:
        tx = {"timeStamp": "9999999999", "value": str(2 * 10**18), "input": "0x"}
        self.assertFalse(
            txbot.is_spam_tx(
                tx,
                min_native_value=1.0,
                ignore_zero_value_contract_calls=True,
                max_tx_age_minutes=0,
            )
        )


if __name__ == "__main__":
    unittest.main()
