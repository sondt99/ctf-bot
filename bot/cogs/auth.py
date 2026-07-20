from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.db.repository import Repository
from bot.services.platform import RCTFAdapter, create_adapter
from bot.utils.embeds import build_simple_embed

_log = logging.getLogger(__name__)


class AuthCog(commands.Cog):
    auth = app_commands.Group(name="auth", description="Platform authentication")

    def __init__(self, bot: commands.Bot, repo: Repository) -> None:
        self.bot = bot
        self.repo = repo

    async def _resolve_event_and_config(
        self,
        interaction: discord.Interaction,
        event_id: int | None,
    ) -> tuple | None:
        """Resolve the CTF event and its platform config.

        Returns (event, config) or sends an error and returns None.
        """
        if interaction.guild is None:
            await interaction.followup.send(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return None

        events = await self.repo.list_ctf_events(interaction.guild.id)
        if not events:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No active CTF",
                    "No CTF events in this server. Run `/ctf join` first.",
                ),
                ephemeral=True,
            )
            return None

        if event_id is None:
            if len(events) == 1:
                event = events[0]
            else:
                await interaction.followup.send(
                    embed=build_simple_embed(
                        "Need event ID",
                        "Multiple events in this server. Please provide event_id.",
                    ),
                    ephemeral=True,
                )
                return None
        else:
            event = next((e for e in events if e.ctftime_event_id == event_id), None)
            if event is None:
                await interaction.followup.send(
                    embed=build_simple_embed(
                        "Event not found",
                        f"Event ID {event_id} not found in this server.",
                    ),
                    ephemeral=True,
                )
                return None

        config = await self.repo.get_platform_config(
            interaction.guild.id, event.ctftime_event_id
        )
        if config is None:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No platform configured",
                    "Run `/ctf connect` first to link a platform.",
                ),
                ephemeral=True,
            )
            return None

        return event, config

    @auth.command(name="token", description="Save your platform API token")
    @app_commands.describe(
        token="Your platform API token",
        event_id="CTFtime event ID (required if multiple events)",
    )
    async def token(
        self,
        interaction: discord.Interaction,
        token: str,
        event_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        result = await self._resolve_event_and_config(interaction, event_id)
        if result is None:
            return
        event, config = result

        adapter = create_adapter(config.platform_type, config.platform_url, token)
        try:
            valid, username = await adapter.validate_token()
        except Exception as exc:
            _log.warning("Token validation error for guild %s: %s", interaction.guild_id, exc)
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Validation error",
                    f"Could not validate token: {str(exc)[:200]}",
                ),
                ephemeral=True,
            )
            return

        if not valid:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Invalid token",
                    f"Token rejected by {config.platform_type.upper()}: {username}",
                ),
                ephemeral=True,
            )
            return

        await self.repo.upsert_user_token(
            guild_id=interaction.guild.id,  # type: ignore[union-attr]
            ctftime_event_id=event.ctftime_event_id,
            discord_user_id=interaction.user.id,
            auth_token=token,
            platform_username=username,
        )

        await interaction.followup.send(
            embed=build_simple_embed(
                "Token saved",
                f"Authenticated as **{username}** on {config.platform_type.upper()}.",
            ),
            ephemeral=True,
        )

    @auth.command(name="login", description="Login with rCTF team token")
    @app_commands.describe(
        team_token="Your rCTF team token from registration",
        event_id="CTFtime event ID (required if multiple events)",
    )
    async def login(
        self,
        interaction: discord.Interaction,
        team_token: str,
        event_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        result = await self._resolve_event_and_config(interaction, event_id)
        if result is None:
            return
        event, config = result

        if config.platform_type.lower() != "rctf":
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Wrong platform",
                    "This event uses CTFd. Use `/auth token` instead.",
                ),
                ephemeral=True,
            )
            return

        adapter = RCTFAdapter(config.platform_url)
        auth_token = await adapter.login_with_team_token(team_token)
        if auth_token is None:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Login failed",
                    "Could not exchange team token for auth token. Check that the token is correct.",
                ),
                ephemeral=True,
            )
            return

        authed_adapter = create_adapter(config.platform_type, config.platform_url, auth_token)
        try:
            valid, username = await authed_adapter.validate_token()
        except Exception as exc:
            _log.warning("Post-login validation error: %s", exc)
            valid, username = False, str(exc)

        if not valid:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Login error",
                    f"Obtained auth token but validation failed: {username}",
                ),
                ephemeral=True,
            )
            return

        await self.repo.upsert_user_token(
            guild_id=interaction.guild.id,  # type: ignore[union-attr]
            ctftime_event_id=event.ctftime_event_id,
            discord_user_id=interaction.user.id,
            auth_token=auth_token,
            platform_username=username,
        )

        await interaction.followup.send(
            embed=build_simple_embed(
                "Login successful",
                f"Authenticated as **{username}** on rCTF.",
            ),
            ephemeral=True,
        )

    @auth.command(name="status", description="Check your authentication status")
    @app_commands.describe(
        event_id="CTFtime event ID (required if multiple events)",
    )
    async def status(
        self,
        interaction: discord.Interaction,
        event_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        result = await self._resolve_event_and_config(interaction, event_id)
        if result is None:
            return
        event, config = result

        saved = await self.repo.get_user_token(
            interaction.guild.id,  # type: ignore[union-attr]
            event.ctftime_event_id,
            interaction.user.id,
        )
        if saved is None:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Not authenticated",
                    f"No token saved for **{event.event_title}**.\n"
                    f"Use `/auth token` or `/auth login` to authenticate.",
                ),
                ephemeral=True,
            )
            return

        adapter = create_adapter(config.platform_type, config.platform_url, saved.auth_token)
        try:
            valid, username = await adapter.validate_token()
        except Exception:
            valid, username = False, "Could not reach platform"

        if valid:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Authenticated",
                    f"**Event:** {event.event_title}\n"
                    f"**Platform:** {config.platform_type.upper()}\n"
                    f"**Username:** {username}\n"
                    f"**Saved at:** {saved.validated_at}",
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Token expired or invalid",
                    f"**Event:** {event.event_title}\n"
                    f"**Platform:** {config.platform_type.upper()}\n"
                    f"**Status:** {username}\n\n"
                    f"Re-authenticate with `/auth token` or `/auth login`.",
                ),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    repo: Repository = bot.repo  # type: ignore[attr-defined]
    await bot.add_cog(AuthCog(bot, repo))
