from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from bot.config import TIMEZONE

_log = logging.getLogger(__name__)


def _parse_timezone_offset(value: str) -> tzinfo:
    v = value.strip()
    # Try IANA name first (e.g. Asia/Ho_Chi_Minh, Europe/Berlin)
    try:
        return ZoneInfo(v)
    except (ZoneInfoNotFoundError, KeyError):
        pass
    # Fall back to UTC+N / UTC-N fixed-offset format
    match = re.fullmatch(r"UTC([+-])(\d{1,2})", v)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        return timezone(sign * timedelta(hours=hours))
    _log.warning(
        "Unrecognized TIMEZONE value %r — falling back to UTC. "
        "Accepted formats: IANA name (Asia/Ho_Chi_Minh) or UTC+N / UTC-N.",
        value,
    )
    return timezone.utc


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


def _rank_medal(pos: int) -> str:
    return {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(pos, "")


def _rank_arrow(name: str, prev_ranks: dict[str, int], current_pos: int) -> str:
    prev = prev_ranks.get(name)
    if prev is None or prev == current_pos:
        return ""
    if prev > current_pos:
        return f" ▲{prev - current_pos}"
    return f" ▼{current_pos - prev}"


def build_scoreboard_embed(
    entries: list[dict],
    changes: list[str],
    source_url: str,
    top_n: int = 10,
    tracked_team: str | None = None,
    prev_ranks: dict[str, int] | None = None,
    event_title: str | None = None,
) -> discord.Embed:
    title = f"Scoreboard — {event_title}" if event_title else "Scoreboard Update"
    embed = discord.Embed(title=title, color=discord.Color.gold())

    _prev = prev_ranks or {}
    team_lower = (tracked_team or "").lower()

    if entries:
        lines: list[str] = []
        for entry in entries[:top_n]:
            pos = entry["pos"]
            name = entry["name"]
            score = entry["score"]
            medal = _rank_medal(pos)
            arrow = _rank_arrow(name, _prev, pos)

            is_tracked = team_lower and name.lower() == team_lower
            if is_tracked:
                line = f"**`{pos:>2}.` {medal} {name} — {score}{arrow}** ⭐"
            else:
                line = f"`{pos:>2}.` {medal} {name} — {score}{arrow}"
            lines.append(line)

        embed.description = "\n".join(lines)

    tracked_entry = None
    if team_lower:
        tracked_entry = next(
            (e for e in entries if e["name"].lower() == team_lower), None,
        )
    if tracked_entry:
        arrow = _rank_arrow(tracked_entry["name"], _prev, tracked_entry["pos"])
        embed.add_field(
            name="Your team",
            value=(
                f"**{tracked_entry['name']}** — "
                f"Rank `#{tracked_entry['pos']}`{arrow} | "
                f"Score `{tracked_entry['score']}`"
            ),
            inline=False,
        )

    if changes:
        embed.add_field(
            name="Rank changes",
            value="\n".join(changes[:8]),
            inline=False,
        )

    embed.set_footer(text=source_url)
    return embed


def _format_event_block(event: dict) -> str:
    weight_value = event.get("weight")
    if weight_value is None:
        weight_text = "N/A"
    elif isinstance(weight_value, (int, float)):
        weight_text = f"{weight_value:.2f}"
    else:
        weight_text = str(weight_value)
    lines = [
        f"Format: {event.get('format') or 'N/A'} | Rating Weight: {weight_text}",
        f"Time: {_format_time_range(event)}",
        f"CTFtime: {event.get('ctftime_url') or 'N/A'}",
        f"URL: {event.get('url') or 'N/A'}",
        "---",
    ]
    return "\n".join(lines)


def build_events_page_embed(
    events: list[dict], page: int, page_size: int, title: str = "Upcoming CTFs"
) -> discord.Embed:
    total_pages = max(1, (len(events) + page_size - 1) // page_size)
    start_index = page * page_size
    slice_events = events[start_index : start_index + page_size]

    embed = discord.Embed(title=title, color=discord.Color.gold())
    for event in slice_events:
        embed.add_field(
            name=event.get("title") or "CTF Event",
            value=_format_event_block(event),
            inline=False,
        )

    embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed
