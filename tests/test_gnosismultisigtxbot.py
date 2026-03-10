from __future__ import annotations

import unittest
from unittest.mock import patch

from alertbot.bots import gnosismultisigtxbot as bot


class FetchPendingMultisigTransactionsTests(unittest.TestCase):
    @patch.object(bot, "request_json")
    def test_fetch_pending_multisig_transactions_follows_pagination(self, request_json_mock) -> None:
        request_json_mock.side_effect = [
            {
                "results": [{"safeTxHash": "0x1"}],
                "next": "https://safe/api/v1/safes/x/multisig-transactions/?page=2",
            },
            {
                "results": [{"safeTxHash": "0x2"}],
                "next": None,
            },
        ]

        txs = bot.fetch_pending_multisig_transactions(
            "https://safe-transaction-mainnet.safe.global",
            "0x1111111111111111111111111111111111111111",
        )

        self.assertEqual([tx["safeTxHash"] for tx in txs], ["0x1", "0x2"])
        self.assertEqual(len(request_json_mock.call_args_list), 2)
        first_call = request_json_mock.call_args_list[0]
        second_call = request_json_mock.call_args_list[1]
        self.assertEqual(first_call.kwargs["params"]["executed"], "false")
        self.assertIsNone(second_call.kwargs["params"])


class GnosisMultisigHelperTests(unittest.TestCase):
    def test_build_current_pending_state_dedupes_and_sorts_hashes(self) -> None:
        txs = [
            {"safeTxHash": "0xbb"},
            {"safeTxHash": "0xaa"},
            {"safeTxHash": "0xbb"},
            {"safeTxHash": ""},
        ]
        self.assertEqual(bot.build_current_pending_state(txs), ["0xaa", "0xbb"])

    def test_format_alert_message_includes_safe_link_and_method_details(self) -> None:
        tx = {
            "nonce": 7,
            "confirmationsRequired": 2,
            "confirmations": [{"owner": "0x1"}],
            "proposer": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "to": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "value": str(10**18),
            "dataDecoded": {"method": "transfer"},
            "submissionDate": "2026-02-25T12:00:00Z",
            "safeTxHash": "0xhash",
        }

        message = bot.format_alert_message(
            tx=tx,
            safe_address="0x1111111111111111111111111111111111111111",
            safe_label="Treasury Safe",
            chain_id=1,
            chain_info=bot.CHAIN_INFO_BY_ID[1],
        )

        self.assertIn("New Safe tx queued (Ethereum)", message)
        self.assertIn("Confirmations: 1/2", message)
        self.assertIn("Method: transfer", message)
        self.assertIn("Value: 1 ETH", message)
        self.assertIn("Queue: https://app.safe.global/transactions/queue?safe=eth:", message)

    def test_format_execution_alert_message_includes_onchain_details(self) -> None:
        tx = {
            "nonce": 7,
            "to": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "value": str(2 * 10**18),
            "dataDecoded": {"method": "swap"},
            "executionDate": "2026-02-25T13:00:00Z",
            "executor": "0xcccccccccccccccccccccccccccccccccccccccc",
            "transactionHash": "0xonchain",
            "safeTxHash": "0xsafe",
        }

        message = bot.format_execution_alert_message(
            tx=tx,
            safe_address="0x1111111111111111111111111111111111111111",
            safe_label="Treasury Safe",
            chain_id=1,
            chain_info=bot.CHAIN_INFO_BY_ID[1],
        )

        self.assertIn("Safe tx executed on-chain (Ethereum)", message)
        self.assertIn("Method: swap", message)
        self.assertIn("Value: 2 ETH", message)
        self.assertIn("TxHash: 0xonchain", message)
        self.assertIn("Queue: https://app.safe.global/transactions/queue?safe=eth:", message)

    def test_get_chain_info_allows_override_for_unknown_chain(self) -> None:
        chain_info, api_base = bot.get_chain_info(999999, "https://custom-safe.example/")
        self.assertIsNone(chain_info)
        self.assertEqual(api_base, "https://custom-safe.example")

    def test_get_chain_info_supports_katana(self) -> None:
        chain_info, api_base = bot.get_chain_info(747474, None)
        self.assertIsNotNone(chain_info)
        assert chain_info is not None
        self.assertEqual(chain_info.name, "Katana")
        self.assertEqual(chain_info.safe_app_slug, "katana")
        self.assertEqual(api_base, "https://safe-transaction-katana.safe.global")

    @patch.dict(
        "os.environ",
        {
            "GNOSISMULTISIGTXBOT_TARGETS_JSON": (
                '[{"chain_id":1,"safe_address":"0x1111111111111111111111111111111111111111"},'
                '{"chain_id":8453,"safe_address":"0x2222222222222222222222222222222222222222","safe_label":"Base Treasury"}]'
            )
        },
        clear=False,
    )
    def test_parse_safe_targets_from_env_supports_multiple_targets_json(self) -> None:
        targets = bot.parse_safe_targets_from_env()
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].chain_id, 1)
        self.assertEqual(targets[0].safe_address, "0x1111111111111111111111111111111111111111")
        self.assertEqual(targets[1].chain_id, 8453)
        self.assertEqual(targets[1].safe_label, "Base Treasury")


class GnosisMultisigRunTests(unittest.TestCase):
    @patch.object(bot, "save_json")
    @patch.object(bot, "send_telegram_alert")
    @patch.object(bot, "fetch_executed_multisig_transactions")
    @patch.object(bot, "fetch_pending_multisig_transactions")
    @patch.object(bot, "load_json")
    @patch.object(bot, "getenv_int_required", return_value=1)
    @patch.object(
        bot,
        "getenv_required",
        side_effect=[
            "telegram-token",
            "telegram-chat",
            "0x1111111111111111111111111111111111111111",
        ],
    )
    def test_run_alerts_when_previous_pending_tx_is_now_executed(
        self,
        _getenv_required_mock,
        _getenv_int_required_mock,
        load_json_mock,
        fetch_pending_mock,
        fetch_executed_mock,
        send_mock,
        _save_json_mock,
    ) -> None:
        load_json_mock.return_value = {
            "version": 1,
            "chain_id": 1,
            "api_base_url": "https://safe-transaction-mainnet.safe.global",
            "safes": {
                "1:0x1111111111111111111111111111111111111111": {
                    "pending_safe_tx_hashes": ["0xsafehash1"],
                }
            },
            "last_run": None,
        }
        fetch_pending_mock.return_value = []
        fetch_executed_mock.return_value = [
            {
                "safeTxHash": "0xsafehash1",
                "nonce": 5,
                "to": "0x2222222222222222222222222222222222222222",
                "value": "0",
                "executionDate": "2026-02-25T14:00:00Z",
                "transactionHash": "0xonchainhash",
            }
        ]

        result = bot.run()

        self.assertTrue(result["success"])
        self.assertEqual(result["alerts_sent"], 1)
        fetch_pending_mock.assert_called_once()
        fetch_executed_mock.assert_called_once()
        send_mock.assert_called_once()
        sent_text = send_mock.call_args.args[2]
        self.assertIn("Safe tx executed on-chain (Ethereum)", sent_text)
        self.assertIn("TxHash: 0xonchainhash", sent_text)

    @patch.object(bot, "save_json")
    @patch.object(bot, "process_safe_target")
    @patch.object(bot, "load_json", return_value={"safes": {}, "last_run": None})
    @patch.object(
        bot,
        "parse_safe_targets_from_env",
        return_value=[
            bot.SafeTarget(chain_id=1, safe_address="0x1111111111111111111111111111111111111111"),
            bot.SafeTarget(chain_id=8453, safe_address="0x2222222222222222222222222222222222222222"),
        ],
    )
    @patch.object(bot, "getenv_required", side_effect=["telegram-token", "telegram-chat"])
    def test_run_aggregates_across_multiple_chain_targets(
        self,
        _getenv_required_mock,
        _parse_targets_mock,
        _load_json_mock,
        process_target_mock,
        save_json_mock,
    ) -> None:
        process_target_mock.side_effect = [
            {
                "alerts_sent": 1,
                "queued_alerts_sent": 1,
                "executed_alerts_sent": 0,
                "pending_count": 2,
                "chain_label": "Ethereum",
                "safe_address": "0x1111111111111111111111111111111111111111",
                "chain_id": 1,
                "api_base_url": "https://safe-transaction-mainnet.safe.global",
            },
            {
                "alerts_sent": 1,
                "queued_alerts_sent": 0,
                "executed_alerts_sent": 1,
                "pending_count": 0,
                "chain_label": "Base",
                "safe_address": "0x2222222222222222222222222222222222222222",
                "chain_id": 8453,
                "api_base_url": "https://safe-transaction-base.safe.global",
            },
        ]

        result = bot.run(manual_trigger=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["alerts_sent"], 2)
        self.assertIn("2 target(s)", result["message"])
        self.assertIn("queued: 1, executed: 1", result["message"])
        self.assertEqual(process_target_mock.call_count, 2)
        save_json_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
