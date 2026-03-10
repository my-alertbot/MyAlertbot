"""Tests for alertbot.plugin_registry — covers Part 6 test matrix from modularplan.md."""

from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from alertbot.plugin_registry import (
    BOTS_EP_GROUP,
    PLUGIN_API_VERSION,
    SCHEDULES_EP_GROUP,
    BotSpec,
    PluginRegistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bot_ep(ep_name: str, dist_name: str, *, bot_id: str, bot_command: str, has_run: bool = True) -> MagicMock:
    """Fake entry point that loads a module-like object with required bot metadata."""
    module = types.SimpleNamespace(
        BOT_ID=bot_id,
        BOT_COMMAND=bot_command,
        __name__=f"fakepkg_{dist_name}.{ep_name}",
    )
    if has_run:
        module.run = lambda **kw: {"success": True}  # type: ignore[attr-defined]

    ep = MagicMock()
    ep.name = ep_name
    ep.dist = MagicMock()
    ep.dist.name = dist_name
    ep.load.return_value = module
    return ep


def _make_sched_ep(ep_name: str, dist_name: str, payload: object) -> MagicMock:
    """Fake schedule entry point that returns *payload* when the callable is invoked."""
    ep = MagicMock()
    ep.name = ep_name
    ep.dist = MagicMock()
    ep.dist.name = dist_name
    ep.load.return_value = lambda: payload
    return ep


def _mock_eps(bots: list = (), schedules: list = ()):
    """Return a side_effect for entry_points(group=...) calls."""

    def _side_effect(*, group: str) -> list:
        if group == BOTS_EP_GROUP:
            return list(bots)
        if group == SCHEDULES_EP_GROUP:
            return list(schedules)
        return []

    return _side_effect


# ---------------------------------------------------------------------------
# 6a — Registry and Discovery
# ---------------------------------------------------------------------------


class TestRegistryDiscovery(unittest.TestCase):
    def test_discovers_valid_bot_entry_point(self) -> None:
        ep = _make_bot_ep("mybot", "my-pkg", bot_id="mybot", bot_command="mybot")
        with patch("alertbot.plugin_registry.entry_points", side_effect=_mock_eps(bots=[ep])):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        specs = registry.bot_specs()
        self.assertIn("mybot", specs)
        self.assertEqual(specs["mybot"].bot_command, "mybot")
        self.assertEqual(specs["mybot"].distribution_name, "my-pkg")

    def test_discovers_valid_schedule_entry_point(self) -> None:
        payload = {
            "api_version": PLUGIN_API_VERSION,
            "bots": {"mybot": {"enabled": True, "interval_minutes": 60}},
        }
        ep = _make_sched_ep("mypkg", "my-pkg", payload)
        with patch("alertbot.plugin_registry.entry_points", side_effect=_mock_eps(schedules=[ep])):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        defaults = registry.merged_schedule_defaults()
        self.assertIn("mybot", defaults)
        self.assertEqual(defaults["mybot"]["interval_minutes"], 60)

    def test_rejects_schedule_with_invalid_api_version(self) -> None:
        payload = {"api_version": "v99", "bots": {"mybot": {}}}
        ep = _make_sched_ep("mypkg", "my-pkg", payload)
        with patch("alertbot.plugin_registry.entry_points", side_effect=_mock_eps(schedules=[ep])):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        self.assertEqual(registry.merged_schedule_defaults(), {})
        self.assertTrue(any("unknown api_version" in d for d in registry.diagnostics()))

    def test_rejects_schedule_with_unknown_top_level_keys(self) -> None:
        payload = {"api_version": PLUGIN_API_VERSION, "bots": {}, "extra_key": "bad"}
        ep = _make_sched_ep("mypkg", "my-pkg", payload)
        with patch("alertbot.plugin_registry.entry_points", side_effect=_mock_eps(schedules=[ep])):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        self.assertEqual(registry.merged_schedule_defaults(), {})
        self.assertTrue(any("unknown top-level keys" in d for d in registry.diagnostics()))

    def test_rejects_schedule_with_non_dict_bots(self) -> None:
        payload = {"api_version": PLUGIN_API_VERSION, "bots": "not-a-dict"}
        ep = _make_sched_ep("mypkg", "my-pkg", payload)
        with patch("alertbot.plugin_registry.entry_points", side_effect=_mock_eps(schedules=[ep])):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        self.assertEqual(registry.merged_schedule_defaults(), {})
        self.assertTrue(any("not a mapping" in d for d in registry.diagnostics()))

    def test_stable_deterministic_ordering_across_runs(self) -> None:
        """Two identical refresh() calls produce the same bot_specs keyset."""
        ep_a = _make_bot_ep("botA", "pkg-a", bot_id="botA", bot_command="botA")
        ep_b = _make_bot_ep("botB", "pkg-b", bot_id="botB", bot_command="botB")
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep_a, ep_b]),
        ):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()
            first = set(registry.bot_specs())
            registry.refresh()
            second = set(registry.bot_specs())

        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# 6b — Conflict Handling
# ---------------------------------------------------------------------------


class TestConflictHandling(unittest.TestCase):
    def test_duplicate_bot_id_first_in_sorted_order_wins(self) -> None:
        # pkg-a < pkg-b alphabetically, so pkg-a's ep wins
        ep_a = _make_bot_ep("mybot", "pkg-a", bot_id="mybot", bot_command="cmd1")
        ep_b = _make_bot_ep("mybot", "pkg-b", bot_id="mybot", bot_command="cmd2")
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep_a, ep_b]),
        ):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        specs = registry.bot_specs()
        self.assertIn("mybot", specs)
        self.assertEqual(specs["mybot"].distribution_name, "pkg-a")
        self.assertTrue(any("already claimed" in d for d in registry.diagnostics()))

    def test_duplicate_bot_command_first_in_sorted_order_wins(self) -> None:
        # Both bots have different BOT_IDs but the same BOT_COMMAND
        ep_a = _make_bot_ep("botA", "pkg-a", bot_id="botA", bot_command="shared_cmd")
        ep_b = _make_bot_ep("botB", "pkg-b", bot_id="botB", bot_command="shared_cmd")
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep_a, ep_b]),
        ):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        cmds = registry.bot_commands()
        self.assertEqual(cmds.get("shared_cmd"), "botA")  # pkg-a wins
        specs = registry.bot_specs()
        self.assertIn("botA", specs)
        self.assertNotIn("botB", specs)
        self.assertTrue(any("already claimed" in d for d in registry.diagnostics()))

    def test_core_bot_ids_cannot_be_overridden(self) -> None:
        ep = _make_bot_ep("stock", "evil-pkg", bot_id="stock", bot_command="stock")
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep]),
        ):
            registry = PluginRegistry(core_bot_ids=frozenset({"stock"}))
            registry.refresh()

        self.assertNotIn("stock", registry.bot_specs())
        self.assertTrue(any("conflicts with core" in d for d in registry.diagnostics()))

    def test_missing_bot_id_is_rejected(self) -> None:
        module = types.SimpleNamespace(BOT_COMMAND="cmd", run=lambda **kw: {}, __name__="fakepkg.nobot")
        ep = MagicMock()
        ep.name = "nobot"
        ep.dist.name = "bad-pkg"
        ep.load.return_value = module
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep]),
        ):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        self.assertEqual(registry.bot_specs(), {})
        self.assertTrue(any("missing BOT_ID" in d for d in registry.diagnostics()))

    def test_entry_point_name_mismatch_with_bot_id_is_rejected(self) -> None:
        ep = _make_bot_ep("epname", "pkg", bot_id="different_id", bot_command="cmd")
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep]),
        ):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        self.assertEqual(registry.bot_specs(), {})
        self.assertTrue(any("entry point name" in d for d in registry.diagnostics()))

    def test_bot_command_matching_core_id_is_rejected(self) -> None:
        """A plugin cannot steal a core command name as its BOT_COMMAND."""
        # BOT_ID is distinct from core (so BOT_ID check passes), but BOT_COMMAND = "stock"
        ep = _make_bot_ep("myplugin", "pkg", bot_id="myplugin", bot_command="stock")
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep]),
        ):
            registry = PluginRegistry(core_bot_ids=frozenset({"stock"}))
            registry.refresh()

        self.assertNotIn("myplugin", registry.bot_specs())
        self.assertTrue(any("already claimed" in d for d in registry.diagnostics()))

    def test_bot_command_matching_core_alias_is_rejected(self) -> None:
        """A plugin cannot claim reserved core alias commands (for example '/stocks')."""
        ep = _make_bot_ep("myplugin", "pkg", bot_id="myplugin", bot_command="stocks")
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep]),
        ):
            registry = PluginRegistry(
                core_bot_ids=frozenset({"stock"}),
                core_reserved_commands=frozenset({"stock", "stocks"}),
            )
            registry.refresh()

        self.assertNotIn("myplugin", registry.bot_specs())
        self.assertTrue(any("already claimed" in d for d in registry.diagnostics()))

    def test_bot_with_no_run_or_main_is_rejected(self) -> None:
        ep = _make_bot_ep("mybot", "pkg", bot_id="mybot", bot_command="mybot", has_run=False)
        with patch(
            "alertbot.plugin_registry.entry_points",
            side_effect=_mock_eps(bots=[ep]),
        ):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        self.assertEqual(registry.bot_specs(), {})
        self.assertTrue(any("missing run()" in d for d in registry.diagnostics()))


# ---------------------------------------------------------------------------
# 6c — Schedule Merge Semantics
# ---------------------------------------------------------------------------


class TestScheduleMergeSemantics(unittest.TestCase):
    def _make_schedule(self, base_yaml: str, private_yaml: str | None, plugin_defaults: dict) -> object:
        from alertbot.controller import ScheduleConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            base_file = Path(tmpdir) / "schedule.yaml"
            base_file.write_text(base_yaml, encoding="utf-8")

            private_file: Path | None = None
            if private_yaml is not None:
                private_file = Path(tmpdir) / "privateschedule.yaml"
                private_file.write_text(private_yaml, encoding="utf-8")

            sched = ScheduleConfig(base_file, private_file, plugin_defaults=plugin_defaults)
            # Return a plain dict copy of the config so tmpdir can be deleted
            import copy
            return copy.deepcopy(sched.config)

    def test_plugin_defaults_add_bots_not_in_base(self) -> None:
        config = self._make_schedule(
            base_yaml="bots:\n  basebot:\n    enabled: true\n    interval_minutes: 60\n",
            private_yaml=None,
            plugin_defaults={"pluginbot": {"enabled": True, "interval_minutes": 90}},
        )
        bots = config["bots"]  # type: ignore[index]
        self.assertIn("pluginbot", bots)
        self.assertEqual(bots["pluginbot"]["interval_minutes"], 90)

    def test_plugin_defaults_override_base_for_same_bot(self) -> None:
        # Merge order: base < plugin defaults < private.  Plugin wins over base.
        config = self._make_schedule(
            base_yaml="bots:\n  mybot:\n    enabled: true\n    interval_minutes: 60\n",
            private_yaml=None,
            plugin_defaults={"mybot": {"enabled": True, "interval_minutes": 999}},
        )
        bots = config["bots"]  # type: ignore[index]
        self.assertEqual(bots["mybot"]["interval_minutes"], 999)

    def test_private_overlay_wins_over_plugin_defaults(self) -> None:
        config = self._make_schedule(
            base_yaml="bots: {}\n",
            private_yaml="bots:\n  pluginbot:\n    enabled: false\n",
            plugin_defaults={"pluginbot": {"enabled": True, "interval_minutes": 90}},
        )
        bots = config["bots"]  # type: ignore[index]
        # private overlay sets enabled: false — must win over plugin default
        self.assertFalse(bots["pluginbot"]["enabled"])

    def test_private_overlay_can_change_interval_of_plugin_bot(self) -> None:
        config = self._make_schedule(
            base_yaml="bots: {}\n",
            private_yaml="bots:\n  pluginbot:\n    interval_minutes: 30\n",
            plugin_defaults={"pluginbot": {"enabled": True, "interval_minutes": 90}},
        )
        bots = config["bots"]  # type: ignore[index]
        self.assertEqual(bots["pluginbot"]["interval_minutes"], 30)

    def test_base_only_bot_is_preserved_when_no_plugin_conflict(self) -> None:
        config = self._make_schedule(
            base_yaml="bots:\n  basebot:\n    enabled: true\n    interval_minutes: 60\n",
            private_yaml=None,
            plugin_defaults={"otherbot": {"enabled": True, "interval_minutes": 120}},
        )
        bots = config["bots"]  # type: ignore[index]
        self.assertIn("basebot", bots)
        self.assertEqual(bots["basebot"]["interval_minutes"], 60)


class TestScheduleValidationAndSnapshots(unittest.TestCase):
    def test_rejects_non_mapping_bot_schedule_config(self) -> None:
        payload = {"api_version": PLUGIN_API_VERSION, "bots": {"mybot": 123}}
        ep = _make_sched_ep("mypkg", "my-pkg", payload)
        with patch("alertbot.plugin_registry.entry_points", side_effect=_mock_eps(schedules=[ep])):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        self.assertEqual(registry.merged_schedule_defaults(), {})
        self.assertTrue(any("is not a mapping" in d for d in registry.diagnostics()))

    def test_merged_schedule_defaults_returns_deep_copy(self) -> None:
        payload = {
            "api_version": PLUGIN_API_VERSION,
            "bots": {"mybot": {"enabled": True, "interval_minutes": 60}},
        }
        ep = _make_sched_ep("mypkg", "my-pkg", payload)
        with patch("alertbot.plugin_registry.entry_points", side_effect=_mock_eps(schedules=[ep])):
            registry = PluginRegistry(core_bot_ids=frozenset())
            registry.refresh()

        snapshot = registry.merged_schedule_defaults()
        snapshot["mybot"]["interval_minutes"] = 999
        fresh_snapshot = registry.merged_schedule_defaults()
        self.assertEqual(fresh_snapshot["mybot"]["interval_minutes"], 60)


# ---------------------------------------------------------------------------
# 6d — Controller Integration
# ---------------------------------------------------------------------------


class TestControllerIntegration(unittest.TestCase):
    def test_sync_bot_command_handlers_includes_plugin_commands(self) -> None:
        from alertbot.controller import AlertBotController
        from alertbot.plugin_registry import PluginRegistry

        controller = AlertBotController.__new__(AlertBotController)
        controller.telegram_app = MagicMock()
        controller.schedule = MagicMock()
        controller.schedule.config = {"bots": {}}
        controller._registered_bot_commands = set()
        controller._make_handler = MagicMock(return_value=MagicMock())

        registry = MagicMock(spec=PluginRegistry)
        registry.bot_commands.return_value = {"plugincmd": "pluginbot"}
        controller.registry = registry

        with patch("alertbot.controller.CommandHandler", side_effect=lambda name, cb: (name, cb)):
            controller._sync_bot_command_handlers()

        self.assertIn("plugincmd", controller._registered_bot_commands)

    def test_botrunner_is_bot_available_resolves_plugin_bot(self) -> None:
        """A bot known to the registry should be importable via the registry path."""
        import sys
        import types as _types
        from alertbot.controller import BotRunner, ControllerState, ScheduleConfig

        # Install a fake module into sys.modules so the import succeeds
        fake_module = _types.ModuleType("fakepkg.pluginbot")
        fake_module.BOT_ID = "pluginbot"  # type: ignore[attr-defined]
        fake_module.BOT_COMMAND = "pluginbot"  # type: ignore[attr-defined]
        fake_module.run = lambda **kw: {"success": True}  # type: ignore[attr-defined]
        sys.modules["fakepkg.pluginbot"] = fake_module

        try:
            registry = MagicMock(spec=PluginRegistry)
            spec = BotSpec(
                bot_id="pluginbot",
                bot_command="pluginbot",
                module_path="fakepkg.pluginbot",
                distribution_name="fake-pkg",
            )
            registry.bot_specs.return_value = {"pluginbot": spec}

            state = MagicMock(spec=ControllerState)
            schedule = MagicMock(spec=ScheduleConfig)
            runner = BotRunner(state, schedule, registry)

            available, module_name = runner.is_bot_available("pluginbot")
            self.assertTrue(available)
        finally:
            sys.modules.pop("fakepkg.pluginbot", None)

    def test_reload_path_calls_registry_refresh(self) -> None:
        """_reload_schedule_job_wrapper must call registry.refresh() then schedule.reload()."""
        from alertbot.controller import AlertBotController

        controller = AlertBotController.__new__(AlertBotController)
        controller.registry = MagicMock(spec=PluginRegistry)
        controller.registry.merged_schedule_defaults.return_value = {}
        controller.schedule = MagicMock()
        controller.scheduler = MagicMock()
        controller.scheduler.get_jobs.return_value = []
        controller.telegram_app = None
        controller.runner = MagicMock()
        controller.runner.is_bot_available.return_value = (True, "somemod")
        controller.schedule.list_enabled_bots.return_value = []
        controller.schedule.get_controller_config_reload_minutes.return_value = 0
        controller.scheduler.get_job.return_value = None

        asyncio.run(controller._reload_schedule_job_wrapper())

        controller.registry.refresh.assert_called_once()
        controller.schedule.reload.assert_called_once_with(plugin_defaults={})

    def test_manual_run_resolves_plugin_command_to_bot_id(self) -> None:
        """Manual command names from plugin BOT_COMMAND should run the BOT_ID bot."""
        import sys
        import types as _types
        from alertbot.controller import BotRunner, ControllerState, ScheduleConfig

        fake_module = _types.ModuleType("fakepkg.pluginbot")
        fake_module.BOT_ID = "pluginbot"  # type: ignore[attr-defined]
        fake_module.BOT_COMMAND = "plugincmd"  # type: ignore[attr-defined]
        fake_module.run = lambda **kw: {"success": True, "alerts_sent": 1}  # type: ignore[attr-defined]
        sys.modules["fakepkg.pluginbot"] = fake_module

        try:
            registry = MagicMock(spec=PluginRegistry)
            registry.bot_commands.return_value = {"plugincmd": "pluginbot"}
            spec = BotSpec(
                bot_id="pluginbot",
                bot_command="plugincmd",
                module_path="fakepkg.pluginbot",
                distribution_name="fake-pkg",
            )
            registry.bot_specs.return_value = {"pluginbot": spec}

            state = MagicMock(spec=ControllerState)
            schedule = MagicMock(spec=ScheduleConfig)
            schedule.is_bot_enabled.side_effect = lambda name: name == "pluginbot"
            runner = BotRunner(state, schedule, registry)

            ack_msg = MagicMock()
            ack_msg.delete = AsyncMock()
            context = MagicMock()
            context.bot.send_message = AsyncMock(return_value=ack_msg)

            result = asyncio.run(runner.run_manual("plugincmd", 12345, context))

            self.assertTrue(result.get("success"))
            schedule.is_bot_enabled.assert_any_call("pluginbot")
        finally:
            sys.modules.pop("fakepkg.pluginbot", None)


# ---------------------------------------------------------------------------
# 6e — Compatibility Coverage (legacy private bot fallback)
# ---------------------------------------------------------------------------


class TestCompatibilityCoverage(unittest.TestCase):
    def test_private_bot_fallback_still_works_with_no_plugins(self) -> None:
        """BotRunner falls back to alertbot.bots.private.<name> when public and plugin paths fail."""
        import sys
        import types as _types
        from alertbot.controller import BotRunner, ControllerState, ScheduleConfig

        fake_private = _types.ModuleType("alertbot.bots.private.legacybot")
        fake_private.BOT_NAME = "legacybot"  # type: ignore[attr-defined]
        fake_private.run = lambda **kw: {"success": True}  # type: ignore[attr-defined]
        sys.modules["alertbot.bots.private.legacybot"] = fake_private

        try:
            registry = MagicMock(spec=PluginRegistry)
            registry.bot_specs.return_value = {}  # no plugin for this bot

            state = MagicMock(spec=ControllerState)
            schedule = MagicMock(spec=ScheduleConfig)
            runner = BotRunner(state, schedule, registry)

            available, module_name = runner.is_bot_available("legacybot")
            self.assertTrue(available)
            self.assertEqual(module_name, "legacybot")
        finally:
            sys.modules.pop("alertbot.bots.private.legacybot", None)

    def test_private_bot_fallback_works_when_registry_is_none(self) -> None:
        """BotRunner without a registry still falls back to private bots."""
        import sys
        import types as _types
        from alertbot.controller import BotRunner, ControllerState, ScheduleConfig

        fake_private = _types.ModuleType("alertbot.bots.private.noregibot")
        fake_private.BOT_NAME = "noregibot"  # type: ignore[attr-defined]
        fake_private.run = lambda **kw: {"success": True}  # type: ignore[attr-defined]
        sys.modules["alertbot.bots.private.noregibot"] = fake_private

        try:
            state = MagicMock(spec=ControllerState)
            schedule = MagicMock(spec=ScheduleConfig)
            runner = BotRunner(state, schedule, registry=None)

            available, module_name = runner.is_bot_available("noregibot")
            self.assertTrue(available)
        finally:
            sys.modules.pop("alertbot.bots.private.noregibot", None)

    def test_load_failed_plugin_falls_through_to_private(self) -> None:
        """If the plugin module path can't be imported, private fallback is tried."""
        import sys
        import types as _types
        from alertbot.controller import BotRunner, ControllerState, ScheduleConfig

        fake_private = _types.ModuleType("alertbot.bots.private.fallthrubot")
        fake_private.BOT_NAME = "fallthrubot"  # type: ignore[attr-defined]
        fake_private.run = lambda **kw: {"success": True}  # type: ignore[attr-defined]
        sys.modules["alertbot.bots.private.fallthrubot"] = fake_private

        try:
            registry = MagicMock(spec=PluginRegistry)
            spec = BotSpec(
                bot_id="fallthrubot",
                bot_command="fallthrubot",
                module_path="nonexistent.package.fallthrubot",  # will fail to import
                distribution_name="bad-pkg",
            )
            registry.bot_specs.return_value = {"fallthrubot": spec}

            state = MagicMock(spec=ControllerState)
            schedule = MagicMock(spec=ScheduleConfig)
            runner = BotRunner(state, schedule, registry)

            available, module_name = runner.is_bot_available("fallthrubot")
            self.assertTrue(available, "Should fall back to private bot after plugin import failure")
        finally:
            sys.modules.pop("alertbot.bots.private.fallthrubot", None)


if __name__ == "__main__":
    unittest.main()
