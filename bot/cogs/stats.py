from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.db.repository import (
    MessageLeaderboardEntry,
    Repository,
    UserMessageStats,
)
from bot.utils.embeds import build_simple_embed


logger = logging.getLogger(__name__)
_TRACKED_MESSAGE_TYPES = {
    discord.MessageType.default,
    discord.MessageType.reply,
    discord.MessageType.thread_starter_message,
}


def _format_timestamp(value: str | None, style: str = "f") -> str:
    if not value:
        return "N/A"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def _user_mention(user_id: int) -> str:
    return f"<@{user_id}>"


def _channel_mention(channel_id: int) -> str:
    return f"<#{channel_id}>"


class StatsCog(commands.Cog):
    stats = app_commands.Group(name="stats", description="Message statistics")

    def __init__(self, bot: commands.Bot, repo: Repository) -> None:
        self.bot = bot
        self.repo = repo

    @staticmethod
    def _get_sync_targets(
        guild: discord.Guild,
        channel: discord.TextChannel | None,
    ) -> list[discord.TextChannel | discord.Thread]:
        if channel is not None:
            return [channel, *channel.threads]
        return [*guild.text_channels, *guild.threads]

    @staticmethod
    def _should_track_message(message: discord.Message) -> bool:
        if message.guild is None:
            return False
        if message.author.bot or message.webhook_id is not None:
            return False
        return message.type in _TRACKED_MESSAGE_TYPES

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._should_track_message(message):
            return
        try:
            await self.repo.record_message(
                message_id=message.id,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                created_at=message.created_at.astimezone(timezone.utc).isoformat(),
            )
        except Exception:
            logger.exception("Failed to record message %s", message.id)

    def _build_leaderboard_embed(
        self,
        entries: list[MessageLeaderboardEntry],
        limit: int,
        channel: discord.TextChannel | None = None,
    ) -> discord.Embed:
        scope = channel.mention if channel else "entire server"
        title = "Message Leaderboard"
        lines = []
        for index, entry in enumerate(entries, start=1):
            lines.append(
                f"**{index}.** {_user_mention(entry.user_id)}"
                f" - `{entry.message_count}` messages"
            )
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.add_field(name="Scope", value=scope, inline=False)
        embed.set_footer(text=f"Showing top {min(limit, len(entries))} users")
        return embed

    def _build_user_stats_embed(
        self,
        member: discord.Member,
        stats: UserMessageStats,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Message Stats - {member.display_name}",
            description=f"User: {member.mention}",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Total messages",
            value=f"`{stats.message_count}`",
            inline=True,
        )
        embed.add_field(name="Rank", value=f"`#{stats.rank}`", inline=True)
        embed.add_field(
            name="Active channels",
            value=f"`{stats.active_channels}`",
            inline=True,
        )
        embed.add_field(
            name="First tracked message",
            value=_format_timestamp(stats.first_message_at),
            inline=False,
        )
        embed.add_field(
            name="Last tracked message",
            value=(
                f"{_format_timestamp(stats.last_message_at)}"
                f" ({_format_timestamp(stats.last_message_at, 'R')})"
            ),
            inline=False,
        )
        if stats.top_channels:
            lines = [
                f"{_channel_mention(channel.channel_id)}"
                f" - `{channel.message_count}` messages"
                for channel in stats.top_channels
            ]
            embed.add_field(name="Top channels", value="\n".join(lines), inline=False)
        embed.set_footer(text="Counts update in real time for newly tracked messages.")
        return embed

    @stats.command(name="leaderboard", description="Show the top message senders")
    @app_commands.describe(
        limit="Number of users to show (max 20)",
        channel="Optional text channel to filter",
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        limit: int = 10,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return

        limit = max(3, min(limit, 20))
        entries = await self.repo.get_message_leaderboard(
            guild_id=interaction.guild.id,
            limit=limit,
            channel_id=channel.id if channel else None,
        )
        if not entries:
            scope = channel.mention if channel else "this server"
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No stats yet",
                    (
                        f"No tracked messages found for {scope}.\n"
                        "The bot will count new messages automatically, or an admin can run `/stats sync`."
                    ),
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=self._build_leaderboard_embed(entries, limit, channel),
        )

    @stats.command(name="user", description="Show message stats for a user")
    @app_commands.describe(member="Member to inspect")
    async def user(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return

        stats = await self.repo.get_user_message_stats(
            guild_id=interaction.guild.id,
            user_id=member.id,
        )
        if stats is None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No stats yet",
                    (
                        f"No tracked messages found for {member.mention}.\n"
                        "The bot will count new messages automatically, or an admin can run `/stats sync`."
                    ),
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=self._build_user_stats_embed(member, stats),
        )

    @stats.command(name="sync", description="Backfill message stats from history")
    @app_commands.describe(
        limit_per_channel="Maximum messages to scan in each channel (max 5000)",
        channel="Optional channel to scan instead of the whole server",
    )
    @app_commands.default_permissions(administrator=True)
    async def sync(
        self,
        interaction: discord.Interaction,
        limit_per_channel: int = 500,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Admin only",
                    "Only admins can sync historical message stats.",
                ),
                ephemeral=True,
            )
            return

        limit_per_channel = max(50, min(limit_per_channel, 5000))
        targets: list[discord.TextChannel | discord.Thread] = list(
            self._get_sync_targets(interaction.guild, channel)
        )

        # Also scan archived threads — guild.threads only returns active ones
        source_channels: list[discord.TextChannel] = (
            [channel] if channel is not None else list(interaction.guild.text_channels)
        )
        for tc in source_channels:
            try:
                async for archived in tc.archived_threads(limit=None):
                    targets.append(archived)
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.defer(thinking=True, ephemeral=True)

        inserted = 0
        scanned = 0
        skipped_channels = 0

        for text_channel in targets:
            batch: list[tuple[int, int, int, int, str]] = []
            try:
                async for message in text_channel.history(
                    limit=limit_per_channel,
                    oldest_first=True,
                ):
                    scanned += 1
                    if not self._should_track_message(message):
                        continue
                    batch.append(
                        (
                            message.id,
                            message.guild.id,
                            message.channel.id,
                            message.author.id,
                            message.created_at.astimezone(timezone.utc).isoformat(),
                        )
                    )
                    if len(batch) >= 500:
                        inserted += await self.repo.record_messages(batch)
                        batch.clear()
                if batch:
                    inserted += await self.repo.record_messages(batch)
            except (discord.Forbidden, discord.HTTPException):
                skipped_channels += 1
                logger.warning("Failed to sync history for channel %s", text_channel.id)

        scope = channel.mention if channel else "all text channels and active threads"
        await interaction.followup.send(
            embed=build_simple_embed(
                "Stats sync complete",
                (
                    f"Scope: {scope}\n"
                    f"Channels scanned: {len(targets) - skipped_channels}/{len(targets)}\n"
                    f"Messages scanned: {scanned}\n"
                    f"New tracked messages: {inserted}\n"
                    f"Skipped channels: {skipped_channels}"
                ),
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCog(bot, bot.repo))
