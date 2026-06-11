import logging
import os

from dotenv import load_dotenv


load_dotenv()

_log = logging.getLogger(__name__)


def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value


DISCORD_TOKEN = _get_env("DISCORD_TOKEN")
DISCORD_GUILD_ID = _get_env("DISCORD_GUILD_ID")
DATABASE_PATH = _get_env("DATABASE_PATH", "ctf_bot.db")
SCOREBOARD_POLL_SECONDS = int(_get_env("SCOREBOARD_POLL_SECONDS") or "30")
SCOREBOARD_TOP_N = int(_get_env("SCOREBOARD_TOP_N") or "10")
TIMEZONE = _get_env("TIMEZONE", "UTC+7")
CTF_REMOVE_PASSWORD = _get_env("CTF_REMOVE_PASSWORD")
SCOREBOARD_TEAM_NAME = _get_env("SCOREBOARD_TEAM_NAME")
FERNET_KEY = _get_env("FERNET_KEY")

if FERNET_KEY is None:
    _log.warning(
        "FERNET_KEY not set — auth tokens will be stored in plaintext. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\""
    )

