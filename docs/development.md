# Development Guide

## Prerequisites

- Python 3.11+
- A Discord bot application with a valid token ([Discord Developer Portal](https://discord.com/developers/applications))
- Required bot intents: **Server Members**, **Message Content**

## Local Setup

```bash
# 1. Clone
git clone git@github.com:sondt99/ctf-bot.git
cd ctf-bot

# 2. Create virtualenv
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install runtime + dev dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — at minimum set DISCORD_TOKEN and DISCORD_GUILD_ID
```

## Environment Variables

See [`.env.example`](../.env.example) for all variables. The most important ones for development:

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Required — your bot token |
| `DISCORD_GUILD_ID` | Set this for instant command sync during development (vs. up to 1h global sync) |
| `DATABASE_PATH` | Defaults to `ctf_bot.db` in the project root |
| `TIMEZONE` | IANA name (`Asia/Ho_Chi_Minh`) or offset (`UTC+7`) |

## Running the Bot

```bash
python -m bot.main
```

## Testing

Tests use `pytest` with `pytest-asyncio` in auto mode.

```bash
# Run all tests
pytest

# Verbose output
pytest -v

# Run a specific test file
pytest tests/test_embeds.py -v

# Run a specific test
pytest tests/test_ctftime_service.py::test_fetch_archived_events_filters_running -v
```

The test suite does **not** require a running Discord server or real API credentials — all external calls are mocked.

## Type Checking and Linting

```bash
python -m pyright
python -m ruff check .
python -m compileall bot tests scoreboard
```

The project targets Pyright's default (basic) mode. All Pyright errors and Ruff findings must be clean before a PR is merged. Compile checks should pass for all runtime modules, tests, and scoreboard helper scripts.

## Project Structure

```
ctf-bot/
├── bot/
│   ├── cogs/          # discord.py Cog classes (one per feature area)
│   ├── db/            # database.py (DDL + migrations) + repository.py (ORM)
│   ├── services/      # external API calls and Discord guild setup
│   ├── utils/         # embed builders, timezone parsing
│   ├── views/         # discord.ui.View subclasses (paginator)
│   ├── config.py      # env-var access
│   ├── crypto.py      # Fernet token encryption
│   └── main.py        # entry point
├── tests/             # pytest test suite
├── docs/              # this directory
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
└── docker-compose.yml
```

## Adding a New Command

1. Identify the correct Cog in `bot/cogs/` (or create one).
2. Add an `@app_commands.command` method.
3. If it needs database access, add a method to `Repository` in `bot/db/repository.py` and update the schema in `bot/db/database.py` if needed.
4. Write tests in `tests/` covering the new logic.
5. Run `pytest`, `pyright`, `ruff`, and `compileall` — all must be clean.

## Adding a New Service

Create a new module in `bot/services/`. Keep HTTP calls in `services/`, Discord API calls in `cogs/`, and database calls in `db/repository.py`. Services should not import from `cogs/`.

## Commit Convention

```
<type>: <short description> (#issue)

feat:    new feature
fix:     bug fix
refactor: code change without feature/fix
test:    test-only changes
docs:    documentation only
chore:   tooling, deps, CI
```
