from __future__ import annotations

import csv
import hmac
import io
import json
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import CTF_REMOVE_PASSWORD
from bot.db.repository import Repository
from bot.services.ctftime import fetch_archived_events, fetch_event, fetch_running_events, fetch_upcoming_events
from bot.services.platform import create_adapter
from bot.services.guild_setup import (
    create_ctf_category_and_channels,
    delete_ctf_category_and_channels,
    ensure_ctf_role,
    hide_ctf_category_and_channels,
)
from bot.utils.embeds import build_simple_embed
from bot.views.ctf_pagination import CtfPaginationView


class CtfCog(commands.Cog):
    ctf = app_commands.Group(name="ctf", description="CTFtime commands")

    def __init__(self, bot: commands.Bot, repo: Repository) -> None:
        self.bot = bot
        self.repo = repo

    @ctf.command(name="upcoming", description="List upcoming CTF events")
    @app_commands.describe(limit="Number of events to show (max 50)")
    async def upcoming(self, interaction: discord.Interaction, limit: int = 10) -> None:
        limit = max(3, min(limit, 50))
        await interaction.response.defer()
        try:
            events = await fetch_upcoming_events(limit=limit)
        except Exception:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "CTFtime error",
                    "Unable to fetch upcoming events. Try again later.",
                )
            )
            return
        if not events:
            await interaction.followup.send(
                embed=build_simple_embed("No events", "No upcoming CTFs found.")
            )
            return
        view = CtfPaginationView(events=events, author_id=interaction.user.id, page_size=3)
        embeds = view.build_page_payload()
        message = await interaction.followup.send(embeds=embeds, view=view)
        view.message = message

    @ctf.command(name="join", description="Create category and channels for a CTF")
    @app_commands.describe(event_id="CTFtime event ID")
    @app_commands.default_permissions(administrator=True)
    async def join(self, interaction: discord.Interaction, event_id: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Admin only", "Only admins can create CTF channels."
                ),
                ephemeral=True,
            )
            return

        existing = await self.repo.get_ctf_event(interaction.guild.id, event_id)
        if existing:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "CTF already configured",
                    f"Event already exists: {existing.event_title} (ID {existing.ctftime_event_id}).",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            event = await fetch_event(event_id)
        except Exception:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "CTFtime error",
                    "Unable to fetch event details. Check the ID.",
                )
            )
            return
        event_title = event.get("title") or f"CTF {event_id}"

        try:
            category, channels = await create_ctf_category_and_channels(
                interaction.guild, event_title
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Missing permissions",
                    "Bot may be missing Manage Channels permission.",
                )
            )
            return
        except Exception:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Setup error",
                    "Unable to create category/channels. Try again later.",
                )
            )
            return

        await self.repo.upsert_ctf_event(
            guild_id=interaction.guild.id,
            ctftime_event_id=event_id,
            event_title=event_title,
            category_id=category.id,
            channels=channels,
            start_time=event.get("start"),
            finish_time=event.get("finish"),
        )

        # Ensure @ctf role exists for /done access
        try:
            ctf_role = await ensure_ctf_role(interaction.guild)
            role_note = f"\nRole `@{ctf_role.name}` is ready for `/done` access."
        except discord.Forbidden:
            role_note = "\nCould not create `@ctf` role — missing Manage Roles permission."
        except Exception:
            role_note = ""

        status = f"Created category `{category.name}` with {len(channels)} channels.{role_note}"

        await interaction.followup.send(
            embed=build_simple_embed(
                "CTF configured",
                status,
            )
        )

    @ctf.command(name="running", description="List currently running CTF events from CTFtime")
    @app_commands.describe(limit="Number of events to show (max 20)")
    async def running(self, interaction: discord.Interaction, limit: int = 10) -> None:
        limit = max(3, min(limit, 20))
        await interaction.response.defer()
        try:
            events = await fetch_running_events(limit=limit)
        except Exception:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "CTFtime error",
                    "Unable to fetch running events. Try again later.",
                )
            )
            return
        if not events:
            await interaction.followup.send(
                embed=build_simple_embed("No running CTFs", "No CTFs are currently running.")
            )
            return
        view = CtfPaginationView(events=events, author_id=interaction.user.id, page_size=3)
        embeds = view.build_page_payload()
        message = await interaction.followup.send(embeds=embeds, view=view)
        view.message = message

    @ctf.command(name="archive", description="List recently ended CTF events from CTFtime")
    @app_commands.describe(
        limit="Number of events to show (max 20)",
        days="How many days back to look (default 30)",
    )
    async def archive(
        self, interaction: discord.Interaction, limit: int = 10, days: int = 30
    ) -> None:
        limit = max(3, min(limit, 20))
        days = max(1, min(days, 90))
        await interaction.response.defer()
        try:
            events = await fetch_archived_events(limit=limit, window_days=days)
        except Exception:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "CTFtime error",
                    "Unable to fetch archived events. Try again later.",
                )
            )
            return
        if not events:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No archived CTFs",
                    f"No CTFs ended in the past {days} days.",
                )
            )
            return
        view = CtfPaginationView(
            events=events,
            author_id=interaction.user.id,
            page_size=3,
            title=f"Archived CTFs (last {days}d)",
        )
        embeds = view.build_page_payload()
        message = await interaction.followup.send(embeds=embeds, view=view)
        view.message = message

    async def _resolve_event(
        self, interaction: discord.Interaction, event_id: int | None
    ):
        if interaction.guild is None:
            return None
        events = await self.repo.list_ctf_events(interaction.guild.id)
        if not events:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No active CTF",
                    "Run /ctf join first to create channels.",
                )
            )
            return None
        if event_id is None:
            if len(events) == 1:
                return events[0]
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Need event ID",
                    "Multiple events in this server. Please provide event_id.",
                )
            )
            return None
        event = next((e for e in events if e.ctftime_event_id == event_id), None)
        if event is None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Event not found",
                    f"Event ID {event_id} not found in this server.",
                )
            )
        return event

    @ctf.command(name="list", description="List joined CTF events")
    async def list_events(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return
        events = await self.repo.list_ctf_events(interaction.guild.id)
        if not events:
            await interaction.response.send_message(
                embed=build_simple_embed("No active CTF", "No events joined yet."),
            )
            return

        lines = []
        for event in events:
            lines.append(f"{event.ctftime_event_id} - {event.event_title}")

        await interaction.response.send_message(
            embed=build_simple_embed("CTF Events", "\n".join(lines)),
        )

    async def _handle_hidden(
        self, interaction: discord.Interaction, event_id: int | None
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Admin only",
                    "Only admins can use this command.",
                )
            )
            return
        event = await self._resolve_event(interaction, event_id)
        if event is None:
            return

        await interaction.response.defer()
        try:
            await hide_ctf_category_and_channels(interaction.guild, event.category_id)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Missing permissions",
                    "Bot may be missing Manage Channels permission.",
                )
            )
            return
        except Exception:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Hide error",
                    "Unable to hide category/channels. Try again later.",
                )
            )
            return

        await interaction.followup.send(
            embed=build_simple_embed(
                "Hidden",
                f"Category `{event.event_title}` is now hidden.",
            )
        )

    @ctf.command(name="hidden", description="Hide CTF category from non-admins")
    @app_commands.describe(event_id="CTFtime event ID (required if multiple)")
    @app_commands.default_permissions(administrator=True)
    async def hidden(
        self, interaction: discord.Interaction, event_id: int | None = None
    ) -> None:
        await self._handle_hidden(interaction, event_id)

    @ctf.command(name="remove", description="Remove CTF category and data")
    @app_commands.describe(
        event_id="CTFtime event ID (required if multiple)",
        password="Remove password",
    )
    @app_commands.default_permissions(administrator=True)
    async def remove(
        self,
        interaction: discord.Interaction,
        event_id: int | None = None,
        password: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Admin only",
                    "Only admins can use this command.",
                ),
                ephemeral=True,
            )
            return

        if not CTF_REMOVE_PASSWORD:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Missing config",
                    "CTF_REMOVE_PASSWORD is not set in .env.",
                ),
                ephemeral=True,
            )
            return
        if not hmac.compare_digest(password or "", CTF_REMOVE_PASSWORD):
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Wrong password",
                    "Incorrect password.",
                ),
                ephemeral=True,
            )
            return

        event = await self._resolve_event(interaction, event_id)
        if event is None:
            return

        await interaction.response.defer(ephemeral=True)

        try:
            await delete_ctf_category_and_channels(
                interaction.guild, event.category_id
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Missing permissions",
                    "Bot may be missing Manage Channels permission.",
                ),
                ephemeral=True,
            )
            return
        except Exception:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Remove error",
                    "Unable to delete category/channels. Try again later.",
                ),
                ephemeral=True,
            )
            return

        await self.repo.delete_ctf_event(
            interaction.guild.id, event.ctftime_event_id
        )

        await interaction.followup.send(
            embed=build_simple_embed(
                "Removed",
                f"Deleted category and data for `{event.event_title}`.",
            ),
            ephemeral=True,
        )


    @ctf.command(name="progress", description="Show challenge progress for a CTF event")
    @app_commands.describe(event_id="CTFtime event ID (required if multiple)")
    async def progress(
        self, interaction: discord.Interaction, event_id: int | None = None
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return
        event = await self._resolve_event(interaction, event_id)
        if event is None:
            return

        challenges = await self.repo.list_challenges(interaction.guild.id, event.ctftime_event_id)
        if not challenges:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No challenges",
                    "No challenges tracked yet. Use `/challenge` or `/challenge-fetch` to add them.",
                )
            )
            return

        total = len(challenges)
        solved = sum(1 for c in challenges if c.status == "done")
        open_count = total - solved
        pct = int(solved / total * 100) if total > 0 else 0

        # Per-category breakdown
        from collections import defaultdict
        cat_total: dict[str, int] = defaultdict(int)
        cat_solved: dict[str, int] = defaultdict(int)
        for c in challenges:
            cat_total[c.category] += 1
            if c.status == "done":
                cat_solved[c.category] += 1

        bar_width = 8
        lines = []
        for cat in sorted(cat_total):
            t = cat_total[cat]
            s = cat_solved[cat]
            filled = int(s / t * bar_width) if t > 0 else 0
            bar = "█" * filled + "░" * (bar_width - filled)
            suffix = " ✓" if s == t else ""
            lines.append(f"`{cat:<6}` {bar} {s}/{t}{suffix}")

        # Last solve time
        solved_times = [c.solved_at for c in challenges if c.solved_at]
        last_solve = ""
        if solved_times:
            latest = max(solved_times)
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(latest).astimezone(timezone.utc)
                ts = int(dt.timestamp())
                last_solve = f"\nLast solve: <t:{ts}:R>"
            except Exception:
                pass

        desc = (
            f"**Total:** {total} challenges\n"
            f"**Solved:** {solved} ({pct}%)\n"
            f"**Open:** {open_count}\n\n"
            + "\n".join(lines)
            + last_solve
        )

        embed = discord.Embed(
            title=f"CTF Progress — {event.event_title}",
            description=desc,
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @ctf.command(name="export", description="Export challenge data for this CTF as JSON or CSV")
    @app_commands.describe(
        event_id="CTFtime event ID (required if multiple)",
        format="Export format: json or csv",
    )
    @app_commands.choices(format=[
        app_commands.Choice(name="JSON", value="json"),
        app_commands.Choice(name="CSV", value="csv"),
    ])
    async def export(
        self,
        interaction: discord.Interaction,
        format: str = "json",
        event_id: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return

        event = await self._resolve_event(interaction, event_id)
        if event is None:
            return

        challenges = await self.repo.list_challenges(
            interaction.guild.id, event.ctftime_event_id
        )

        exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if format == "json":
            payload = {
                "event_id": event.ctftime_event_id,
                "event_title": event.event_title,
                "exported_at": exported_at,
                "challenges": [
                    {
                        "name": c.challenge_name,
                        "category": c.category,
                        "status": c.status,
                        "solved_by": c.solved_by,
                        "solved_at": c.solved_at,
                        "thread_id": c.thread_id,
                    }
                    for c in challenges
                ],
            }
            content = json.dumps(payload, ensure_ascii=False, indent=2)
            filename = f"challenges_{event.ctftime_event_id}.json"
            file_bytes = content.encode("utf-8")
        else:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                ["challenge_name", "category", "status", "solved_by", "solved_at", "thread_id"]
            )
            for c in challenges:
                solved_by_str = ";".join(str(uid) for uid in c.solved_by)
                writer.writerow(
                    [
                        c.challenge_name,
                        c.category,
                        c.status,
                        solved_by_str,
                        c.solved_at or "",
                        c.thread_id,
                    ]
                )
            content = buf.getvalue()
            filename = f"challenges_{event.ctftime_event_id}.csv"
            file_bytes = content.encode("utf-8")

        discord_file = discord.File(
            fp=io.BytesIO(file_bytes),
            filename=filename,
        )
        await interaction.response.send_message(
            content=f"Export for **{event.event_title}** ({len(challenges)} challenges)",
            file=discord_file,
        )

    @ctf.command(name="connect", description="Connect CTF event to a platform (CTFd/rCTF)")
    @app_commands.describe(
        platform="Platform type",
        url="Platform URL (e.g. https://ctf.example.com)",
        event_id="CTFtime event ID (required if multiple events)",
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="CTFd", value="ctfd"),
        app_commands.Choice(name="rCTF", value="rctf"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def connect(
        self,
        interaction: discord.Interaction,
        platform: str,
        url: str,
        event_id: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Admin only",
                    "Only admins can connect platforms.",
                ),
                ephemeral=True,
            )
            return

        event = await self._resolve_event(interaction, event_id)
        if event is None:
            return

        await interaction.response.defer(ephemeral=True)

        url = url.strip().rstrip("/")
        if not url:
            await interaction.followup.send(
                embed=build_simple_embed("Invalid URL", "Platform URL cannot be empty."),
                ephemeral=True,
            )
            return

        await self.repo.upsert_platform_config(
            guild_id=interaction.guild.id,
            ctftime_event_id=event.ctftime_event_id,
            platform_type=platform,
            platform_url=url,
            team_token=None,
            team_name=None,
            category_mapping={},
        )

        account_channel_id = event.channels.get("Account") or event.channels.get("account")
        account_channel: discord.TextChannel | None = None
        if account_channel_id:
            ch = interaction.guild.get_channel(int(account_channel_id))
            if isinstance(ch, discord.TextChannel):
                account_channel = ch

        if account_channel is not None:
            if platform == "ctfd":
                instructions = (
                    "**How to get your token:**\n"
                    "1. Log in to the CTFd platform\n"
                    "2. Go to **Settings** -> **Access Tokens** -> **Generate**\n"
                    "3. Copy the token and run:\n"
                    "```\n/auth token <your-token>\n```"
                )
            else:
                instructions = (
                    "**How to authenticate:**\n"
                    "**Option 1 — Team token (recommended):**\n"
                    "Use your team token from registration:\n"
                    "```\n/auth login <team-token>\n```\n"
                    "**Option 2 — Auth token:**\n"
                    "If you already have an auth token:\n"
                    "```\n/auth token <auth-token>\n```"
                )

            guide_embed = discord.Embed(
                title=f"Platform Connected — {event.event_title}",
                description=(
                    f"**Platform:** {platform.upper()}\n"
                    f"**URL:** {url}\n\n"
                    f"{instructions}"
                ),
                color=discord.Color.teal(),
            )
            try:
                await account_channel.send(embed=guide_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.followup.send(
            embed=build_simple_embed(
                "Platform connected",
                f"**{platform.upper()}** linked to **{event.event_title}**.\n"
                f"URL: {url}"
                + (f"\nSetup guide posted to <#{account_channel.id}>." if account_channel else ""),
            ),
            ephemeral=True,
        )

    # ── /ctf info ────────────────────────────────────────────────────

    @ctf.command(name="info", description="Show event and platform details")
    @app_commands.describe(event_id="CTFtime event ID (optional if single event)")
    async def info(
        self, interaction: discord.Interaction, event_id: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return

        event = await self._resolve_event(interaction, event_id)
        if event is None:
            return

        await interaction.response.defer()

        config = await self.repo.get_platform_config(
            interaction.guild.id, event.ctftime_event_id,
        )

        embed = discord.Embed(
            title=event.event_title,
            color=discord.Color.teal(),
        )

        time_parts: list[str] = []
        if event.start_time:
            time_parts.append(f"Start: {event.start_time}")
        if event.finish_time:
            time_parts.append(f"End: {event.finish_time}")
        embed.add_field(
            name="Event",
            value=(
                f"CTFtime ID: `{event.ctftime_event_id}`\n"
                + ("\n".join(time_parts) + "\n" if time_parts else "")
                + f"Category: <#{event.category_id}>"
            ),
            inline=False,
        )

        if config:
            platform_lines = [
                f"Type: **{config.platform_type.upper()}**",
                f"URL: {config.platform_url}",
            ]
            if config.team_name:
                platform_lines.append(f"Team: **{config.team_name}**")

            adapter = create_adapter(
                config.platform_type, config.platform_url, config.team_token,
            )
            try:
                team_info = await adapter.get_team_info()
                if team_info:
                    platform_lines.append(f"\n**Live team info:**")
                    platform_lines.append(f"Name: {team_info.name}")
                    platform_lines.append(f"Score: `{team_info.score}`")
                    if team_info.rank is not None:
                        platform_lines.append(f"Rank: `#{team_info.rank}`")
                    if team_info.members:
                        platform_lines.append(
                            f"Members: {', '.join(team_info.members[:10])}"
                        )
            except Exception:
                platform_lines.append("*(could not fetch live data)*")

            embed.add_field(
                name="Platform",
                value="\n".join(platform_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Platform",
                value="Not connected. Run `/ctf connect` to link a platform.",
                inline=False,
            )

        challenges = await self.repo.list_challenges(
            interaction.guild.id, event.ctftime_event_id,
        )
        total = len(challenges)
        solved = sum(1 for c in challenges if c.status == "done")
        embed.add_field(
            name="Challenges",
            value=f"{solved}/{total} solved",
            inline=True,
        )

        await interaction.followup.send(embed=embed)

    # ── /team ────────────────────────────────────────────────────────

    @app_commands.command(name="team", description="Show team info from the connected platform")
    @app_commands.describe(event_id="CTFtime event ID (optional if single event)")
    async def team(
        self, interaction: discord.Interaction, event_id: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return

        event = await self._resolve_event(interaction, event_id)
        if event is None:
            return

        await interaction.response.defer()

        config = await self.repo.get_platform_config(
            interaction.guild.id, event.ctftime_event_id,
        )
        if config is None:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No platform",
                    "Run `/ctf connect` first to link a platform.",
                ),
            )
            return

        adapter = create_adapter(
            config.platform_type, config.platform_url, config.team_token,
        )

        try:
            team_info = await adapter.get_team_info()
        except Exception as exc:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Fetch failed",
                    f"Could not get team info: {str(exc)[:300]}",
                ),
            )
            return

        if team_info is None:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No team data",
                    "Platform returned no team info. A team token may be required.",
                ),
            )
            return

        embed = discord.Embed(
            title=f"Team: {team_info.name}",
            color=discord.Color.teal(),
        )
        lines = [f"Score: `{team_info.score}`"]
        if team_info.rank is not None:
            lines.append(f"Rank: `#{team_info.rank}`")
        if team_info.members:
            lines.append(f"Members: {', '.join(team_info.members[:20])}")
        embed.add_field(name="Stats", value="\n".join(lines), inline=False)

        try:
            solves = await adapter.get_team_solves()
            if solves:
                recent = solves[:10]
                solve_lines = []
                for s in recent:
                    line = f"- **{s.challenge_name}**"
                    if s.solver:
                        line += f" by {s.solver}"
                    if s.solved_at:
                        line += f" ({s.solved_at})"
                    solve_lines.append(line)
                embed.add_field(
                    name=f"Recent solves ({len(solves)} total)",
                    value="\n".join(solve_lines)[:1024],
                    inline=False,
                )
        except Exception:
            pass

        embed.set_footer(
            text=f"{config.platform_type.upper()} — {event.event_title}",
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    repo: Repository = bot.repo  # type: ignore[attr-defined]
    cog = CtfCog(bot, repo)
    await bot.add_cog(cog)
