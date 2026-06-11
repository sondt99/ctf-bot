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
from bot.services.ctftime import fetch_event, fetch_running_events, fetch_upcoming_events
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


async def setup(bot: commands.Bot) -> None:
    repo: Repository = bot.repo  # type: ignore[attr-defined]
    cog = CtfCog(bot, repo)
    await bot.add_cog(cog)
