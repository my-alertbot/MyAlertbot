# AlertBot — Telegram/Matrix alerting assistant

AlertBot is a collection of monitoring bots managed by a central controller. Each bot monitors a different source (air quality, stocks, stock thresholds, crypto, precious metals, GitHub, RSS, YouTube, rain, weather, aurora, geopolitical shocks, blockchain transactions, Gnosis multisig transactions, new subdomains, new top AI model leaderboard entries, calendar reminders) and sends alerts when something noteworthy happens. Alerts can be delivered via Telegram (default) or Matrix.

AI usage note: this repo may have used AI for rapid development to help build bots/plugins and integrations, but the bots do not connect to AI models while running (unless you explicitly add a bot that does so). Consider this the anti-openclaw assistant.

## Architecture

AlertBot uses a **central controller** (`src/alertbot/controller.py`) that:
- Runs continuously as a single process
- Schedules all bots according to the merged schedule config (`configs/schedule.yaml` plus an optional local overlay)
- Handles Telegram commands for manual triggers (e.g., `/stock`, `/stockalert`, `/crypto`, `/rain`, `/weather`, `/aurora`, `/geoshock`) when using Telegram polling
- Manages state and tracks last run times
- Sends a startup activation message via the active alert transport

**No cron jobs needed** — the controller handles all scheduling internally.

## Quick Start

Prerequisite: Python `3.12+` (`python3 --version`).

1. Create a virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate
```

2. Install:
```bash
pip install .
```

3. Create and configure `.env` (and optional private overlay):
```bash
cp .env.example .env
# Edit .env with your API keys
# Optional local-only overlay vars:
# touch .env.private
```

4. Create your local schedule and review enabled bots:
```bash
cp configs/schedule.example.yaml configs/schedule.yaml
# Edit configs/schedule.yaml and disable bots you have not configured yet
```

5. Set your location (default is London, UK):
```bash
# Edit state/location_state.json directly, or run the controller first
# and use the /location Telegram command:
# /location "New York"
```
See [Location Configuration](#location-configuration) for details.

6. Run the controller:
```bash
alertbot
```

## Transport Setup

### Telegram Setup (alerts + manual commands)

Use this when you want Telegram alerts and Telegram `/commands` via the controller.

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather)
   - Run `/newbot`
   - Copy the bot token into `TELEGRAM_BOT_TOKEN`
2. Start a chat with your bot and send any message (for example `hi`)
3. Get your Telegram chat ID
   - Open: `https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates`
   - Find `chat.id` in the JSON response
   - Put it into `TELEGRAM_CHAT_ID`
4. Set transport in `.env`
   - `ALERTBOT_TRANSPORT=telegram` (default)
5. Start the controller (`alertbot`)

Notes:
- Telegram is currently the only transport with controller command polling/manual commands.
- Scheduled alerts and startup messages also use Telegram when this transport is selected.

### Matrix Setup (send-only alerts)

Use this when you want scheduled alerts and startup messages sent to a Matrix room.

Matrix does not have a BotFather-style standard bot flow for this repo yet. The simplest setup is:
- create a dedicated Matrix user account (your "bot" account)
- log in with that account
- use its access token for AlertBot

1. Create a Matrix account (recommended: a dedicated bot user)
   - Example usernames: `@alertbot:your-homeserver.tld`
   - You can create it in Element (or your Matrix client) or via your homeserver admin tooling
2. Create or choose a room for alerts
3. Invite the bot account to that room and make sure it has permission to send messages
4. Get the homeserver URL
   - Example: `https://matrix-client.matrix.org`
   - This becomes `MATRIX_HOMESERVER_URL`
5. Get an access token for the bot account
   - Option A (easiest): log in as the bot account in Element and use developer tools / session token export
   - Option B (API): use Matrix login API to obtain an access token from your homeserver
   - Store it as `MATRIX_ACCESS_TOKEN`
6. Get the room ID (not alias)
   - In Element, use room settings / advanced / developer info and copy the room ID (looks like `!abc123:server`)
   - Store it as `MATRIX_ROOM_ID`
7. Set transport in `.env`
   - `ALERTBOT_TRANSPORT=matrix`
8. Start the controller (`alertbot`)

Notes:
- Matrix support is currently send-only (scheduled alerts + startup message).
- Telegram command polling/manual `/commands` are not available when running with `ALERTBOT_TRANSPORT=matrix`.
- Most existing bots still call Telegram-named helpers internally, but delivery is routed through the active transport.

## Configuration

### Environment Variables (`.env` + optional `.env.private`)

`load_env_file()` loads `.env` and then `.env.private` (if present). Existing shell
environment variables still win, and `.env.private` does not override values already set.
Use `.env.private` for local-only values and secrets you do not want in the tracked
`.env.example`.

**Required transport variables (pick one transport):**
- Telegram (`ALERTBOT_TRANSPORT=telegram`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Matrix (`ALERTBOT_TRANSPORT=matrix`): `MATRIX_HOMESERVER_URL`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID`

**Bot-specific credentials:**

| Bot | Required Variables |
|-----|-------------------|
| AQI | `AQI_API_TOKEN` (required), `AQI_STATIONS` (optional), `AQI_THRESHOLD` (optional), `AQI_MAX_AGE_HOURS` (optional), `AQI_STATE_FILE` (optional) |
| Stock | `STOCK_TICKERS` (required), plus provider key: `FINNHUB_API_KEY` (recommended for default `finnhub`) or `STOCK_PRICE_API_KEY` (required for `twelvedata`, fallback for `finnhub`) |
| Stock Alert | Same provider key requirements as Stock, plus `STOCKALERT_CONFIG` (optional), `STOCKALERT_STATE` (optional) |
| Crypto | `CRYPTOBOT_CONFIG` (optional), `CRYPTOBOT_STATE` (optional) |
| GitHub | `GH_TOKEN`, `GH_ALERT_STATE` (optional) |
| YouTube | `YT_CHANNEL_IDS`, `YT_STATE_FILE` (optional), `YT_YTDLP_BIN` (optional) |
| Rain | (uses Open-Meteo, no key needed) |
| Weather | (uses Open-Meteo, no key needed) |
| Aurora | (uses NOAA SWPC, no key needed; optional: `AURORA_SIMPLE_KP_THRESHOLD`, `AURORA_ALERT_COOLDOWN_MINUTES`, `AURORA_STATE_FILE`) |
| Metals | `METALS_API_PROVIDER` (optional: `gold-api`, `metalapi`, `metals-api`), provider key if needed (`METALAPI_KEY` or `METALS_API_KEY`) |
| Transaction | `ETHERSCAN_API_KEY`, `TXBOT_CONFIG` (optional), `TXBOT_STATE` (optional), `TXBOT_API_URL` (optional) |
| Gnosis Multisig Tx | `GNOSISMULTISIGTXBOT_TARGETS_JSON` (optional, multi-target) or `GNOSISMULTISIGTXBOT_SAFE_ADDRESS` + `GNOSISMULTISIGTXBOT_CHAIN_ID`; `GNOSISMULTISIGTXBOT_STATE_FILE` (optional), `GNOSISMULTISIGTXBOT_SAFE_LABEL` (optional, single-target), `GNOSISMULTISIGTXBOT_API_BASE_URL` (optional, single-target), `GNOSISMULTISIGTXBOT_ALERT_ON_FIRST_RUN` (optional) |
| New Subdomain | `NEWSUBDOMAINBOT_DOMAIN` (comma-separated domains supported), `NEWSUBDOMAINBOT_STATE_FILE` (optional), `NEWSUBDOMAINBOT_SUBFINDER_BIN` (optional), `NEWSUBDOMAINBOT_SUBFINDER_TIMEOUT_SECONDS` (optional), `NEWSUBDOMAINBOT_ALERT_ON_FIRST_RUN` (optional); requires `subfinder` CLI |
| New Top AI Model | (no API key required; polls llm-stats.com leaderboard via zeroeval.com API) |
| RSS | `RSS_FEED_URL` (required; comma-separated for multiple feeds), `RSS_STATE_FILE` (optional), `RSS_MAX_ITEMS` (optional) |
| Geopolitical Shock | no API keys required; optional: `GEOSHOCK_STATE_FILE`, `GEOSHOCK_NEWS_LOOKBACK_MINUTES`, `GEOSHOCK_MAX_ITEMS`, `GEOSHOCK_MIN_CONFIRMATIONS`, `GEOSHOCK_MIN_HIGH_TRUST`, `GEOSHOCK_PERSISTENCE_RUNS`, `GEOSHOCK_COOLDOWN_MINUTES`, `GEOSHOCK_INFRA_DROP_THRESHOLD_PCT`, `GEOSHOCK_MARKET_JUMP_THRESHOLD_PCT`, `GEOSHOCK_VIX_LEVEL_THRESHOLD`, `GEOSHOCK_OVX_LEVEL_THRESHOLD` |
| Calendar | `CALENDARBOT_STATE` (optional) |

#### API Token Setup (How To Get Each Key)

Use this as a mapping from `.env.example` to provider dashboards and required scopes.

| Env Variable | Where to get it | Notes |
|-----|-------------------|-------|
| `TELEGRAM_BOT_TOKEN` | Create a bot with [@BotFather](https://t.me/BotFather) on Telegram | Run `/newbot`; BotFather returns the token |
| `TELEGRAM_CHAT_ID` | Send a message to your bot, then open `https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates` | Copy `chat.id` from the JSON response |
| `MATRIX_HOMESERVER_URL` | Your Matrix homeserver base URL | Example: `https://matrix-client.matrix.org` |
| `MATRIX_ACCESS_TOKEN` | From your Matrix client/session (or appservice/bot account tooling) | Used for Matrix Client-Server API requests |
| `MATRIX_ROOM_ID` | Copy the Matrix room ID from client/developer tools | Looks like `!abc123:matrix.org` |
| `FINNHUB_API_KEY` | Create account at [Finnhub](https://finnhub.io/) | Recommended key for the default stock provider (`STOCK_PRICE_PROVIDER=finnhub`) |
| `STOCK_PRICE_API_KEY` | Create account at [Twelve Data](https://twelvedata.com/) | Required when `STOCK_PRICE_PROVIDER=twelvedata`; also accepted as a fallback key for `finnhub` |
| `AQI_API_TOKEN` | Generate token in the [WAQI Data Platform](https://aqicn.org/data-platform/token/#/) | Required for AQI bot |
| `GH_TOKEN` | Generate a token at [GitHub token settings](https://github.com/settings/tokens) | Use **Personal access token (classic)** with `notifications` scope; add `repo` for private repo notification details |
| `ETHERSCAN_API_KEY` | Create account at [Etherscan](https://etherscan.io/apis) | Generate API key from account API settings |
| `METALAPI_KEY` (optional) | Create account at [MetalpriceAPI / metalapi.com](https://metalapi.com/) | Use when `METALS_API_PROVIDER=metalapi` |
| `METALS_API_KEY` (optional) | Create account at [metals-api.com](https://metals-api.com/) | Use when `METALS_API_PROVIDER=metals-api` |

Bots that do not need API keys:
- Rain and Weather (Open-Meteo)
- Aurora (NOAA SWPC)
- Geopolitical Shock (RSS + RIPE Stat + FRED public CSV)
- RSS and YouTube (feed/channel based)

#### Complete `.env.example` Variable Reference

All path values below are examples; you can use absolute paths or repo-relative paths.

Core:
- `TELEGRAM_BOT_TOKEN`: Telegram bot token used to send alerts and receive commands when `ALERTBOT_TRANSPORT=telegram`.
- `TELEGRAM_CHAT_ID`: Default Telegram chat ID for scheduled alerts when `ALERTBOT_TRANSPORT=telegram`.
- `MATRIX_HOMESERVER_URL`: Matrix homeserver base URL when `ALERTBOT_TRANSPORT=matrix`.
- `MATRIX_ACCESS_TOKEN`: Matrix access token for outbound alerts when `ALERTBOT_TRANSPORT=matrix`.
- `MATRIX_ROOM_ID`: Default Matrix room ID for scheduled alerts when `ALERTBOT_TRANSPORT=matrix`.

Stock and Stock Alert:
- `STOCK_PRICE_PROVIDER`: Stock data provider identifier (default/example: `finnhub`; also supports `twelvedata`).
- `FINNHUB_API_KEY`: Preferred API key for provider `finnhub`.
- `STOCK_PRICE_API_KEY`: API key for provider `twelvedata`; accepted as a fallback key for `finnhub`.
- `STOCK_TICKERS`: Required comma-separated tickers for `stockbot` (for example `AAPL,MSFT,GOOGL`).
- `STOCK_CURRENCY`: Output currency code for stock prices (default/example: `USD`).
- `STOCKALERT_CONFIG`: Path to stock alert rules config file.
- `STOCKALERT_STATE`: Path to stock alert state file.

AQI:
- `AQI_API_TOKEN`: AQI provider token (required for AQI bot).
- `AQI_PROVIDER`: AQI data provider name (default/example: `waqi`).
- `AQI_STATIONS`: Optional station selector override (comma-separated).
- `AQI_THRESHOLD`: AQI alert threshold value.
- `AQI_MAX_AGE_HOURS`: Maximum accepted station data age in hours.
- `AQI_STATE_FILE`: Path to AQI bot state file.

YouTube and RSS:
- `YT_CHANNEL_IDS`: Comma-separated YouTube channel IDs to monitor.
- `YT_STATE_FILE`: Path to YouTube bot state file.
- `RSS_FEED_URL`: Required. One feed URL or comma-separated feed URLs.
- `RSS_STATE_FILE`: Path to RSS bot state file.
- `RSS_MAX_ITEMS`: Maximum feed items processed per run.

Geopolitical Shock:
- `GEOSHOCK_STATE_FILE`: Path to geoshock state file.
- `GEOSHOCK_NEWS_LOOKBACK_MINUTES`: News lookback window used for confirmation checks.
- `GEOSHOCK_MAX_ITEMS`: Maximum entries parsed per feed per run.
- `GEOSHOCK_MIN_CONFIRMATIONS`: Minimum distinct source confirmations.
- `GEOSHOCK_MIN_HIGH_TRUST`: Minimum distinct high-trust source confirmations.
- `GEOSHOCK_PERSISTENCE_RUNS`: Consecutive trigger-ready runs required before alerting.
- `GEOSHOCK_COOLDOWN_MINUTES`: Cooldown before re-alerting on the same fingerprint.
- `GEOSHOCK_INFRA_DROP_THRESHOLD_PCT`: RIPE routing-visibility drop threshold for infrastructure corroboration.
- `GEOSHOCK_MARKET_JUMP_THRESHOLD_PCT`: Daily percent-jump threshold for VIX/OVX corroboration.
- `GEOSHOCK_VIX_LEVEL_THRESHOLD`: Absolute VIX level that can satisfy market corroboration.
- `GEOSHOCK_OVX_LEVEL_THRESHOLD`: Absolute OVX level that can satisfy market corroboration.

GitHub:
- `GH_TOKEN`: GitHub token for notifications API access.
- `GH_ALERT_STATE`: Path to GitHub bot state file.

Crypto:
- `CRYPTOBOT_CONFIG`: Path to crypto alert rules config file.
- `CRYPTOBOT_STATE`: Path to crypto bot state file.

Transactions:
- `ETHERSCAN_API_KEY`: Etherscan API key.
- `TXBOT_CONFIG`: Path to transaction bot config file.
- `TXBOT_STATE`: Path to transaction bot state file.
- `TXBOT_API_URL`: Etherscan-compatible API base URL.

Gnosis Multisig Tx:
- `GNOSISMULTISIGTXBOT_TARGETS_JSON`: Optional JSON array for monitoring multiple Safe addresses/chains in one bot run. Each item requires `chain_id` and `safe_address`; optional `safe_label` and `api_base_url`.
- `GNOSISMULTISIGTXBOT_SAFE_ADDRESS`: Safe address to monitor (use checksummed format expected by Safe Transaction Service).
- `GNOSISMULTISIGTXBOT_CHAIN_ID`: Chain ID to monitor for the Safe (required to disambiguate same address on multiple chains).
- `GNOSISMULTISIGTXBOT_SAFE_LABEL`: Optional label included in alert messages (single-target mode, or use per-target `safe_label` in `GNOSISMULTISIGTXBOT_TARGETS_JSON`).
- `GNOSISMULTISIGTXBOT_STATE_FILE`: Path to bot state file (stores the current pending queue snapshot).
- `GNOSISMULTISIGTXBOT_API_BASE_URL`: Optional Safe Transaction Service base URL override for unsupported/custom chains (single-target mode, or use per-target `api_base_url` in `GNOSISMULTISIGTXBOT_TARGETS_JSON`).
- `GNOSISMULTISIGTXBOT_ALERT_ON_FIRST_RUN`: If `true`, send alerts for currently queued txs on the first run; default/example is `false`.

New Subdomain:
- `NEWSUBDOMAINBOT_DOMAIN`: Domain(s) to monitor, comma-separated (for example `example.com,example.org`).
- `NEWSUBDOMAINBOT_STATE_FILE`: Path to bot state file (stores known subdomains and the latest DNS snapshot).
- `NEWSUBDOMAINBOT_SUBFINDER_BIN`: `subfinder` binary name/path (default `subfinder`).
- `NEWSUBDOMAINBOT_SUBFINDER_TIMEOUT_SECONDS`: Timeout for running the `subfinder` command.
- `NEWSUBDOMAINBOT_ALERT_ON_FIRST_RUN`: If `true`, alert on already-known subdomains during the first successful run; default/example is `false`.

Rain:
- `RAIN_THRESHOLD`: Rain probability threshold percent for alerts.
- `RAIN_SLEEP_START`: Quiet-hours start time (`HH:MM`) in local timezone.
- `RAIN_SLEEP_END`: Quiet-hours end time (`HH:MM`) in local timezone.

Calendar:
- `CALENDARBOT_STATE`: Path to calendar bot state file.
  Event storage is `configs/calendarbot.ics` (no env var required by default).

Metals:
- `METALAPI_KEY`: API key for provider `metalapi`.
- `METALS_API_KEY`: API key for provider `metals-api` (commented in sample file).
- `METALS_CURRENCY`: Quote currency for metals prices (default/example: `USD`).
- `METALS_API_PROVIDER`: Provider selector (`gold-api`, `metalapi`, `metals-api`).

Aurora:
- `AURORA_SIMPLE_KP_THRESHOLD`: Kp threshold that triggers aurora alerts.
- `AURORA_ALERT_COOLDOWN_MINUTES`: Minimum minutes between repeated aurora alerts.
- `AURORA_STATE_FILE`: Path to aurora bot state file.

General:
- `ALERTBOT_TRANSPORT`: Alert transport backend (`telegram` or `matrix`, default: `telegram`).
- `LOG_LEVEL`: Application log verbosity (for example `INFO`, `DEBUG`).
- `XDG_CONFIG_HOME`: Optional XDG config base directory override.
- `XDG_STATE_HOME`: Optional XDG state base directory override.

Transport notes:
- Matrix support is currently send-only (scheduled alerts + startup message).
- Controller command polling/manual commands remain Telegram-only.

### Runtime Paths

AlertBot resolves config/state/log paths differently depending on how you run it:

- **Source checkout mode** (repo root detected): uses repo-local paths (`configs/`, `state/`, `logs/`).
- **Installed package mode** (no repo root detected): uses XDG user paths:
  - Config: `$XDG_CONFIG_HOME/alertbot` (defaults to `~/.config/alertbot`)
  - State: `$XDG_STATE_HOME/alertbot` (defaults to `~/.local/state/alertbot`)
  - Logs: `$XDG_STATE_HOME/alertbot/logs` (or `~/.local/state/alertbot/logs`)

### Schedule Configuration (`configs/schedule.yaml` + optional local overlay)

`configs/schedule.example.yaml` is the tracked template.
Copy it to local `configs/schedule.yaml` for your actual recurring schedule.
`configs/privateschedule.yaml` (optional, git-ignored) is a local overlay merged on top.

How to use it:
- `bots.<name>.enabled`: enable/disable a bot.
- `bots.<name>.interval_minutes`: run frequency for scheduled checks.
- `bots.<name>.manual_only` (optional): enable Telegram/manual runs without creating a scheduled job.
- `bots.<name>.config_file` (optional): path to bot-specific config when required.
- `telegram.drop_pending_updates`: whether Telegram backlog is dropped on startup.
- `controller.config_reload_minutes`: how often to reload this file at runtime (`0` = restart required to apply changes).

Merge behavior (lowest → highest precedence):

1. `configs/schedule.yaml` — your base schedule (public core bots)
2. Plugin collection defaults — schedules shipped by any installed `alertbot-bots-*` collection packages
3. `configs/privateschedule.yaml` — your local overlay (always wins; git-ignored)

Any key present in a higher layer overrides the same key from lower layers. Plugin defaults only kick in for bots not already configured in `configs/schedule.yaml`.

For private/local-only bot module setup (legacy approach) see `CONTRIBUTING.md` (drop a bot module in `src/alertbot/bots/private/`; it is auto-discovered via the schedule name fallback).
For the recommended installable-package approach see [Plugin Collections](#plugin-collections) below.

Minimal example:

```yaml
bots:
  stock:
    enabled: true
    interval_minutes: 360

  stockalert:
    enabled: true
    interval_minutes: 180
    config_file: configs/stockalert.config

  gh:
    enabled: true
    interval_minutes: 60

  metals:
    enabled: false
    interval_minutes: 360

  tx:
    enabled: true
    interval_minutes: 5
    config_file: configs/private/txbot.config.json

telegram:
  drop_pending_updates: true

controller:
  config_reload_minutes: 0
```

**Note:** Bots not listed in the merged schedule are disabled. Manual commands for disabled bots return an error.
For current shared defaults, see `configs/schedule.example.yaml` in this repo.

## Plugin Collections

AlertBot supports installable bot collections — pip packages that bundle location-specific or domain-specific bots and their schedule defaults.  Once installed, the controller discovers them automatically at startup and on every config reload.  No edits to `controller.py` or local schedule files are required.

### Installing a collection

```bash
pip install git+https://github.com/someone/alertbot-bots-cityname-author
```

The controller picks up the new bots on the next startup (or config reload if `controller.config_reload_minutes` is set).  You can still override any plugin-default schedule entry locally:

```yaml
# configs/privateschedule.yaml
bots:
  somebot:
    enabled: false          # disable a plugin bot
  otherbot:
    interval_minutes: 120   # change its run interval
```

### Authoring a collection

A collection is a standard Python package that declares entry points in two groups:

```toml
# pyproject.toml of your collection
[project.entry-points."alertbot.bots"]
mybotid = "mypackage.mybotmodule"

[project.entry-points."alertbot.schedules"]
mycollection = "mypackage:get_schedule"
```

**Bot module contract (`alertbot.bots` entry points)**

Each bot module must define:

| Attribute | Type | Description |
|-----------|------|-------------|
| `BOT_ID` | `str` | Stable identity used in scheduling/state. Must equal the entry point name. |
| `BOT_COMMAND` | `str` | Telegram command name (may differ from `BOT_ID`). |
| `run(...)` | callable | Same `run(manual_trigger, chat_id, schedule_context)` interface as core bots. |

**Schedule entry point contract (`alertbot.schedules`)**

The callable must return:

```python
{
    "api_version": "v1",
    "bots": {
        "mybotid": {"enabled": True, "interval_minutes": 60},
    }
}
```

Allowed top-level keys: `api_version`, `bots`. Unknown keys cause the payload to be rejected.

**Conflict rules (enforced by `PluginRegistry`):**

- Core bot IDs (`BOT_MODULES` keys) and their command names are reserved and cannot be claimed by a plugin.
- When two plugins declare the same `BOT_ID` or `BOT_COMMAND`, the one from the alphabetically-first distribution name wins; the other is skipped and logged.
- Entry point name must match `BOT_ID` exactly.

Install and test locally:

```bash
pip install -e /path/to/your-collection
alertbot  # bots appear in /help and /schedule automatically
```

## Location Configuration

Location is managed centrally via `state/location_state.json`. All location-aware bots (rain, weather, AQI) use this shared configuration.

### Default Location
The default location is **London, United Kingdom**.

### Changing Location

#### Via Telegram Command
Send `/location <city_name>` to the bot:
```
/location           # Show current location
/location London    # Change to London
/location "London, United Kingdom" # Cities with spaces/commas need quotes
```

#### Via State File
Edit `state/location_state.json` directly:
```json
{
  "city": "london",
  "display_name": "London, United Kingdom",
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London",
  "country_code": "GB"
}
```

### Location-Aware Bots
- **rainbot** — Uses latitude/longitude for forecast, timezone for sleep hours
- **weatherbot** — Sends a daily forecast summary for today + tomorrow
- **airqualitybot** — Uses location city for AQI lookup; optional station list uses city-name matching with nearby-coordinate fallback for station selection; WAQI freshness checks use timezone-aware station timestamps

## Telegram Commands

Once the controller is running, you can trigger any bot manually via Telegram:

| Command | Description |
|---------|-------------|
| `/stock` or `/stocks` | Get current stock prices |
| `/stockalert` | Check stock threshold alerts (`watch`/`action` rules) |
| `/crypto` | Check crypto prices against rules |
| `/airquality` | Get current air quality |
| `/gh` | Check GitHub notifications |
| `/calendar` | Show upcoming calendar events |
| `/rss` | Check RSS feed for new items |
| `/yt` or `/youtube` | Check YouTube channels |
| `/rain` | Get rain forecast |
| `/weather` | Get weather forecast |
| `/sunrise` | Get today’s sunrise/sunset times |
| `/aurora` | Check strong aurora conditions (Kp-based) |
| `/geoshock` | Evaluate extreme geopolitical shock conditions |
| `/metals` | Check precious metals prices |
| `/tx` | Check blockchain transactions |
| `/newtopaimodelbot` | Check LLM Stats top-10 for new AI model entrants |
| `/newsubdomainbot` | Check for new subdomains via subfinder |
| `/all` | Run all enabled bots now |
| `/location` | Show or change location |
| `/addevent` | Add a calendar event to `configs/calendarbot.ics` |
| `/listevents` | List calendar events from `configs/calendarbot.ics` |
| `/deleteevent` | Delete a calendar event by UID/name |
| `/schedule` | List active bots and their run intervals |
| `/help` | List available commands |

## Bot-Specific Configuration

### Crypto Bot (`configs/cryptobot.config`)

```json
{
  "currency": "usd",
  "rules": [
    {"id": "bitcoin", "direction": "above", "price": 100000},
    {"id": "bitcoin", "direction": "below", "price": 90000},
    {"id": "ethereum", "direction": "above", "price": 4000}
  ]
}
```

- `id`: Token ID resolved via DefiLlama's `coingecko:<id>` namespace (for example `bitcoin`, `ethereum`)
- `direction`: `"above"` or `"below"`
- `price`: Threshold price
- `currency`: Currently `usd` only when using DefiLlama price data
- Manual `/crypto` returns a deduplicated list of current prices for all configured token IDs, including signed 24h change percent (no timestamps and no "crossed above/below" lines).

### Stock Alert Bot (`configs/stockalert.config`)

```json
{
  "currency": "USD",
  "watch": [
    {"ticker": "AAPL", "direction": "above", "price": 230},
    {"ticker": "MSFT", "direction": "below", "price": 390}
  ],
  "action": [
    {"ticker": "TSLA", "direction": "below", "price": 180}
  ]
}
```

- `watch` and `action` are separate rule lists; each rule requires:
- `ticker`: Stock ticker symbol
- `direction`: `"above"` or `"below"`
- `price`: Threshold price
- Scheduled runs alert only on threshold crossing (compared to previous run), and alert text is labeled as `watch alert` or `action alert`.
- Manual `/stockalert` returns current prices for configured tickers without sending crossing alerts.

### Calendar Bot (`configs/calendarbot.ics`)

Calendar events are stored in an iCalendar file: `configs/calendarbot.ics`.
The file is created automatically (empty VCALENDAR) when calendar commands/bot access it and it does not exist.

- `calendarbot` reads `VEVENT` items from the `.ics` file.
- Default timezone comes from shared location state (`state/location_state.json`) managed by `/location`.
- Events can still override timezone with an ICS `DTSTART;TZID=...` (or UTC `Z`) timestamp.
- Reminder offsets are stored in `X-ALERTBOT-REMINDER-MINUTES` and also written as a simple `VALARM`.

Example ICS content (for reference):

```ics
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//alertbot//calendarbot//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:birthday
DTSTART:20260315T090000
RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=15
SUMMARY:Mom's Birthday
DESCRIPTION:Don't forget to call!
X-ALERTBOT-REMINDER-MINUTES:60
END:VEVENT
BEGIN:VEVENT
UID:cantina
DTSTART:20260311T170000Z
RRULE:FREQ=WEEKLY;BYDAY=WE
SUMMARY:Cantina
X-ALERTBOT-REMINDER-MINUTES:0
END:VEVENT
END:VCALENDAR
```

Supported recurrence mappings in `.ics`:
- One-time events: `DTSTART` with no `RRULE`
- Daily: `RRULE:FREQ=DAILY`
- Weekly: `RRULE:FREQ=WEEKLY;BYDAY=<MO..SU>`
- Monthly: `RRULE:FREQ=MONTHLY;BYMONTHDAY=<day>`
- Yearly: `RRULE:FREQ=YEARLY;BYMONTH=<month>;BYMONTHDAY=<day>`

Telegram calendar management commands:
- `/addevent YYYY-MM-DD HH:MM Event name`
- Optional `|` segments: `recurrence=...`, `reminder=<minutes>`, `message=...`, `tz=...`
- `/listevents [all] [limit]`
- `/deleteevent <event_uid_or_name>`

Legacy migration helper:
- `PYTHONPATH=src python3 scripts/migrate_calendar_json_to_ics.py <old_json_path> --out configs/calendarbot.ics --replace`

### Transaction Bot (`TXBOT_CONFIG`, example: `configs/txbot.config.example.json`)

Use a local config file for your watchlist (recommended path: `configs/private/txbot.config.json`).
The repo includes a sanitized example config at `configs/txbot.config.example.json`.

```json
{
  "poll_window_blocks": 200,
  "min_native_value": 5,
  "ignore_zero_value_contract_calls": true,
  "chains": [
    {"name": "Ethereum", "chain_id": 1},
    {"name": "Arbitrum", "chain_id": 42161}
  ],
  "watch_addresses": [
    "0x..."
  ]
}
```

**Spam filtering** is enabled by default to reduce noise from dust transfers, token approvals, DEX swaps, and other low-value contract interactions:

| Option | Default | Description |
|--------|---------|-------------|
| `min_native_value` | `5` | Ignore transactions with native value (ETH/MATIC) below this amount. Set to `0` to disable. |
| `ignore_zero_value_contract_calls` | `true` | Ignore transactions that transfer zero native value and call a contract (e.g. ERC-20 transfers, DEX swaps, bridge calls). Set to `false` to disable. |

### RSS Bot Notes

- RSS entries are marked as seen only after a successful alert send, so transient Telegram failures are retried on later runs.
- RSS has no built-in default feed URL; set `RSS_FEED_URL` in `.env` or `.env.private`.
- `RSS_FEED_URL` accepts one URL or a comma-separated list of multiple feed URLs.

### Stock Bot Notes

- Scheduled stock price updates are sent only during NYSE regular hours (Mon-Fri 09:30-16:00 America/New_York).
- Manual `/stock` and `/stocks` commands can run outside market hours and include a note when NYSE is closed.
- Supports `finnhub` (default) and `twelvedata` via `STOCK_PRICE_PROVIDER`.
- When the active provider rate limits quote requests, the message reports a single consolidated "rate limit reached" line instead of per-ticker `ERROR (fetch failed)` noise.

### Stock Alert Bot Notes

- Uses the same stock provider/key settings as `stockbot` (`STOCK_PRICE_PROVIDER`, `FINNHUB_API_KEY`, `STOCK_PRICE_API_KEY`).
- Runs independently from `stockbot`; configure and schedule it via `configs/stockalert.config` + `configs/schedule.yaml`.
- Scheduled stock alert checks run only during NYSE regular hours (Mon-Fri 09:30-16:00 America/New_York).
- Manual `/stockalert` can run outside market hours.
- When the active provider rate limits quote requests, affected tickers are summarized in a single "rate limit reached" line instead of repeated per-ticker fetch errors.

### Rain Bot Notes

- Default rain alert threshold is `45%`.
- Override with `RAIN_THRESHOLD` if you want a stricter/looser trigger.
- Alerts now include a `Likely start` line using local time plus ETA.
- `Likely start` is defined as the first hourly forecast block where precipitation probability is greater than `RAIN_THRESHOLD`.

### Sunrise Bot Notes

- `sunrisebot` uses the shared location from `state/location_state.json` (same source as weather/rain/AQI).
- It is configured as `manual_only` in `configs/schedule.yaml`, so it does not create scheduled alerts.
- Use `/sunrise` to get today’s sunrise/sunset times for the current location.

### Aurora Bot Notes

- Uses NOAA SWPC planetary K-index data (Option A) to alert when strong activity is detected.
- Default strong threshold is `Kp >= 7`.
- Scheduled alerts use cooldown dedupe (default `360` minutes), unless activity increases significantly.
- Optional env vars: `AURORA_SIMPLE_KP_THRESHOLD`, `AURORA_ALERT_COOLDOWN_MINUTES`, `AURORA_STATE_FILE`.

### Geopolitical Shock Bot Notes

- Uses a curated mix of Middle East and Western RSS feeds with strict language filters (multi-actor + high-severity military terms).
- Requires cross-source and cross-region confirmation before considering an alert.
- Corroborates with infrastructure disruption (RIPE routing visibility drops) and market stress (VIX/OVX daily movement/levels).
- Uses persistence gating (default `2` consecutive trigger-ready runs) and cooldown dedupe (default `360` minutes).
- Optional env vars are listed under the Geopolitical Shock section above.

### New Top AI Model Bot Notes

- Monitors the [llm-stats.com](https://llm-stats.com/) chat-arena leaderboard top-10 via the zeroeval.com API.
- Alerts when a model ID that was not in the previous top-10 snapshot appears in the current top-10.
- No API key required.
- On the first run, the snapshot is initialized without sending an alert.
- Default check interval is `1440` minutes (once per day) in `configs/schedule.example.yaml`.

## Running Individual Bots (Standalone Mode)

Bots can still be run individually for testing or one-off checks:

```bash
python -m alertbot.bots.stockbot
python -m alertbot.bots.stockalertbot
python -m alertbot.bots.cryptobot
python -m alertbot.bots.airqualitybot
python -m alertbot.bots.aurorabot
python -m alertbot.bots.geoshockbot
python -m alertbot.bots.metalsbot
python -m alertbot.bots.ghbot
python -m alertbot.bots.rssbot
python -m alertbot.bots.ytbot
python -m alertbot.bots.rainbot
python -m alertbot.bots.weatherbot
python -m alertbot.bots.sunrisebot
python -m alertbot.bots.txbot
python -m alertbot.bots.gnosismultisigtxbot
python -m alertbot.bots.newsubdomainbot
python -m alertbot.bots.newtopaimodelbot
python -m alertbot.bots.calendarbot
```

When run standalone, bots execute one run; recurring schedules come from the merged controller schedule (base schedule plus optional local overlay).

## Running Tests

This repo uses a `src/` layout, so when running tests directly from the repository root you should set `PYTHONPATH=src`.

Run all tests (built-in `unittest`):

```bash
PYTHONPATH=src python3 -m unittest -q
```

Run a specific bot test file:

```bash
PYTHONPATH=src python3 -m unittest -q tests.test_rssbot
PYTHONPATH=src python3 -m unittest -q tests.test_newtopaimodelbot
PYTHONPATH=src python3 -m unittest -q tests.test_geoshockbot
```

If you prefer `pytest` and have it installed locally, this also works:

```bash
PYTHONPATH=src python3 -m pytest -q tests/
```

## State Files

The controller and bots maintain state to avoid duplicate alerts:

- Source checkout mode:
  - `state/controller_state.json` — Last run times for each bot
  - `state/*.json` — Bot-specific state (seen IDs, last alerts, etc.)
- Installed package mode:
  - `~/.local/state/alertbot/controller_state.json` (or `$XDG_STATE_HOME/alertbot/controller_state.json`)
  - `~/.local/state/alertbot/*.json` (or `$XDG_STATE_HOME/alertbot/*.json`)

## Logs

Set log level via environment:
```bash
LOG_LEVEL=DEBUG alertbot
```

## Notes

- All scheduling is managed by the controller schedule files (base schedule plus optional local overlay) — no need to configure intervals in individual bot configs or environment variables.
- The controller uses APScheduler for reliable timing.
- State files are written atomically to prevent corruption.
- All timestamps are stored in UTC ISO-8601 format.
