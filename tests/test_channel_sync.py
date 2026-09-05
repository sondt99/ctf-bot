"""One category = one channel.

/challenge-fetch reconciles the event's topic channels against the platform's
real categories instead of forcing them into a fixed REV/PWN/WEB/... set.
"""

from types import SimpleNamespace

import pytest

from bot.cogs.challenge import _drop_channel_keys
from bot.services.guild_setup import BASE_CHANNELS, plan_channel_sync
from bot.views.challenge_views import channel_name_for_category


def _category(*names: str):
    return SimpleNamespace(
        text_channels=[
            SimpleNamespace(name=name, position=i) for i, name in enumerate(names)
        ]
    )


@pytest.mark.parametrize(
    "category,expected",
    [
        ("blockchain", "blockchain"),
        ("boot2root", "boot2root"),
        ("Web Exploitation", "web-exploitation"),
        ("Reverse-Engineering", "reverse-engineering"),
        ("MISC / Trivia", "misc-trivia"),
        ("Tiếng Việt", "tieng-viet"),
        ("🔥 pwn", "pwn"),
        ("", "uncategorized"),
        (None, "uncategorized"),
    ],
)
def test_channel_name_for_category(category, expected):
    assert channel_name_for_category(category) == expected


def test_channel_name_stays_within_discord_limit():
    assert len(channel_name_for_category("a" * 150)) == 100


def test_plan_creates_missing_and_deletes_unused():
    category = _category("account", "general", "scoreboard", "rev", "pwn", "web")
    to_create, to_delete = plan_channel_sync(category, ["web", "rev", "blockchain"])

    assert to_create == ["blockchain"]
    assert [c.name for c in to_delete] == ["pwn"]


def test_plan_never_touches_base_channels():
    category = _category(*(name.lower() for name in BASE_CHANNELS))
    to_create, to_delete = plan_channel_sync(category, ["crypto"])

    assert to_create == ["crypto"]
    assert to_delete == []


def test_plan_is_case_insensitive_against_existing_channels():
    category = _category("REV", "Crypto")
    to_create, to_delete = plan_channel_sync(category, ["rev", "crypto"])

    assert to_create == []
    assert to_delete == []


def test_plan_deletion_order_follows_channel_position():
    category = _category("zeta", "alpha", "mid")
    _, to_delete = plan_channel_sync(category, [])

    assert [c.name for c in to_delete] == ["zeta", "alpha", "mid"]


def test_plan_with_nothing_to_do():
    category = _category("general", "web")
    assert plan_channel_sync(category, ["web"]) == ([], [])


def test_drop_channel_keys_matches_stored_casing():
    channels = {"Account": 1, "General": 2, "REV": 3, "PWN": 4}
    assert _drop_channel_keys(channels, ["pwn", "rev"]) == {"Account": 1, "General": 2}


def test_drop_channel_keys_leaves_untouched_map_alone():
    channels = {"Account": 1, "WEB": 2}
    assert _drop_channel_keys(channels, []) == channels
