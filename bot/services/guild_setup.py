from __future__ import annotations

import re
from typing import Mapping

import discord


CHANNELS = [
    "Account",
    "General",
    "REV",
    "PWN",
    "WEB",
    "CRYPTO",
    "FOR",
    "MISC",
    "Scoreboard",
]

BOT_CATEGORY_NAME = "BOT"
BOT_LOG_CHANNEL = "log"
BOT_BACKUP_CHANNEL = "backup"

_Overwrites = Mapping[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite]


def _sanitize_category_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > 100:
        return name[:97] + "..."
    return name


async def create_ctf_category_and_channels(
    guild: discord.Guild, event_title: str
) -> tuple[discord.CategoryChannel, dict[str, int]]:
    category_name = _sanitize_category_name(event_title)
    category = await guild.create_category(name=category_name)

    channels: dict[str, int] = {}
    for channel_name in CHANNELS:
        if channel_name == "Account":
            ow: _Overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=False
                )
            }
        else:
            ow = {}

        channel = await category.create_text_channel(
            name=channel_name.lower(),
            overwrites=ow,
        )
        channels[channel_name] = channel.id

    return category, channels


async def hide_ctf_category_and_channels(
    guild: discord.Guild, category_id: int
) -> None:
    category = guild.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        raise ValueError("Category not found.")

    overwrites: _Overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False)
    }
    await category.edit(overwrites=overwrites)

    for channel in category.channels:
        await channel.edit(overwrites=overwrites)


async def delete_ctf_category_and_channels(
    guild: discord.Guild, category_id: int
) -> None:
    category = guild.get_channel(category_id)
    if not isinstance(category, discord.CategoryChannel):
        raise ValueError("Category not found.")

    for channel in list(category.channels):
        await channel.delete()
    await category.delete()


async def ensure_bot_admin_category(
    guild: discord.Guild,
) -> tuple[discord.CategoryChannel, dict[str, discord.TextChannel]]:
    ow: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if guild.me is not None:
        ow[guild.me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        )

    category = discord.utils.get(guild.categories, name=BOT_CATEGORY_NAME)
    if category is None:
        category = await guild.create_category(name=BOT_CATEGORY_NAME, overwrites=ow)

    log_channel = discord.utils.get(category.text_channels, name=BOT_LOG_CHANNEL)
    if log_channel is None:
        log_channel = await category.create_text_channel(name=BOT_LOG_CHANNEL)

    backup_channel = discord.utils.get(category.text_channels, name=BOT_BACKUP_CHANNEL)
    if backup_channel is None:
        backup_channel = await category.create_text_channel(name=BOT_BACKUP_CHANNEL)

    return category, {"log": log_channel, "backup": backup_channel}


CTF_ROLE_NAME = "ctf"


async def ensure_ctf_role(guild: discord.Guild) -> discord.Role:
    """Return the @ctf role, creating it if it doesn't exist."""
    role = discord.utils.get(guild.roles, name=CTF_ROLE_NAME)
    if role is None:
        role = await guild.create_role(
            name=CTF_ROLE_NAME,
            reason="Auto-created by ctf-bot for /done access",
        )
    return role
