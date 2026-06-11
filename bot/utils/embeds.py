from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import discord

from bot.config import TIMEZONE

_log = logging.getLogger(__name__)


def _parse_timezone_offset(value: str) -> timezone:
    match = re.fullmatch(r"UTC([+-])(\d{1,2})", value.strip())
    if not match:
        _log.warning(
            "Unrecognized TIMEZONE value %r — falling back to UTC. "
            "Use format UTC+N or UTC-N (e.g., UTC+7).",
            value,
        )
        return timezone.utc
    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    return timezone(sign * timedelta(hours=hours))


def _format_time_range(event: dict) -> str:
    start = event.get("start")
    finish = event.get("finish")
    if not start or not finish:
        return "N/A"

    tz = _parse_timezone_offset(TIMEZONE or "UTC+0")
    start_dt = datetime.fromisoformat(start).astimezone(tz)
    finish_dt = datetime.fromisoformat(finish).astimezone(tz)
    return f"{start_dt:%Y-%m-%d %H:%M} → {finish_dt:%Y-%m-%d %H:%M}"



def build_simple_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=title, description=description, color=discord.Color.gold()
    )


def build_scoreboard_embed(
    entries: list[dict],
    changes: list[str],
    source_url: str,
    top_n: int = 10,
) -> discord.Embed:
    embed = discord.Embed(title="Scoreboard Update", color=discord.Color.gold())
    embed.add_field(name="Source", value=source_url, inline=False)

    if entries:
        if len(entries) == 1:
            entry = entries[0]
            embed.add_field(
                name="Team",
                value=f"{entry['name']} — {entry['score']} (pos {entry['pos']})",
                inline=False,
            )
        else:
            lines = []
            for entry in entries[:top_n]:
                lines.append(f"{entry['pos']}. {entry['name']} — {entry['score']}")
            embed.add_field(name="Scores", value="\n".join(lines), inline=False)

    if changes:
        embed.add_field(name="Changes", value="\n".join(changes[:5]), inline=False)

    return embed


def _format_event_block(event: dict) -> str:
    weight_value = event.get("weight")
    if weight_value is None:
        weight_text = "N/A"
    elif isinstance(weight_value, (int, float)):
        weight_text = f"{weight_value:.2f}"
    else:
        weight_text = str(weight_value)
    title = event.get("title") or "CTF Event"
    lines = [
        f"Format: {event.get('format') or 'N/A'} | Rating Weight: {weight_text}",
        f"Time: {_format_time_range(event)}",
        f"CTFtime: {event.get('ctftime_url') or 'N/A'}",
        f"URL: {event.get('url') or 'N/A'}",
        "---",
    ]
    return "\n".join(lines)


def build_events_page_embed(
    events: list[dict], page: int, page_size: int
) -> discord.Embed:
    total_pages = max(1, (len(events) + page_size - 1) // page_size)
    start_index = page * page_size
    slice_events = events[start_index : start_index + page_size]

    embed = discord.Embed(title="Upcoming CTFs", color=discord.Color.gold())
    for event in slice_events:
        embed.add_field(
            name=event.get("title") or "CTF Event",
            value=_format_event_block(event),
            inline=False,
        )

    embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed
