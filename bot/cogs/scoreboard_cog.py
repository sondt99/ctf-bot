from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.config import SCOREBOARD_POLL_SECONDS, SCOREBOARD_TEAM_NAME, SCOREBOARD_TOP_N
from bot.db.repository import Repository
from bot.services.scoreboard_fetcher import (
    fetch_ctfd_scoreboard,
    fetch_rctf_scoreboard,
    make_payload_hash,
)
from bot.utils.embeds import build_scoreboard_embed, build_simple_embed


class ScoreboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repo: Repository) -> None:
        self.bot = bot
        self.repo = repo
        self._check_lock = asyncio.Lock()
        self.scoreboard_loop.start()
        asyncio.create_task(self._run_initial_check())

    def cog_unload(self) -> None:
        self.scoreboard_loop.cancel()

    @app_commands.command(name="scoreboard", description="Configure scoreboard polling")
    @app_commands.describe(
        type="Scoreboard type (ctfd or rctf)",
        url="Scoreboard base URL",
        auth_token="Optional auth token",
        team="Team name to track (optional)",
        event_id="CTFtime event ID (required if multiple)",
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="CTFd", value="ctfd"),
            app_commands.Choice(name="rCTF", value="rctf"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    async def scoreboard(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        url: str,
        auth_token: str | None = None,
        team: str | None = None,
        event_id: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Admin only", "Only admins can configure scoreboard."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer()

        events = await self.repo.list_ctf_events(interaction.guild.id)
        if not events:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No active CTF",
                    "Run /ctf join first to create channels.",
                )
            )
            return

        if event_id is None:
            if len(events) == 1:
                event = events[0]
            else:
                await interaction.followup.send(
                    embed=build_simple_embed(
                        "Need event ID",
                        "Multiple events in this server. Please provide event_id.",
                    )
                )
                return
        else:
            event = next(
                (e for e in events if e.ctftime_event_id == event_id), None
            )
            if event is None:
                await interaction.followup.send(
                    embed=build_simple_embed(
                        "Event not found",
                        f"Event ID {event_id} not found in this server.",
                    )
                )
                return

        scoreboard_channel_id = event.channels.get("Scoreboard")
        if not scoreboard_channel_id:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Missing channel", "Scoreboard channel not found."
                )
            )
            return

        await self.repo.upsert_scoreboard_config(
            guild_id=interaction.guild.id,
            ctftime_event_id=event.ctftime_event_id,
            type_name=type.value,
            url=url,
            auth_token=auth_token,
            team_name=team or SCOREBOARD_TEAM_NAME,
            scoreboard_channel_id=scoreboard_channel_id,
        )

        await interaction.followup.send(
            embed=build_simple_embed(
                "Scoreboard configured",
                (
                    f"Event ID: {event.ctftime_event_id}\nType: {type.name}\nURL: {url}"
                    + (
                        f"\nTeam: {team or SCOREBOARD_TEAM_NAME}"
                        if (team or SCOREBOARD_TEAM_NAME)
                        else ""
                    )
                ),
            )
        )

    @app_commands.command(name="scoreboard_list", description="List scoreboard configs")
    async def scoreboard_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return
        configs = await self.repo.list_scoreboard_configs()
        guild_configs = [
            c for c in configs if c.guild_id == interaction.guild.id
        ]
        if not guild_configs:
            await interaction.response.send_message(
                embed=build_simple_embed("No configs", "No scoreboard configs found."),
            )
            return
        lines = []
        for cfg in guild_configs:
            team_text = f", team={cfg.team_name}" if cfg.team_name else ""
            lines.append(
                f"{cfg.ctftime_event_id}: {cfg.type} ({cfg.url}{team_text})"
            )
        await interaction.response.send_message(
            embed=build_simple_embed("Scoreboard configs", "\n".join(lines)),
        )

    @app_commands.command(name="scoreboard_remove", description="Remove scoreboard config")
    @app_commands.describe(event_id="CTFtime event ID")
    @app_commands.default_permissions(administrator=True)
    async def scoreboard_remove(
        self, interaction: discord.Interaction, event_id: int
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Admin only", "Only admins can remove scoreboard configs."
                ),
                ephemeral=True,
            )
            return
        await self.repo.delete_scoreboard_config(interaction.guild.id, event_id)
        await interaction.response.send_message(
            embed=build_simple_embed(
                "Scoreboard removed",
                f"Removed scoreboard config for event ID {event_id}.",
            )
        )

    async def _run_initial_check(self) -> None:
        await self.bot.wait_until_ready()
        await self._run_scoreboard_checks()

    @tasks.loop(seconds=SCOREBOARD_POLL_SECONDS)
    async def scoreboard_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._run_scoreboard_checks()

    async def _run_scoreboard_checks(self) -> None:
        async with self._check_lock:
            configs = await self.repo.list_scoreboard_configs()

            for config in configs:
                event = await self.repo.get_ctf_event(
                    config.guild_id, config.ctftime_event_id
                )
                if not event:
                    continue

                if event.finish_time:
                    try:
                        finish = datetime.fromisoformat(event.finish_time)
                        if datetime.now(timezone.utc) > finish:
                            continue
                    except ValueError:
                        pass

                try:
                    if config.type == "ctfd":
                        entries = await fetch_ctfd_scoreboard(
                            config.url, config.auth_token
                        )
                    elif config.type == "rctf":
                        entries = await fetch_rctf_scoreboard(
                            config.url, config.auth_token
                        )
                    else:
                        continue
                except Exception:
                    continue

                tracked_team = config.team_name or SCOREBOARD_TEAM_NAME
                tracked_entry = None
                if tracked_team:
                    lower_name = tracked_team.lower()
                    for entry in entries:
                        if entry["name"].lower() == lower_name:
                            tracked_entry = entry
                            break
                    if tracked_entry is None:
                        continue
                    entries = [tracked_entry]

                payload_hash = make_payload_hash(entries)
                last_state = await self.repo.get_scoreboard_state(
                    config.guild_id, config.ctftime_event_id
                )
                if last_state and last_state.last_hash == payload_hash:
                    continue

                # Detect rank changes only
                rank_changes = []
                if last_state and last_state.last_payload:
                    try:
                        previous = json.loads(last_state.last_payload)
                        prev_rank = {e["name"]: e["pos"] for e in previous}
                        for entry in entries[:SCOREBOARD_TOP_N]:
                            name = entry["name"]
                            if name in prev_rank and prev_rank[name] != entry["pos"]:
                                delta = prev_rank[name] - entry["pos"]
                                direction = "up" if delta > 0 else "down"
                                rank_changes.append(
                                    (name, direction, entry["pos"], entry["score"], delta)
                                )
                    except Exception:
                        rank_changes = []

                # Update state regardless
                await self.repo.upsert_scoreboard_state(
                    config.guild_id,
                    config.ctftime_event_id,
                    payload_hash,
                    json.dumps(entries, ensure_ascii=False),
                )

                # Only notify when there are rank changes
                if not rank_changes:
                    continue

                changes = [
                    f"{name} {direction} to {pos} ({score})"
                    for name, direction, pos, score, _ in rank_changes
                ]

                channel = self.bot.get_channel(config.scoreboard_channel_id)
                if isinstance(channel, discord.TextChannel):
                    embed = build_scoreboard_embed(
                        entries, changes, config.url, top_n=SCOREBOARD_TOP_N
                    )
                    await channel.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    repo: Repository = bot.repo  # type: ignore[attr-defined]
    await bot.add_cog(ScoreboardCog(bot, repo))
