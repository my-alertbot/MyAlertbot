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


if __name__ == "__main__":
    unittest.main()
