# Contributing

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
cp configs/schedule.example.yaml configs/schedule.yaml
```

Run the controller:

```bash
alertbot
# or:
python -m alertbot
```

## How AlertBot is wired

- `src/alertbot/controller.py` is the scheduler + Telegram command router.
- `src/alertbot/bots/*.py` contains public/shared bot modules.
- `src/alertbot/bots/private/*.py` is for local/private bot modules (git-ignored by default).
- `configs/schedule.yaml` is the local scheduling source of truth.
  - Start from tracked `configs/schedule.example.yaml`.
  - Bots must be listed under `bots:`.
  - Bots not listed are treated as disabled.
  - The controller currently uses `enabled` and `interval_minutes`.

## Bot contract

New bots should expose:

```python
def run(
    manual_trigger: bool = False,
    chat_id: str | None = None,
    schedule_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

`schedule_context` (for scheduled runs) contains:
- `interval_minutes`
- `last_run`
- `this_run`
- `bot_name`

Return a structured result dict. Common keys are:
- `success` (`bool`)
- `alerts_sent` (`int`)
- `message` (`str`, optional)
- `error` (`str`, optional)

If a bot needs a lookback window, derive it from `schedule_context` with `calculate_lookback_minutes()`.

## Adding a new bot

1. Add a bot module with a `run()` function.
   Public/shared bot: `src/alertbot/bots/<name>bot.py`
   Private/local bot: `src/alertbot/bots/private/<name>.py`
2. Public/shared bots: register in `BOT_MODULES` in `src/alertbot/controller.py`.
   Private/local bots in `src/alertbot/bots/private/` are auto-discovered via schedule name fallback (no `BOT_MODULES` edit required).
3. Add it under `bots:` in `configs/schedule.yaml` with at least:

```yaml
bots:
  <name>:
    enabled: true
    interval_minutes: 60
```

4. If needed, add command aliases in `BOT_ALIASES` (`src/alertbot/controller.py`).
5. Update docs/config examples:
   - `.env.example` for env vars
   - `README.md` for user-facing setup/usage

## Env conventions

- Telegram env vars are `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (not `TG_*`).
- Required env vars use `getenv_required()` (or `getenv_required_any()` only when intentional aliases are required).
- Use bot-specific names for optional state/config overrides (for example `AQI_STATE_FILE`, `TXBOT_STATE`), not generic names like `STATE_FILE`.
- When env vars change, update `.env.example` and `README.md` in the same change.

## Practical checklist

- Keep changes in `src/` (not `build/` artifacts).
- Reuse helpers from `src/alertbot/common.py` before adding new utilities.
- Use explicit HTTP timeouts and handle failures without crashing the controller.
- Persist state atomically (`save_json()` or `.tmp` + `os.replace`).
