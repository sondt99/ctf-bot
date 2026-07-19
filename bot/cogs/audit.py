from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.config import AUTO_BACKUP_INTERVAL_HOURS, DATABASE_PATH
from bot.services.guild_setup import ensure_bot_admin_category
from bot.utils.embeds import build_simple_embed


class AuditCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ready_once = False
        self._channel_cache: dict[int, dict[str, discord.TextChannel]] = {}
        if AUTO_BACKUP_INTERVAL_HOURS > 0:
            self.auto_backup_loop.change_interval(hours=AUTO_BACKUP_INTERVAL_HOURS)
            self.auto_backup_loop.start()

    async def cog_unload(self) -> None:
        self.auto_backup_loop.cancel()

    @tasks.loop(hours=24)
    async def auto_backup_loop(self) -> None:
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            channels = await self._get_admin_channels(guild)
            if channels is None:
                continue
            backup_channel = channels["backup"]
            db_path = Path(DATABASE_PATH or "ctf_bot.db")
            if not db_path.exists():
                continue
            try:
                with open(db_path, "rb") as f:
                    await backup_channel.send(
                        content="Auto backup",
                        file=discord.File(f, filename="ctf_bot.db"),
                    )
            except Exception:
                pass

    async def _get_admin_channels(
        self, guild: discord.Guild
    ) -> dict[str, discord.TextChannel] | None:
        cached = self._channel_cache.get(guild.id)
        if cached is not None:
            return cached
        try:
            _, channels = await ensure_bot_admin_category(guild)
            self._channel_cache[guild.id] = channels
            return channels
        except Exception:
            return None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.ready_once:
            return
        self.ready_once = True
        for guild in self.bot.guilds:
            try:
                await self._get_admin_channels(guild)
            except Exception:
                continue

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._get_admin_channels(guild)

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        guild = channel.guild
        cached = self._channel_cache.get(guild.id)
        if cached and any(c.id == channel.id for c in cached.values()):
            self._channel_cache.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_app_command_completion(
        self, interaction: discord.Interaction, command: discord.app_commands.Command
    ) -> None:
        if interaction.guild is None:
            return

        channels = await self._get_admin_channels(interaction.guild)
        if channels is None:
            return

        log_channel = channels["log"]
        user = interaction.user
        command_name = command.qualified_name
        log_embed = build_simple_embed(
            "Command Log",
            f"User: {user}\nCommand: /{command_name}\nTime: {datetime.now(timezone.utc).isoformat()}",
        )
        try:
            await log_channel.send(embed=log_embed)
        except discord.NotFound:
            self._channel_cache.pop(interaction.guild.id, None)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    embed=build_simple_embed(
                        "Command Error",
                        "An unexpected error occurred. Please try again.",
                    ),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

        if interaction.guild is None:
            return

        channels = await self._get_admin_channels(interaction.guild)
        if channels is None:
            return

        log_channel = channels["log"]
        log_embed = build_simple_embed("Command Error", f"Error: {error}")
        try:
            await log_channel.send(embed=log_embed)
        except discord.HTTPException:
            pass

    # ── /backup ──────────────────────────────────────────────────────

    @app_commands.command(name="backup", description="Upload database backup to BOT category")
    @app_commands.default_permissions(administrator=True)
    async def backup(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return

        if not interaction.permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed("Admin only", "Only admins can use this command."),
                ephemeral=True,
            )
            return

        db_path = Path(DATABASE_PATH or "ctf_bot.db")
        if not db_path.exists():
            await interaction.response.send_message(
                embed=build_simple_embed("Not found", "Database file not found."),
                ephemeral=True,
            )
            return

        channels = await self._get_admin_channels(interaction.guild)
        if channels is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Error", "Could not create BOT category."),
                ephemeral=True,
            )
            return

        backup_channel = channels["backup"]

        await interaction.response.defer(ephemeral=True)

        with open(db_path, "rb") as f:
            await backup_channel.send(file=discord.File(f, filename="ctf_bot.db"))

        await interaction.followup.send(
            embed=build_simple_embed("Done", f"Database uploaded to {backup_channel.mention}."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AuditCog(bot))
