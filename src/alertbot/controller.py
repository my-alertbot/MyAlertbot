#!/usr/bin/env python3
"""AlertBot Controller - Central scheduler and Telegram command handler.

This is the main entry point for the AlertBot system. It:
1. Reads a local schedule from configs/schedule.yaml for bot scheduling configuration
2. Uses APScheduler to run bots automatically
3. Handles Telegram bot commands for manual triggers
4. Maintains state (last run times) in controller_state.json

Bots not listed in configs/schedule.yaml are disabled by default.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# Import common utilities
from alertbot.common import (
    CONFIG_DIR,
    STATE_DIR,
    load_env_file,
    setup_logging,
    load_location,
    save_location,
    geocode_city,
)
from alertbot.transport_manager import close_transport, send_alert_async
from alertbot.bots.calendar_commands import addevent_usage, parse_addevent_request
from alertbot.bots.calendar_ics import load_calendar_events_from_ics, delete_event_from_ics
from alertbot.plugin_registry import PluginRegistry

# Constants
SCHEDULE_FILE = CONFIG_DIR / "schedule.yaml"
PRIVATE_SCHEDULE_FILE = CONFIG_DIR / "privateschedule.yaml"
STATE_FILE = STATE_DIR / "controller_state.json"
DEFAULT_LOG_LEVEL = "INFO"

# Canonical bot module mapping: bot name -> module path
BOT_MODULES = {
    "stock": "stockbot",
    "stockalert": "stockalertbot",
    "crypto": "cryptobot",
    "airquality": "airqualitybot",
    "gh": "ghbot",
    "calendar": "calendarbot",
    "rss": "rssbot",
    "yt": "ytbot",
    "rain": "rainbot",
    "weather": "weatherbot",
    "sunrise": "sunrisebot",
    "tx": "txbot",
    "metals": "metalsbot",
    "aurora": "aurorabot",
    "newtopaimodelbot": "newtopaimodelbot",
    "newsubdomainbot": "newsubdomainbot",
    "geoshock": "geoshockbot",
    "luma": "lumabot",
}

# Telegram command aliases -> canonical bot names
BOT_ALIASES = {
    "stocks": "stock",
    "youtube": "yt",
}

LOCATION_DEPENDENT_BOTS = ("airquality", "rain", "weather")


def resolve_bot_name(name: str) -> str:
    """Resolve command aliases to canonical bot names."""
    return BOT_ALIASES.get(name, name)


def format_interval_minutes(minutes: int | None) -> str:
    """Format interval minutes into a compact human-readable string."""
    if minutes is None:
        return "unknown"
    if minutes % 1440 == 0:
        days = minutes // 1440
        unit = "day" if days == 1 else "days"
        return f"every {days} {unit} ({minutes}m)"
    if minutes % 60 == 0:
        hours = minutes // 60
        unit = "hour" if hours == 1 else "hours"
        return f"every {hours} {unit} ({minutes}m)"
    return f"every {minutes} min"


class ControllerState:
    """Manages controller state (last run times, etc.)."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.data = self._load()

    def _load(self) -> dict:
        """Load state from disk."""
        if not self.state_file.exists():
            return {"last_runs": {}, "version": 1}
        try:
            with open(self.state_file, "r") as f:
                return json.load(f)
        except Exception as exc:
            logging.warning("Failed to load state file: %s", exc)
            return {"last_runs": {}, "version": 1}

    def save(self) -> None:
        """Save state to disk atomically."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_file.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp_path, self.state_file)

    def get_last_run(self, bot_name: str) -> Optional[str]:
        """Get ISO timestamp of last run for a bot."""
        return self.data.get("last_runs", {}).get(bot_name)

    def set_last_run(self, bot_name: str, timestamp: str) -> None:
        """Set last run timestamp for a bot."""
        if "last_runs" not in self.data:
            self.data["last_runs"] = {}
        self.data["last_runs"][bot_name] = timestamp
        self.save()


class ScheduleConfig:
    """Manages merged schedule configuration.

    Base config is loaded from local `configs/schedule.yaml`.
    Optional local overrides/additions are loaded from `configs/privateschedule.yaml`
    and merged on top.
    """

    def __init__(
        self,
        schedule_file: Path,
        private_schedule_file: Path | None = None,
        plugin_defaults: dict | None = None,
    ):
        self.schedule_file = schedule_file
        self.schedule_example_file = self.schedule_file.with_name("schedule.example.yaml")
        self.private_schedule_file = private_schedule_file
        self.missing_required_schedule = False
        self._plugin_defaults: dict = plugin_defaults or {}
        self.config = self._load()

    @staticmethod
    def _deep_merge(base: Any, overlay: Any) -> Any:
        """Recursively merge dictionaries; overlay wins for non-dicts."""
        if not isinstance(base, dict) or not isinstance(overlay, dict):
            return overlay
        merged = dict(base)
        for key, overlay_value in overlay.items():
            if key in merged:
                merged[key] = ScheduleConfig._deep_merge(merged[key], overlay_value)
            else:
                merged[key] = overlay_value
        return merged

    def _load_yaml_file(self, path: Path) -> dict:
        if not path.exists():
            return {"bots": {}}
        try:
            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
        except Exception as exc:
            logging.error("Failed to load schedule file %s: %s", path, exc)
            return {"bots": {}}
        if not isinstance(data, dict):
            logging.error("Schedule file %s must contain a YAML mapping/object", path)
            return {"bots": {}}
        return data

    def _load(self) -> dict:
        """Load and merge base/private schedule configuration."""
        self.missing_required_schedule = not self.schedule_file.exists()
        if self.missing_required_schedule:
            if self.schedule_example_file.exists():
                logging.warning(
                    "No schedule file found at %s. Create one by copying %s.",
                    self.schedule_file,
                    self.schedule_example_file,
                )
            else:
                logging.warning("No schedule file found at %s.", self.schedule_file)

        base = self._load_yaml_file(self.schedule_file)

        # Plugin defaults layer: sits between base and private overlay in precedence.
        # Merge order: base < plugin defaults < privateschedule.yaml (highest).
        plugin_layer: dict = {"bots": self._plugin_defaults} if self._plugin_defaults else {}

        private = {}
        if self.private_schedule_file is not None:
            private = self._load_yaml_file(self.private_schedule_file)
            if self.private_schedule_file.exists():
                logging.info(
                    "Loaded private schedule overlay from %s",
                    self.private_schedule_file,
                )

        merged = self._deep_merge(base, plugin_layer)
        merged = self._deep_merge(merged, private)
        if "bots" not in merged or not isinstance(merged.get("bots"), dict):
            merged["bots"] = {}
        return merged

    def get_missing_schedule_warning(self) -> str | None:
        """Return a user-facing warning when the required local schedule is missing."""
        if not self.missing_required_schedule:
            return None
        if self.schedule_example_file.exists():
            return (
                "No bot schedule exists at configs/schedule.yaml. "
                "Copy configs/schedule.example.yaml to configs/schedule.yaml and edit it."
            )
        return "No bot schedule exists at configs/schedule.yaml."

    def reload(self, plugin_defaults: dict | None = None) -> None:
        """Reload configuration from disk, optionally updating plugin defaults."""
        if plugin_defaults is not None:
            self._plugin_defaults = plugin_defaults
        self.config = self._load()

    @staticmethod
    def _parse_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on", "y"}:
                return True
            if normalized in {"0", "false", "no", "off", "n"}:
                return False
        return default

    def _get_bot_config_dict(self, bot_name: str) -> Optional[dict]:
        bot_config = self.config.get("bots", {}).get(bot_name)
        if bot_config is None:
            return None
        if not isinstance(bot_config, dict):
            logging.warning(
                "Invalid schedule entry for bot '%s': expected mapping/object, got %s",
                bot_name,
                type(bot_config).__name__,
            )
            return None
        return bot_config

    def is_bot_enabled(self, bot_name: str) -> bool:
        """Check if bot is defined and enabled in schedule."""
        bot_config = self._get_bot_config_dict(bot_name)
        if not bot_config:
            return False
        return self._parse_bool(bot_config.get("enabled", True), default=True)

    def get_bot_config(self, bot_name: str) -> Optional[dict]:
        """Get configuration for a specific bot."""
        return self._get_bot_config_dict(bot_name)

    def get_interval_minutes(self, bot_name: str) -> Optional[int]:
        """Get interval in minutes for a bot."""
        config = self.get_bot_config(bot_name)
        if not config:
            return None
        raw_interval = config.get("interval_minutes")
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            logging.warning(
                "Invalid interval_minutes for bot '%s': expected positive integer, got %r",
                bot_name,
                raw_interval,
            )
            return None
        if interval <= 0:
            logging.warning(
                "Invalid interval_minutes for bot '%s': expected > 0, got %r",
                bot_name,
                raw_interval,
            )
            return None
        return interval

    def is_bot_manual_only(self, bot_name: str) -> bool:
        """Check whether a bot is enabled for manual runs only."""
        config = self.get_bot_config(bot_name)
        if not config:
            return False
        return self._parse_bool(config.get("manual_only", False), default=False)

    def list_enabled_bots(self) -> list[str]:
        """List all enabled bot names."""
        bots = []
        for name in self.config.get("bots", {}):
            if self.is_bot_enabled(name):
                bots.append(name)
        return bots

    def get_telegram_drop_pending_updates(self, default: bool = True) -> bool:
        """Get telegram.drop_pending_updates setting."""
        raw = self.config.get("telegram", {}).get("drop_pending_updates", default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on", "y"}
        return default

    def get_controller_config_reload_minutes(self, default: int = 0) -> int:
        """Get controller.config_reload_minutes setting."""
        raw = self.config.get("controller", {}).get("config_reload_minutes", default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(0, value)


class BotRunner:
    """Handles loading and running bot modules."""

    def __init__(
        self,
        state: ControllerState,
        schedule: ScheduleConfig,
        registry: PluginRegistry | None = None,
    ):
        self.state = state
        self.schedule = schedule
        self._registry = registry
        self._loaded_modules: Dict[str, Any] = {}
        self._bot_locks: Dict[str, asyncio.Lock] = {}

    def _get_bot_lock(self, bot_name: str) -> asyncio.Lock:
        """Get or create an asyncio lock for a bot."""
        lock = self._bot_locks.get(bot_name)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_locks[bot_name] = lock
        return lock

    def _load_module(self, module_name: str) -> Optional[Any]:
        """Load a bot module by name."""
        if module_name in self._loaded_modules:
            return self._loaded_modules[module_name]

        public_path = f"alertbot.bots.{module_name}"
        try:
            module = importlib.import_module(public_path)
            self._loaded_modules[module_name] = module
            return module
        except ModuleNotFoundError as exc:
            # Only fall back to the private namespace when the public module itself
            # is missing; do not mask missing dependencies inside a public module.
            if exc.name != public_path:
                logging.error("Failed to load module %s: %s", module_name, exc)
                return None
        except Exception as exc:
            logging.error("Failed to load module %s: %s", module_name, exc)
            return None

        if "." in module_name:
            logging.error("Failed to load module %s: not found", module_name)
            return None

        # Plugin registry: try entry-point registered modules before private fallback.
        if self._registry is not None:
            specs = self._registry.bot_specs()
            if module_name in specs:
                plugin_path = specs[module_name].module_path
                try:
                    module = importlib.import_module(plugin_path)
                    self._loaded_modules[module_name] = module
                    logging.info(
                        "Loaded plugin bot module %s (%s)", module_name, plugin_path
                    )
                    return module
                except Exception as exc:
                    logging.error(
                        "Failed to load plugin module %s (%s): %s",
                        module_name,
                        plugin_path,
                        exc,
                    )

        # Private bot fallback: src/alertbot/bots/private/<name>.py
        private_path = f"alertbot.bots.private.{module_name}"
        try:
            module = importlib.import_module(private_path)
            self._loaded_modules[module_name] = module
            logging.info("Loaded private bot module %s (%s)", module_name, private_path)
            return module
        except Exception as exc:
            logging.error("Failed to load module %s: %s", module_name, exc)
            return None

    def _iso_now(self) -> str:
        """Get current UTC time as ISO string."""
        return datetime.now(timezone.utc).isoformat()

    def _resolve_invoked_bot_name(self, bot_name: str) -> str:
        """Resolve aliases and plugin commands to canonical bot IDs."""
        canonical_name = resolve_bot_name(bot_name)
        if self._registry is None:
            return canonical_name
        bot_commands = self._registry.bot_commands()
        if not isinstance(bot_commands, dict):
            return canonical_name
        mapped_name = bot_commands.get(canonical_name)
        return mapped_name if isinstance(mapped_name, str) else canonical_name

    @staticmethod
    def _resolve_module_name(bot_name: str) -> str:
        return BOT_MODULES.get(bot_name, bot_name)

    def is_bot_available(self, bot_name: str) -> tuple[bool, str]:
        """Return whether a bot name resolves to an importable module."""
        resolved_name = self._resolve_invoked_bot_name(bot_name)
        module_name = self._resolve_module_name(resolved_name)
        return self._load_module(module_name) is not None, module_name

    async def run_scheduled(self, bot_name: str) -> dict:
        """Run a bot on schedule with proper context."""
        bot_name = self._resolve_invoked_bot_name(bot_name)
        if self.schedule.is_bot_manual_only(bot_name):
            msg = f"Skipped scheduled {bot_name}: manual-only bot"
            logging.info(msg)
            return {"success": True, "alerts_sent": 0, "message": msg}
        lock = self._get_bot_lock(bot_name)
        if lock.locked():
            msg = f"Skipped scheduled {bot_name}: previous run still active"
            logging.warning(msg)
            return {"success": True, "alerts_sent": 0, "message": msg}

        module_name = self._resolve_module_name(bot_name)
        module = self._load_module(module_name)
        if not module:
            return {"success": False, "error": f"Module {module_name} not found"}

        interval = self.schedule.get_interval_minutes(bot_name)
        if interval is None:
            return {"success": False, "error": f"No interval configured for {bot_name}"}

        last_run = self.state.get_last_run(bot_name)
        this_run = self._iso_now()

        schedule_context = {
            "interval_minutes": interval,
            "last_run": last_run,
            "this_run": this_run,
            "bot_name": bot_name,
        }

        async with lock:
            try:
                # Run in thread pool to avoid blocking
                if hasattr(module, "run"):
                    result = await asyncio.to_thread(
                        module.run,
                        manual_trigger=False,
                        schedule_context=schedule_context,
                    )
                else:
                    # Fallback to main() for unrefactored bots
                    result = await asyncio.to_thread(module.main)
                    result = {"success": result == 0}

                # Update state on success
                if isinstance(result, dict) and result.get("success"):
                    self.state.set_last_run(bot_name, this_run)

                return result if isinstance(result, dict) else {"success": True}

            except Exception as exc:
                logging.exception("Bot %s failed", bot_name)
                return {"success": False, "error": str(exc)}

    async def run_manual(
        self, bot_name: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> dict:
        """Run a bot manually via Telegram command."""
        requested_name = bot_name
        bot_name = self._resolve_invoked_bot_name(bot_name)

        # Check if bot is enabled
        if not self.schedule.is_bot_enabled(bot_name):
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ Bot '{requested_name}' is not enabled. "
                    "Add it to configs/schedule.yaml or configs/privateschedule.yaml to enable. "
                    "(If missing, copy configs/schedule.example.yaml to configs/schedule.yaml.)"
                ),
            )
            return {"success": False, "error": "Bot not enabled"}

        module_name = self._resolve_module_name(bot_name)
        module = self._load_module(module_name)
        if not module:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Module for '{bot_name}' not found.",
            )
            return {"success": False, "error": "Module not found"}

        lock = self._get_bot_lock(bot_name)
        if lock.locked():
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ Bot '{bot_name}' is already running. Try again shortly.",
            )
            return {"success": False, "error": "Bot already running", "alerts_sent": 0}

        # Send initial acknowledgment
        ack_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔄 Running {bot_name}...",
        )

        try:
            async with lock:
                # Run in thread pool
                if hasattr(module, "run"):
                    result = await asyncio.to_thread(
                        module.run,
                        manual_trigger=True,
                        chat_id=str(chat_id),
                    )
                else:
                    # Fallback to main() for unrefactored bots
                    result = await asyncio.to_thread(module.main)
                    result = {"success": result == 0}

                # Send result if bot didn't send its own message
                if isinstance(result, dict):
                    if not result.get("alerts_sent") and result.get("message"):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=result["message"],
                        )
                    elif not result.get("success"):
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"❌ Error: {result.get('error', 'Unknown error')}",
                        )

                return result if isinstance(result, dict) else {"success": True}

        except Exception as exc:
            logging.exception("Manual run of %s failed", bot_name)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error running {bot_name}: {exc}",
            )
            return {"success": False, "error": str(exc)}

        finally:
            # Delete the "Running..." acknowledgment message
            try:
                await ack_msg.delete()
            except Exception:
                pass  # Ignore if deletion fails


class AlertBotController:
    """Main controller class."""

    def __init__(self):
        self.state = ControllerState(STATE_FILE)
        self.registry = PluginRegistry(
            frozenset(BOT_MODULES.keys()),
            core_reserved_commands=frozenset(set(BOT_MODULES.keys()) | set(BOT_ALIASES.keys())),
        )
        self.registry.refresh()
        self.schedule = ScheduleConfig(
            SCHEDULE_FILE,
            PRIVATE_SCHEDULE_FILE,
            plugin_defaults=self.registry.merged_schedule_defaults(),
        )
        self.runner = BotRunner(self.state, self.schedule, self.registry)
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.telegram_app: Optional[Application] = None
        self._registered_bot_commands: set[str] = set()

    def _get_telegram_credentials(self) -> tuple[str, str]:
        """Get Telegram credentials from environment."""
        load_env_file()
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set")
        return token, chat_id or ""

    def _sync_scheduler_jobs(self) -> None:
        """Sync APScheduler jobs from current schedule config."""
        if self.scheduler is None:
            return

        desired_jobs: Dict[str, tuple[str, int]] = {}
        for bot_name in self.schedule.list_enabled_bots():
            is_available, module_name = self.runner.is_bot_available(bot_name)
            if not is_available:
                logging.warning(
                    "Bot '%s' is enabled but module '%s' is not importable; skipping schedule job",
                    bot_name,
                    module_name,
                )
                continue
            if self.schedule.is_bot_manual_only(bot_name):
                logging.info("Manual-only bot %s: no scheduled job", bot_name)
                continue
            interval = self.schedule.get_interval_minutes(bot_name)
            if not interval:
                logging.warning("No interval for bot %s, skipping", bot_name)
                continue
            job_id = f"scheduled_{bot_name}"
            desired_jobs[job_id] = (bot_name, interval)
            self.scheduler.add_job(
                self._scheduled_job_wrapper,
                trigger=IntervalTrigger(minutes=interval),
                args=[bot_name],
                id=job_id,
                replace_existing=True,
                max_instances=1,  # Don't overlap scheduled runs of same bot
            )
            logging.info("Scheduled %s to run every %d minutes", bot_name, interval)

        existing_bot_job_ids = {
            job.id
            for job in self.scheduler.get_jobs()
            if job.id.startswith("scheduled_")
        }
        for job_id in sorted(existing_bot_job_ids - set(desired_jobs.keys())):
            self.scheduler.remove_job(job_id)
            logging.info("Removed scheduler job: %s", job_id)

    def _setup_scheduler(self) -> None:
        """Set up APScheduler with jobs from configs/schedule.yaml."""
        logging.getLogger("apscheduler").setLevel(logging.WARNING)
        self.scheduler = AsyncIOScheduler()
        self._sync_scheduler_jobs()
        self._sync_reload_schedule_job()

    def _sync_reload_schedule_job(self) -> None:
        """Ensure config reload job matches current controller settings."""
        if self.scheduler is None:
            return

        reload_minutes = self.schedule.get_controller_config_reload_minutes(default=0)
        existing_job = self.scheduler.get_job("controller_reload_schedule")

        if reload_minutes <= 0:
            if existing_job is not None:
                self.scheduler.remove_job("controller_reload_schedule")
                logging.info("Disabled periodic config reload job")
            return

        self.scheduler.add_job(
            self._reload_schedule_job_wrapper,
            trigger=IntervalTrigger(minutes=reload_minutes),
            id="controller_reload_schedule",
            replace_existing=True,
            max_instances=1,
        )
        if existing_job is None:
            logging.info("Scheduled config reload every %d minute(s)", reload_minutes)

    async def _scheduled_job_wrapper(self, bot_name: str) -> None:
        """Wrapper for scheduled jobs with error handling."""
        logging.info("Running scheduled job: %s", bot_name)
        result = await self.runner.run_scheduled(bot_name)
        if result.get("success"):
            logging.info("Scheduled job %s completed successfully", bot_name)
        else:
            logging.error(
                "Scheduled job %s failed: %s", bot_name, result.get("error")
            )

    async def _reload_schedule_job_wrapper(self) -> None:
        """Periodic schedule reload job."""
        logging.info(
            "Reloading merged schedule config from %s%s",
            SCHEDULE_FILE,
            f" + {PRIVATE_SCHEDULE_FILE}" if PRIVATE_SCHEDULE_FILE.exists() else "",
        )
        self.registry.refresh()
        self.schedule.reload(plugin_defaults=self.registry.merged_schedule_defaults())
        self._sync_scheduler_jobs()
        self._sync_reload_schedule_job()
        if self.telegram_app is not None:
            try:
                self._sync_bot_command_handlers()
                await self._register_commands()
            except Exception as exc:
                logging.warning("Failed to refresh Telegram commands: %s", exc)

    def _sync_bot_command_handlers(self) -> None:
        """Ensure Telegram handlers exist for all configured/public bot commands."""
        if self.telegram_app is None:
            return

        configured_bots = set(self.schedule.config.get("bots", {}).keys())
        plugin_commands = set(self.registry.bot_commands().keys()) if hasattr(self, "registry") else set()
        desired_commands = sorted(
            set(BOT_MODULES.keys()) | set(BOT_ALIASES.keys()) | configured_bots | plugin_commands
        )
        new_commands = [name for name in desired_commands if name not in self._registered_bot_commands]

        for command in new_commands:
            self.telegram_app.add_handler(CommandHandler(command, self._make_handler(command)))
            self._registered_bot_commands.add(command)

        if new_commands:
            logging.info("Added %d new Telegram bot handler(s): %s", len(new_commands), ", ".join(new_commands))

    def _setup_telegram(self) -> None:
        """Set up Telegram bot application."""
        token, _ = self._get_telegram_credentials()
        self.telegram_app = Application.builder().token(token).build()

        # Add command handlers for each bot
        self._sync_bot_command_handlers()

        # Add utility commands
        self.telegram_app.add_handler(CommandHandler("help", self._help_command))
        self.telegram_app.add_handler(CommandHandler("all", self._all_command))
        self.telegram_app.add_handler(CommandHandler("schedule", self._schedule_command))
        self.telegram_app.add_handler(CommandHandler("location", self._location_command))
        self.telegram_app.add_handler(CommandHandler("addevent", self._addevent_command))
        self.telegram_app.add_handler(CommandHandler("listevents", self._listevents_command))
        self.telegram_app.add_handler(CommandHandler("deleteevent", self._deleteevent_command))

    def _command_for_bot(self, bot_name: str) -> str:
        """Resolve a BOT_ID to its user-facing Telegram command."""
        if bot_name in BOT_MODULES:
            return bot_name
        if hasattr(self, "registry"):
            spec = self.registry.bot_specs().get(bot_name)
            if spec is not None and isinstance(spec.bot_command, str):
                return spec.bot_command
        return bot_name

    def _build_startup_message(self) -> str:
        location = load_location()
        enabled_bots = sorted(self.schedule.list_enabled_bots())
        bot_list = ", ".join(enabled_bots) if enabled_bots else "none"
        schedule_warning = self.schedule.get_missing_schedule_warning()
        warning_line = f"\n⚠️ {schedule_warning}" if schedule_warning else ""

        return (
            "✅ AlertBot activated\n\n"
            f"Location: {location.display_name}\n"
            f"Coords: {location.latitude:.4f}, {location.longitude:.4f}\n"
            f"Timezone: {location.timezone}\n"
            f"Active bots: {bot_list}"
            f"{warning_line}"
        )

    async def _send_startup_message(self) -> None:
        try:
            message = self._build_startup_message()
            result = await send_alert_async(message)
            if result is None:
                logging.warning("Startup message send failed")
                return
            logging.info("Startup message sent")
        except Exception as exc:
            logging.warning("Failed to send startup message: %s", exc)

    def _make_handler(self, bot_name: str):
        """Create a handler for a specific bot command."""

        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not update.effective_chat:
                return
            await self.runner.run_manual(
                bot_name, update.effective_chat.id, context
            )

        return handler

    async def _help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /help command."""
        if not update.effective_chat:
            return

        lines = [
            "🤖 AlertBot Commands",
            "",
            "Available bots:",
        ]

        for bot_name in sorted(self.schedule.list_enabled_bots()):
            lines.append(f"  /{self._command_for_bot(bot_name)} - Trigger {bot_name}")

        lines.extend([
            "",
            "Utility commands:",
            "  /location - Show or change location",
            "  /addevent - Add a calendar event (uses /location timezone by default)",
            "  /listevents - List events stored in calendarbot.ics",
            "  /deleteevent - Delete an event by UID/name",
            "  /schedule - List active bots and run intervals",
            "  /all - Run all enabled bots",
            "  /help - Show this help",
        ])

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="\n".join(lines),
        )

    async def _all_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /all command - run all enabled bots."""
        if not update.effective_chat:
            return

        chat_id = update.effective_chat.id
        enabled_bots = self.schedule.list_enabled_bots()

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔄 Running all {len(enabled_bots)} enabled bots...",
        )

        results = []
        for bot_name in enabled_bots:
            result = await self.runner.run_manual(bot_name, chat_id, context)
            status = "✅" if result.get("success") else "❌"
            alerts = result.get("alerts_sent", 0)
            results.append(f"{status} {bot_name}: {alerts} alert(s)")

        summary = "\n".join(results)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📊 All bots complete:\n{summary}",
        )

    async def _schedule_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /schedule command - list active bots and their intervals."""
        if not update.effective_chat:
            return

        enabled_bots = sorted(self.schedule.list_enabled_bots())
        if not enabled_bots:
            schedule_warning = self.schedule.get_missing_schedule_warning()
            if schedule_warning:
                schedule_text = f"📅 Schedule\n\n⚠️ {schedule_warning}"
            else:
                schedule_text = "📅 Schedule\n\nNo enabled bots in configs/schedule.yaml."
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=schedule_text,
            )
            return

        lines = [f"📅 Active Bot Schedule ({len(enabled_bots)} bot(s))", ""]
        for bot_name in enabled_bots:
            command_name = self._command_for_bot(bot_name)
            if self.schedule.is_bot_manual_only(bot_name):
                lines.append(f"/{command_name}: manual only")
                continue
            interval = self.schedule.get_interval_minutes(bot_name)
            lines.append(f"/{command_name}: {format_interval_minutes(interval)}")

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="\n".join(lines),
        )

    @staticmethod
    def _addevent_usage() -> str:
        return addevent_usage()

    async def _addevent_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /addevent - append a calendar event to calendarbot.ics."""
        if not update.effective_chat:
            return

        chat_id = update.effective_chat.id
        raw_message = update.effective_message.text if update.effective_message else ""
        raw_parts = (raw_message or "").split(maxsplit=1)
        body = raw_parts[1] if len(raw_parts) > 1 else ""

        try:
            parsed = parse_addevent_request(body)
        except ValueError as exc:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ {exc}",
            )
            return

        location = load_location()
        tz_name = location.timezone or "UTC"
        try:
            ZoneInfo(tz_name)
        except Exception:
            logging.warning("Invalid location timezone %s during /addevent, falling back to UTC", tz_name)
            tz_name = "UTC"

        event_tz_name = parsed.get("tz_name")
        if event_tz_name:
            try:
                ZoneInfo(event_tz_name)
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Invalid timezone: {event_tz_name}",
                )
                return

        try:
            from alertbot.bots.calendar_ics import append_calendar_event_to_ics

            result = await asyncio.to_thread(
                append_calendar_event_to_ics,
                name=parsed["name"],
                when_local=parsed["when_local"],
                recurrence=parsed["recurrence"],
                reminder_minutes=parsed["reminder_minutes"],
                message=parsed["message"],
                tz_name=event_tz_name,
                weekday=parsed.get("weekday"),
                day=parsed.get("day"),
                month=parsed.get("month"),
            )
        except Exception as exc:
            logging.exception("Failed to append calendar event")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Failed to add event: {exc}",
            )
            return

        confirmation_lines = [
            "✅ Calendar event added",
            f"📅 {result['name']}",
            f"🕐 {result['date']} {result['time']} ({event_tz_name or tz_name}{'' if event_tz_name else ', from /location'})",
        ]
        if result.get("recurrence") and result["recurrence"] != "once":
            confirmation_lines.append(f"🔁 Recurrence: {result['recurrence']}")
        if result.get("reminder_minutes", 0):
            confirmation_lines.append(f"⏰ Reminder: {result['reminder_minutes']}m before")
        if result.get("message"):
            confirmation_lines.append(f"📝 {result['message']}")
        confirmation_lines.append(f"🆔 {result['uid']}")

        if not self.schedule.is_bot_enabled("calendar"):
            confirmation_lines.append("⚠️ Note: calendar bot is currently disabled in configs/schedule.yaml")

        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(confirmation_lines),
        )

    async def _listevents_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /listevents - list events from calendarbot.ics."""
        if not update.effective_chat:
            return

        chat_id = update.effective_chat.id
        args = context.args or []

        show_all = False
        limit = 20
        for arg in args:
            lowered = arg.lower()
            if lowered == "all":
                show_all = True
                continue
            try:
                parsed_limit = int(arg)
            except ValueError:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Usage: /listevents [all] [limit]",
                )
                return
            limit = max(1, min(parsed_limit, 100))

        location = load_location()
        tz_name = location.timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
            tz_name = "UTC"

        now = datetime.now(tz)

        try:
            from alertbot.bots.calendarbot import parse_event_time

            events = await asyncio.to_thread(load_calendar_events_from_ics)
            rows: list[tuple[datetime | None, dict[str, Any]]] = []
            for event in events:
                next_time = parse_event_time(event, tz, now)
                if next_time is None and not show_all:
                    continue
                rows.append((next_time, event))
        except Exception as exc:
            logging.exception("Failed to list calendar events")
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed to list events: {exc}")
            return

        if not rows:
            await context.bot.send_message(chat_id=chat_id, text="📅 No calendar events found.")
            return

        rows.sort(key=lambda item: (item[0] is None, item[0] or now, str(item[1].get("name", "")).lower()))
        rows = rows[:limit]

        lines = [f"📅 Calendar Events ({len(rows)} shown, timezone {tz_name})"]
        for next_time, event in rows:
            recurrence = event.get("recurrence", "once")
            uid = str(event.get("id", ""))[:36]
            when_text = next_time.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if next_time else "n/a"
            lines.append(f"• {event.get('name', 'Unnamed')} [{recurrence}]")
            lines.append(f"  next: {when_text}")
            lines.append(f"  id: {uid}")
            if event.get("reminder_minutes", 0):
                lines.append(f"  reminder: {event['reminder_minutes']}m")

        lines.append("")
        lines.append("Use /deleteevent <id> to remove an event.")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))

    async def _deleteevent_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /deleteevent - delete an event by UID/name."""
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        query = " ".join(context.args or []).strip()
        if not query:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Usage: /deleteevent <event_uid_or_name>",
            )
            return

        try:
            result = await asyncio.to_thread(delete_event_from_ics, query)
        except Exception as exc:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ {exc}")
            return

        lines = [
            "🗑️ Calendar event deleted",
            f"📅 {result.get('name') or 'Unnamed'}",
            f"🆔 {result.get('uid')}",
        ]
        if result.get("recurrence"):
            lines.append(f"🔁 {result['recurrence']}")
        if result.get("date") and result.get("time"):
            lines.append(f"🕐 {result['date']} {result['time']}")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))

    async def _run_location_dependent_bots(
        self, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Run enabled location-dependent bots after a location update."""
        enabled_bots = [
            bot_name
            for bot_name in LOCATION_DEPENDENT_BOTS
            if self.schedule.is_bot_enabled(bot_name)
        ]
        if not enabled_bots:
            logging.info(
                "Location updated, but no location-dependent bots are enabled"
            )
            return

        for bot_name in enabled_bots:
            result = await self.runner.run_manual(bot_name, chat_id, context)
            if not result.get("success"):
                logging.warning(
                    "Location-triggered run failed for %s: %s",
                    bot_name,
                    result.get("error", "unknown error"),
                )

    async def _location_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /location command - show or change location."""
        if not update.effective_chat:
            return

        chat_id = update.effective_chat.id
        args = context.args

        if not args:
            # Show current location
            location = load_location()
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"📍 Current location:\n"
                    f"   City: {location.display_name}\n"
                    f"   Coordinates: {location.latitude:.4f}, {location.longitude:.4f}\n"
                    f"   Timezone: {location.timezone}\n\n"
                    f"To change: /location <city_name>\n"
                    f"Example: /location Paris"
                ),
            )
            return

        # Set new location
        city_name = " ".join(args)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 Looking up '{city_name}'...",
        )

        new_location = geocode_city(city_name)
        if not new_location:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Could not find location: '{city_name}'",
            )
            return

        save_location(new_location)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Location updated!\n"
                f"   City: {new_location.display_name}\n"
                f"   Coordinates: {new_location.latitude:.4f}, {new_location.longitude:.4f}\n"
                f"   Timezone: {new_location.timezone}"
            ),
        )
        await self._run_location_dependent_bots(chat_id, context)

    async def _register_commands(self) -> None:
        """Register bot commands with Telegram for the menu."""
        from telegram import BotCommand

        commands = []

        # Add all enabled bot commands
        for bot_name in sorted(self.schedule.list_enabled_bots()):
            command_name = self._command_for_bot(bot_name)
            description = f"Run {bot_name} check"
            commands.append(BotCommand(command_name, description))

        # Add utility commands
        commands.append(BotCommand("location", "Show or change location"))
        commands.append(BotCommand("addevent", "Add a calendar event"))
        commands.append(BotCommand("listevents", "List calendar events"))
        commands.append(BotCommand("deleteevent", "Delete a calendar event"))
        commands.append(BotCommand("schedule", "List active bot schedules"))
        commands.append(BotCommand("all", "Run all enabled bots"))
        commands.append(BotCommand("help", "Show available commands"))

        await self.telegram_app.bot.set_my_commands(commands)
        logging.info("Registered %d commands with Telegram", len(commands))

    async def run(self) -> None:
        """Main entry point."""
        load_env_file()
        setup_logging()
        logging.info("Starting AlertBot Controller")

        # Record start time
        if "started" not in self.state.data:
            self.state.data["started"] = datetime.now(timezone.utc).isoformat()
            self.state.save()

        # Set up scheduler
        self._setup_scheduler()
        self.scheduler.start()

        transport_name = os.getenv("ALERTBOT_TRANSPORT", "telegram").lower()
        if transport_name == "telegram":
            # Set up Telegram
            self._setup_telegram()

            # Start Telegram polling
            await self.telegram_app.initialize()
            await self._register_commands()
            await self.telegram_app.start()
            drop_pending_updates = self.schedule.get_telegram_drop_pending_updates(default=True)
            await self.telegram_app.updater.start_polling(
                drop_pending_updates=drop_pending_updates
            )
        else:
            logging.info("Transport '%s' does not use Telegram polling", transport_name)

        await self._send_startup_message()

        logging.info("Controller is running. Press Ctrl+C to stop.")
        print("Alertbot has started. Stop with Ctrl+C.", flush=True)

        # Keep running until interrupted
        interrupted = False
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            interrupted = True
            logging.info("Shutting down...")
        finally:
            if self.telegram_app is not None:
                await self.telegram_app.updater.stop()
                await self.telegram_app.stop()
            self.scheduler.shutdown()
            await close_transport()
            if interrupted:
                logging.info("AlertBot shutdown complete")
                print("AlertBot has shut down.", flush=True)


def main() -> int:
    """Main entry point."""
    try:
        controller = AlertBotController()
        asyncio.run(controller.run())
        return 0
    except Exception as exc:
        logging.exception("Controller failed to start")
        return 1


if __name__ == "__main__":
    sys.exit(main())
