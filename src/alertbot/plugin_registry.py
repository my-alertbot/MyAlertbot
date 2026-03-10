"""Plugin registry for alertbot — discovers and validates plugin bots and schedules.

Plugin packages declare bots and schedule defaults via setuptools entry points:

    [project.entry-points."alertbot.bots"]
    mybotid = "mypkg.mybotmodule"

    [project.entry-points."alertbot.schedules"]
    mycollection = "mypkg:get_schedule"

Each bot module must define:
    BOT_ID: str    — stable identity; must match the entry point name
    BOT_COMMAND: str — Telegram command name (may differ from BOT_ID)
    run(...)       — standard run interface

Each schedule entry point must return a dict with api_version "v1":
    {
        "api_version": "v1",
        "bots": {
            "<bot_id>": {"enabled": True, "interval_minutes": 60}
        }
    }
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_API_VERSION = "v1"
BOTS_EP_GROUP = "alertbot.bots"
SCHEDULES_EP_GROUP = "alertbot.schedules"
_ALLOWED_SCHEDULE_KEYS = frozenset({"api_version", "bots"})


@dataclass(frozen=True)
class BotSpec:
    """Immutable descriptor for a discovered plugin bot."""

    bot_id: str
    bot_command: str
    module_path: str
    distribution_name: str


class PluginRegistry:
    """Discovers, validates, and exposes installed alertbot plugin bots and schedules.

    Usage::

        registry = PluginRegistry(core_bot_ids=frozenset(BOT_MODULES.keys()))
        registry.refresh()

        # Use snapshots — these are copies, safe to iterate while refresh() runs later.
        specs = registry.bot_specs()          # bot_id -> BotSpec
        cmds  = registry.bot_commands()       # BOT_COMMAND -> bot_id
        sched = registry.merged_schedule_defaults()  # bot_id -> schedule config dict
    """

    def __init__(
        self,
        core_bot_ids: frozenset[str],
        core_reserved_commands: frozenset[str] | None = None,
    ) -> None:
        """
        Args:
            core_bot_ids: Set of bot IDs reserved by the core (cannot be overridden).
            core_reserved_commands: Optional set of command names reserved by core
                handlers/aliases and unavailable to plugins.
        """
        self._core_bot_ids = core_bot_ids
        self._core_reserved_commands = core_reserved_commands or frozenset(core_bot_ids)
        self._bot_specs: dict[str, BotSpec] = {}
        self._bot_commands: dict[str, str] = {}
        self._schedule_defaults: dict[str, Any] = {}
        self._diagnostics: list[str] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Discover entry points and rebuild internal state.

        Safe to call multiple times (e.g., on periodic config reload).
        Existing state is fully replaced on each call.
        """
        self._bot_specs = {}
        self._bot_commands = {}
        self._schedule_defaults = {}
        self._diagnostics = []
        self._discover_bots()
        self._discover_schedules()

    def bot_specs(self) -> dict[str, BotSpec]:
        """Return a snapshot of registered bots keyed by BOT_ID."""
        return dict(self._bot_specs)

    def bot_commands(self) -> dict[str, str]:
        """Return a snapshot mapping BOT_COMMAND -> BOT_ID."""
        return dict(self._bot_commands)

    def merged_schedule_defaults(self) -> dict[str, Any]:
        """Return plugin schedule defaults keyed by bot_id.

        These are intended to be merged between the base schedule.yaml and the
        private overlay: base < plugin defaults < privateschedule.yaml.
        """
        return copy.deepcopy(self._schedule_defaults)

    def diagnostics(self) -> list[str]:
        """Return logged warnings/skips from the last refresh() call."""
        return list(self._diagnostics)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dist_name(ep: Any) -> str:
        try:
            return ep.dist.name if ep.dist is not None else "unknown"
        except AttributeError:
            return "unknown"

    def _sorted_eps(self, group: str) -> list[Any]:
        """Return entry points for *group* sorted deterministically."""
        eps = entry_points(group=group)
        return sorted(eps, key=lambda ep: (self._dist_name(ep), group, ep.name))

    def _warn(self, msg: str) -> None:
        logger.warning(msg)
        self._diagnostics.append(msg)

    # ------------------------------------------------------------------
    # Bot discovery
    # ------------------------------------------------------------------

    def _discover_bots(self) -> None:
        # Pre-seed so plugins cannot claim reserved core command names.
        seen_commands: dict[str, str] = {cmd: "core" for cmd in self._core_reserved_commands}

        for ep in self._sorted_eps(BOTS_EP_GROUP):
            dist = self._dist_name(ep)
            ep_name = ep.name

            try:
                module: ModuleType = ep.load()
            except Exception as exc:
                self._warn(f"[plugin] bot '{ep_name}' ({dist}): load failed: {exc}")
                continue

            bot_id: str | None = getattr(module, "BOT_ID", None)
            if bot_id is None:
                self._warn(f"[plugin] bot '{ep_name}' ({dist}): missing BOT_ID, skipping")
                continue

            if ep_name != bot_id:
                self._warn(
                    f"[plugin] bot '{ep_name}' ({dist}): entry point name '{ep_name}' "
                    f"!= BOT_ID '{bot_id}', skipping"
                )
                continue

            if bot_id in self._core_bot_ids:
                self._warn(
                    f"[plugin] bot '{ep_name}' ({dist}): BOT_ID '{bot_id}' conflicts with core, skipping"
                )
                continue

            if bot_id in self._bot_specs:
                existing = self._bot_specs[bot_id]
                self._warn(
                    f"[plugin] bot '{ep_name}' ({dist}): BOT_ID '{bot_id}' already claimed "
                    f"by '{existing.distribution_name}', skipping"
                )
                continue

            bot_command: str | None = getattr(module, "BOT_COMMAND", None)
            if bot_command is None:
                self._warn(f"[plugin] bot '{ep_name}' ({dist}): missing BOT_COMMAND, skipping")
                continue

            if bot_command in seen_commands:
                winner = seen_commands[bot_command]
                self._warn(
                    f"[plugin] bot '{ep_name}' ({dist}): BOT_COMMAND '{bot_command}' already "
                    f"claimed by '{winner}', skipping"
                )
                continue

            if not hasattr(module, "run") and not hasattr(module, "main"):
                self._warn(
                    f"[plugin] bot '{ep_name}' ({dist}): missing run() and main(), skipping"
                )
                continue

            spec = BotSpec(
                bot_id=bot_id,
                bot_command=bot_command,
                module_path=module.__name__,
                distribution_name=dist,
            )
            self._bot_specs[bot_id] = spec
            seen_commands[bot_command] = bot_id
            self._bot_commands[bot_command] = bot_id
            logger.info(
                "[plugin] registered bot: %s (command=%s, dist=%s)", bot_id, bot_command, dist
            )

    # ------------------------------------------------------------------
    # Schedule discovery
    # ------------------------------------------------------------------

    def _discover_schedules(self) -> None:
        merged: dict[str, Any] = {}

        for ep in self._sorted_eps(SCHEDULES_EP_GROUP):
            dist = self._dist_name(ep)
            ep_name = ep.name

            try:
                obj = ep.load()
                payload: Any = obj() if callable(obj) else obj
            except Exception as exc:
                self._warn(f"[plugin] schedule '{ep_name}' ({dist}): load failed: {exc}")
                continue

            if not isinstance(payload, dict):
                self._warn(
                    f"[plugin] schedule '{ep_name}' ({dist}): payload is not a dict, skipping"
                )
                continue

            api_version = payload.get("api_version")
            if api_version != PLUGIN_API_VERSION:
                self._warn(
                    f"[plugin] schedule '{ep_name}' ({dist}): "
                    f"unknown api_version '{api_version}', skipping"
                )
                continue

            unknown_keys = set(payload) - _ALLOWED_SCHEDULE_KEYS
            if unknown_keys:
                self._warn(
                    f"[plugin] schedule '{ep_name}' ({dist}): "
                    f"unknown top-level keys {sorted(unknown_keys)}, skipping"
                )
                continue

            bots = payload.get("bots")
            if not isinstance(bots, dict):
                self._warn(
                    f"[plugin] schedule '{ep_name}' ({dist}): 'bots' is not a mapping, skipping"
                )
                continue

            for bot_id, sched_cfg in bots.items():
                if not isinstance(sched_cfg, dict):
                    self._warn(
                        f"[plugin] schedule '{ep_name}' ({dist}): bot '{bot_id}' config "
                        "is not a mapping, skipping"
                    )
                    continue
                if bot_id not in merged:
                    merged[bot_id] = copy.deepcopy(sched_cfg)
                    logger.debug("[plugin] schedule default: %s from %s", bot_id, dist)

        self._schedule_defaults = merged
