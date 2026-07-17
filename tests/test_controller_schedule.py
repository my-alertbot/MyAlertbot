from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from alertbot.common import LocationConfig
from alertbot.controller import (
    BOT_ALIASES,
    BOT_MODULES,
    AlertBotController,
    ScheduleConfig,
)
from alertbot.plugin_registry import BotSpec, PluginRegistry


class ScheduleConfigTests(unittest.TestCase):
    def test_missing_schedule_with_example_reports_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            schedule_file = config_dir / "schedule.yaml"
            example_file = config_dir / "schedule.example.yaml"
            example_file.write_text(
                "bots:\n"
                "  stock:\n"
                "    enabled: true\n"
                "    interval_minutes: 60\n",
                encoding="utf-8",
            )

            schedule = ScheduleConfig(schedule_file, private_schedule_file=None)

            self.assertEqual(schedule.list_enabled_bots(), [])
            warning = schedule.get_missing_schedule_warning()
            self.assertIsNotNone(warning)
            self.assertIn("No bot schedule exists", warning)
            self.assertIn("schedule.example.yaml", warning)

    def test_present_schedule_has_no_missing_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            schedule_file = config_dir / "schedule.yaml"
            schedule_file.write_text(
                "bots:\n"
                "  stock:\n"
                "    enabled: true\n"
                "    interval_minutes: 60\n",
                encoding="utf-8",
            )

            schedule = ScheduleConfig(schedule_file, private_schedule_file=None)

            self.assertEqual(schedule.list_enabled_bots(), ["stock"])
            self.assertIsNone(schedule.get_missing_schedule_warning())

    def test_invalid_bot_config_entries_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            schedule_file = config_dir / "schedule.yaml"
            schedule_file.write_text(
                "bots:\n"
                "  badbot: not-a-mapping\n"
                "  disabledbot:\n"
                "    enabled: 'false'\n"
                "    interval_minutes: 30\n"
                "  goodbot:\n"
                "    enabled: 'true'\n"
                "    interval_minutes: '15'\n",
                encoding="utf-8",
            )

            schedule = ScheduleConfig(schedule_file, private_schedule_file=None)

            self.assertEqual(schedule.list_enabled_bots(), ["goodbot"])
            self.assertIsNone(schedule.get_bot_config("badbot"))
            self.assertEqual(schedule.get_interval_minutes("goodbot"), 15)

    def test_invalid_interval_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            schedule_file = config_dir / "schedule.yaml"
            schedule_file.write_text(
                "bots:\n"
                "  badinterval:\n"
                "    enabled: true\n"
                "    interval_minutes: abc\n"
                "  nonpositive:\n"
                "    enabled: true\n"
                "    interval_minutes: 0\n",
                encoding="utf-8",
            )

            schedule = ScheduleConfig(schedule_file, private_schedule_file=None)

            self.assertIsNone(schedule.get_interval_minutes("badinterval"))
            self.assertIsNone(schedule.get_interval_minutes("nonpositive"))


class StartupMessageTests(unittest.TestCase):
    @patch("alertbot.controller.load_location")
    def test_build_startup_message_includes_missing_schedule_warning(self, load_location_mock) -> None:
        load_location_mock.return_value = LocationConfig(
            city="london",
            display_name="London, United Kingdom",
            latitude=51.5074,
            longitude=-0.1278,
            timezone="Europe/London",
            country_code="GB",
        )

        schedule = MagicMock()
        schedule.list_enabled_bots.return_value = []
        schedule.get_missing_schedule_warning.return_value = (
            "No bot schedule exists at configs/schedule.yaml. "
            "Copy configs/schedule.example.yaml to configs/schedule.yaml and edit it."
        )

        controller = AlertBotController.__new__(AlertBotController)
        controller.schedule = schedule

        message = controller._build_startup_message()

        self.assertIn("Active bots: none", message)
        self.assertIn("No bot schedule exists", message)


class TelegramHandlerSyncTests(unittest.TestCase):
    def test_sync_bot_command_handlers_adds_new_commands_once(self) -> None:
        controller = AlertBotController.__new__(AlertBotController)
        controller.telegram_app = MagicMock()
        controller.schedule = MagicMock()
        controller.schedule.config = {"bots": {}}
        controller._registered_bot_commands = set()
        controller._make_handler = MagicMock(return_value=MagicMock())
        controller.registry = MagicMock(spec=PluginRegistry)
        controller.registry.bot_commands.return_value = {}

        expected_base_commands = set(BOT_MODULES.keys()) | set(BOT_ALIASES.keys())

        with patch("alertbot.controller.CommandHandler", side_effect=lambda name, cb: (name, cb)):
            controller._sync_bot_command_handlers()
            self.assertEqual(
                controller.telegram_app.add_handler.call_count,
                len(expected_base_commands),
            )
            self.assertEqual(controller._registered_bot_commands, expected_base_commands)

            controller._sync_bot_command_handlers()
            self.assertEqual(
                controller.telegram_app.add_handler.call_count,
                len(expected_base_commands),
            )

            controller.schedule.config = {
                "bots": {"myprivatebot": {"enabled": True, "interval_minutes": 60}}
            }
            controller._sync_bot_command_handlers()
            self.assertEqual(
                controller.telegram_app.add_handler.call_count,
                len(expected_base_commands) + 1,
            )
            self.assertIn("myprivatebot", controller._registered_bot_commands)


class SchedulerSyncTests(unittest.TestCase):
    def test_sync_scheduler_jobs_skips_unavailable_bot_modules(self) -> None:
        controller = AlertBotController.__new__(AlertBotController)
        controller.scheduler = MagicMock()
        controller.scheduler.get_jobs.return_value = []
        controller.schedule = MagicMock()
        controller.schedule.list_enabled_bots.return_value = ["ghostbot", "stock"]
        controller.schedule.is_bot_manual_only.return_value = False
        controller.schedule.get_interval_minutes.return_value = 15
        controller.runner = MagicMock()

        def _availability(bot_name: str) -> tuple[bool, str]:
            if bot_name == "ghostbot":
                return False, "ghostbot"
            return True, "stockbot"

        controller.runner.is_bot_available.side_effect = _availability
        controller._scheduled_job_wrapper = MagicMock()

        controller._sync_scheduler_jobs()

        controller.scheduler.add_job.assert_called_once()
        scheduled_args = controller.scheduler.add_job.call_args.kwargs.get("args")
        self.assertEqual(scheduled_args, ["stock"])


class TelegramCommandRegistrationTests(unittest.TestCase):
    def test_register_commands_uses_plugin_command_names(self) -> None:
        controller = AlertBotController.__new__(AlertBotController)
        controller.schedule = MagicMock()
        controller.schedule.list_enabled_bots.return_value = ["pluginbot"]
        controller.registry = MagicMock(spec=PluginRegistry)
        controller.registry.bot_specs.return_value = {
            "pluginbot": BotSpec(
                bot_id="pluginbot",
                bot_command="plugincmd",
                module_path="fakepkg.pluginbot",
                distribution_name="fake-pkg",
            )
        }
        controller.telegram_app = MagicMock()
        controller.telegram_app.bot = MagicMock()
        controller.telegram_app.bot.set_my_commands = AsyncMock()

        asyncio.run(controller._register_commands())

        commands = controller.telegram_app.bot.set_my_commands.call_args.args[0]
        command_names = [c.command for c in commands]
        self.assertIn("plugincmd", command_names)
        self.assertNotIn("pluginbot", command_names)


class BotRunnerRunTimeoutTests(unittest.TestCase):
    """Regression: a bot run that blocks forever must release its asyncio lock.

    Without a run-timeout guard, ``await asyncio.to_thread(module.run)`` never
    returns, ``async with lock:`` never exits, and every later scheduled fire
    logs 'previous run still active' until the process restarts (the
    yearnstratchangebot symptom).
    """

    def _make_runner(self, timeout_seconds: int):
        import threading
        from alertbot.controller import BotRunner

        schedule = MagicMock()
        schedule.is_bot_manual_only.return_value = False
        schedule.get_interval_minutes.return_value = 60
        schedule.get_controller_run_timeout_seconds.return_value = timeout_seconds

        state = MagicMock()
        state.get_last_run.return_value = None

        runner = BotRunner(state=state, schedule=schedule, registry=None)

        hang_event = threading.Event()

        class FakeBot:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, manual_trigger: bool = False, schedule_context=None) -> dict:
                self.calls += 1
                if self.calls == 1:
                    # Simulate a run that hangs indefinitely.
                    hang_event.wait(timeout=30)
                return {"success": True, "alerts_sent": 0}

        fake = FakeBot()
        runner._loaded_modules["fakemod"] = fake
        return runner, fake, hang_event

    def test_stuck_run_times_out_and_releases_lock(self) -> None:
        import os

        runner, fake, hang_event = self._make_runner(timeout_seconds=1)
        old_env = os.environ.pop("ALERTBOT_RUN_TIMEOUT_SECONDS", None)

        async def scenario() -> tuple[dict, dict]:
            first = await runner.run_scheduled("fakemod")
            hang_event.set()  # release the stuck worker thread
            second = await runner.run_scheduled("fakemod")
            return first, second

        try:
            first, second = asyncio.run(scenario())
        finally:
            if old_env is not None:
                os.environ["ALERTBOT_RUN_TIMEOUT_SECONDS"] = old_env

        # First run: timed out, surfaced as a failure (not a silent skip).
        self.assertFalse(first["success"])
        self.assertIn("timed out", first["error"])

        # The lock must have been released, so the second run executes the bot
        # instead of being skipped as 'previous run still active'.
        self.assertTrue(second["success"])
        self.assertNotIn("previous run still active", second.get("message", ""))
        self.assertEqual(fake.calls, 2, "second run must execute the bot, proving the lock was freed")

    def test_timeout_config_resolution(self) -> None:
        import os
        from alertbot.controller import BotRunner

        schedule = MagicMock()
        schedule.get_controller_run_timeout_seconds.return_value = 600
        runner = BotRunner(state=MagicMock(), schedule=schedule, registry=None)

        old = os.environ.get("ALERTBOT_RUN_TIMEOUT_SECONDS")
        try:
            os.environ["ALERTBOT_RUN_TIMEOUT_SECONDS"] = "0"
            self.assertIsNone(runner._run_timeout_seconds(), "0 must disable the guard")
            os.environ["ALERTBOT_RUN_TIMEOUT_SECONDS"] = "2"
            self.assertEqual(runner._run_timeout_seconds(), 2.0)
        finally:
            if old is None:
                os.environ.pop("ALERTBOT_RUN_TIMEOUT_SECONDS", None)
            else:
                os.environ["ALERTBOT_RUN_TIMEOUT_SECONDS"] = old


if __name__ == "__main__":
    unittest.main()
