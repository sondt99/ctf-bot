from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands

from bot.db.repository import Challenge, CtfEvent, Repository
from bot.services.ctfd import CtfdChallenge, fetch_ctfd_challenges
from bot.utils.embeds import build_simple_embed


# Topic channels where /challenge is allowed
_TOPIC_CHANNELS = {"rev", "pwn", "web", "crypto", "for", "misc"}
_TOPIC_CHOICES = ("REV", "PWN", "WEB", "CRYPTO", "FOR", "MISC")
_CATEGORY_DEFAULTS = {
    "rev": "REV",
    "reverse": "REV",
    "reversing": "REV",
    "reverse engineering": "REV",
    "pwn": "PWN",
    "binary": "PWN",
    "binary exploitation": "PWN",
    "binex": "PWN",
    "web": "WEB",
    "web exploitation": "WEB",
    "web security": "WEB",
    "crypto": "CRYPTO",
    "cryptography": "CRYPTO",
    "for": "FOR",
    "forensic": "FOR",
    "forensics": "FOR",
    "misc": "MISC",
    "miscellaneous": "MISC",
}
_EMBED_DESCRIPTION_LIMIT = 3500
_EMBED_FIELD_LIMIT = 1024
_CATEGORY_PAGE_SIZE = 4
_CATEGORY_MAPPING_TIMEOUT_SECONDS = 300
_UNCATEGORIZED = "Uncategorized"


def _truncate_text(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _category_label(category: str | None) -> str:
    label = " ".join((category or "").strip().split())
    return label or _UNCATEGORIZED


def _default_topic_for_category(category: str) -> str:
    key = category.replace("_", " ").replace("-", " ")
    key = " ".join(key.split()).casefold()
    return _CATEGORY_DEFAULTS.get(key, "MISC")


class CategoryMappingSelect(discord.ui.Select):
    def __init__(
        self,
        mapping_view: "CategoryMappingView",
        category: str,
        row: int,
    ) -> None:
        self.mapping_view_ref = mapping_view
        self.category = category
        current_topic = mapping_view.mappings[category]
        options = [
            discord.SelectOption(
                label=topic,
                value=topic,
                default=topic == current_topic,
            )
            for topic in _TOPIC_CHOICES
        ]
        super().__init__(
            placeholder=_truncate_text(f"{category} -> {current_topic}", 150),
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        mapping_view = self.mapping_view_ref
        mapping_view.mappings[self.category] = self.values[0]
        mapping_view.refresh_items()
        await interaction.response.edit_message(
            embed=mapping_view.build_embed(),
            view=mapping_view,
        )


class CategoryMappingButton(discord.ui.Button):
    def __init__(
        self,
        mapping_view: "CategoryMappingView",
        action: str,
        label: str,
        style: discord.ButtonStyle,
        disabled: bool = False,
    ) -> None:
        super().__init__(label=label, style=style, disabled=disabled, row=4)
        self.mapping_view_ref = mapping_view
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        mapping_view = self.mapping_view_ref

        if self.action == "prev":
            mapping_view.page = max(0, mapping_view.page - 1)
            mapping_view.refresh_items()
            await interaction.response.edit_message(
                embed=mapping_view.build_embed(),
                view=mapping_view,
            )
            return

        if self.action == "next":
            mapping_view.page = min(mapping_view.total_pages - 1, mapping_view.page + 1)
            mapping_view.refresh_items()
            await interaction.response.edit_message(
                embed=mapping_view.build_embed(),
                view=mapping_view,
            )
            return

        if self.action == "confirm":
            mapping_view.confirmed = True
            await interaction.response.edit_message(
                embed=build_simple_embed(
                    "Importing challenges",
                    "Category mapping confirmed. Creating threads now...",
                ),
                view=None,
            )
            mapping_view.stop()
            return

        if self.action == "cancel":
            mapping_view.cancelled = True
            await interaction.response.edit_message(
                embed=build_simple_embed(
                    "Import cancelled",
                    "No challenge threads were created.",
                ),
                view=None,
            )
            mapping_view.stop()


class CategoryMappingView(discord.ui.View):
    def __init__(
        self,
        author_id: int,
        categories: list[str],
        challenge_count: int,
    ) -> None:
        super().__init__(timeout=_CATEGORY_MAPPING_TIMEOUT_SECONDS)
        self.author_id = author_id
        self.categories = categories
        self.challenge_count = challenge_count
        self.page = 0
        self.confirmed = False
        self.cancelled = False
        self.timed_out = False
        self.mappings = {
            category: _default_topic_for_category(category) for category in categories
        }
        self.refresh_items()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.categories) + _CATEGORY_PAGE_SIZE - 1) // _CATEGORY_PAGE_SIZE)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            embed=build_simple_embed(
                "Not your import",
                "Only the admin who ran `/challenge-fetch` can choose this mapping.",
            ),
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.timed_out = True
        self.stop()

    def refresh_items(self) -> None:
        self.clear_items()
        start = self.page * _CATEGORY_PAGE_SIZE
        page_categories = self.categories[start : start + _CATEGORY_PAGE_SIZE]
        for row, category in enumerate(page_categories):
            self.add_item(CategoryMappingSelect(self, category, row))

        self.add_item(
            CategoryMappingButton(
                self,
                "prev",
                "Previous",
                discord.ButtonStyle.secondary,
                disabled=self.page == 0,
            )
        )
        self.add_item(
            CategoryMappingButton(
                self,
                "next",
                "Next",
                discord.ButtonStyle.secondary,
                disabled=self.page >= self.total_pages - 1,
            )
        )
        self.add_item(
            CategoryMappingButton(
                self,
                "confirm",
                "Import",
                discord.ButtonStyle.success,
            )
        )
        self.add_item(
            CategoryMappingButton(
                self,
                "cancel",
                "Cancel",
                discord.ButtonStyle.danger,
            )
        )

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Map CTFd categories",
            color=discord.Color.gold(),
        )
        embed.description = (
            f"Fetched `{self.challenge_count}` challenges across "
            f"`{len(self.categories)}` CTFd categories.\n"
            "Choose where each CTFd category should be imported before creating threads."
        )

        start = self.page * _CATEGORY_PAGE_SIZE
        page_categories = self.categories[start : start + _CATEGORY_PAGE_SIZE]
        lines = [
            f"`{category}` -> **{self.mappings[category]}**"
            for category in page_categories
        ]
        embed.add_field(
            name=f"Page {self.page + 1}/{self.total_pages}",
            value="\n".join(lines) or "No categories found.",
            inline=False,
        )
        embed.set_footer(
            text="Mapping is not saved; the bot asks again every time /challenge-fetch runs."
        )
        return embed


class ChallengeCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repo: Repository) -> None:
        self.bot = bot
        self.repo = repo

    # ── helpers ───────────────────────────────────────────────────────

    async def _find_event_by_channel(
        self, guild_id: int, channel: discord.TextChannel
    ):
        """Return the CtfEvent whose category owns this channel, or None."""
        if channel.category_id is None:
            return None
        events = await self.repo.list_ctf_events(guild_id)
        for event in events:
            if event.category_id == channel.category_id:
                return event
        return None

    @staticmethod
    def _channel_topic(channel: discord.TextChannel) -> str | None:
        """Return the normalised topic name if the channel is a topic channel."""
        name = channel.name.lower()
        if name in _TOPIC_CHANNELS:
            return name.upper()
        return None

    @staticmethod
    def _sanitize_challenge_name(name: str) -> str:
        return name.strip().replace("\n", " ")[:100]

    @staticmethod
    def _challenge_key(name: str, category: str) -> tuple[str, str]:
        return name.casefold(), category.casefold()

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        return _truncate_text(value, limit)

    @staticmethod
    def _is_ctfd_challenge_embed(embed: discord.Embed) -> bool:
        return any(
            field.name == "Meta" and "CTFd ID:" in str(field.value)
            for field in embed.fields
        )

    @staticmethod
    def _ctfd_description_from_embed(embed: discord.Embed) -> str | None:
        description = (embed.description or "").strip()
        if not description or description == "No description provided.":
            return None
        return description

    @staticmethod
    def _ctfd_files_from_embed(embed: discord.Embed) -> list[str]:
        for field in embed.fields:
            if field.name == "Files":
                return re.findall(r"\]\(([^)]+)\)", str(field.value))
        return []

    async def _get_thread(self, thread_id: int) -> discord.Thread | None:
        fetched = self.bot.get_channel(thread_id)
        if isinstance(fetched, discord.Thread):
            return fetched
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def _get_ctfd_challenge_message(
        self, thread: discord.Thread, challenge: Challenge
    ) -> discord.Message | None:
        if challenge.ctfd_message_id is not None:
            try:
                message = await thread.fetch_message(challenge.ctfd_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
            else:
                if any(self._is_ctfd_challenge_embed(embed) for embed in message.embeds):
                    return message

        bot_user_id = self.bot.user.id if self.bot.user is not None else None
        try:
            async for message in thread.history(limit=50, oldest_first=True):
                if bot_user_id is not None and message.author.id != bot_user_id:
                    continue
                if any(self._is_ctfd_challenge_embed(embed) for embed in message.embeds):
                    return message
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def _maybe_update_existing_ctfd_challenge(
        self,
        event: CtfEvent,
        ctfd_challenge: CtfdChallenge,
        topic: str,
        existing: Challenge,
    ) -> str:
        if existing.status == "done":
            return "done"

        thread = await self._get_thread(existing.thread_id)
        if thread is None:
            return "missing-thread"

        message = await self._get_ctfd_challenge_message(thread, existing)
        old_description = existing.ctfd_description
        old_files = existing.ctfd_files
        metadata_known = (
            existing.ctfd_challenge_id is not None
            or existing.ctfd_message_id is not None
            or old_description is not None
            or old_files is not None
        )

        if not metadata_known and message is not None:
            for embed in message.embeds:
                if self._is_ctfd_challenge_embed(embed):
                    old_description = self._ctfd_description_from_embed(embed)
                    old_files = self._ctfd_files_from_embed(embed)
                    metadata_known = True
                    break

        if not metadata_known:
            return "tracked"

        new_description = ctfd_challenge.description
        new_files = list(ctfd_challenge.files)
        changed = old_description != new_description or (old_files or []) != new_files
        message_id = message.id if message is not None else existing.ctfd_message_id

        if not changed:
            await self.repo.update_challenge_ctfd_metadata(
                existing.thread_id,
                ctfd_challenge.id,
                new_description,
                new_files,
                message_id,
            )
            return "unchanged"

        embed = self._build_ctfd_challenge_embed(event, ctfd_challenge, topic)
        if message is not None:
            await message.edit(embed=embed)
            message_id = message.id
        else:
            sent = await thread.send(embed=embed)
            message_id = sent.id

        await self.repo.update_challenge_ctfd_metadata(
            existing.thread_id,
            ctfd_challenge.id,
            new_description,
            new_files,
            message_id,
        )
        return "updated"

    async def _thread_is_live(self, thread_id: int) -> bool:
        fetched = self.bot.get_channel(thread_id)
        if fetched is not None:
            return True
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            return True
        return fetched is not None

    async def _existing_challenge_index(
        self, guild_id: int, ctftime_event_id: int
    ) -> dict[tuple[str, str], Challenge]:
        existing = await self.repo.list_challenges(guild_id, ctftime_event_id)
        index: dict[tuple[str, str], Challenge] = {}
        for chall in existing:
            if await self._thread_is_live(chall.thread_id):
                index[self._challenge_key(chall.challenge_name, chall.category)] = chall
            else:
                await self.repo.delete_challenge_by_thread(chall.thread_id)
        return index

    def _get_topic_channel(
        self, guild: discord.Guild, event: CtfEvent, topic: str
    ) -> discord.TextChannel | None:
        channel_id = None
        for key in (topic, topic.title(), topic.lower()):
            raw_channel_id = event.channels.get(key)
            if raw_channel_id is not None:
                try:
                    channel_id = int(raw_channel_id)
                except (TypeError, ValueError):
                    channel_id = None
                break

        channel = guild.get_channel(channel_id) if channel_id is not None else None
        if channel is None and channel_id is not None:
            channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

        category = guild.get_channel(event.category_id)
        if isinstance(category, discord.CategoryChannel):
            channel = discord.utils.get(category.text_channels, name=topic.lower())
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    def _build_manual_challenge_embed(
        self, event: CtfEvent, name: str, topic: str
    ) -> discord.Embed:
        return build_simple_embed(
            f"Challenge: {name}",
            f"**CTF:** {event.event_title}\n"
            f"**Category:** {topic}\n"
            f"**Status:** Open\n\n"
            f"Good luck! When solved, an admin will use `/done` here.",
        )

    def _build_ctfd_challenge_embed(
        self, event: CtfEvent, challenge: CtfdChallenge, topic: str
    ) -> discord.Embed:
        description = challenge.description or "No description provided."
        embed = discord.Embed(
            title=self._truncate(f"Challenge: {challenge.name}", 256),
            url=challenge.url,
            description=self._truncate(description, _EMBED_DESCRIPTION_LIMIT),
            color=discord.Color.gold(),
        )

        meta = [
            f"CTF: {event.event_title}",
            f"Category: {topic}",
            f"CTFd ID: `{challenge.id}`",
        ]
        if challenge.value is not None:
            meta.append(f"Points: `{challenge.value}`")
        if challenge.solves is not None:
            meta.append(f"Solves: `{challenge.solves}`")
        if challenge.challenge_type:
            meta.append(f"Type: `{challenge.challenge_type}`")
        embed.add_field(
            name="Meta",
            value=self._truncate("\n".join(meta), _EMBED_FIELD_LIMIT),
            inline=False,
        )

        if challenge.connection_info:
            embed.add_field(
                name="Connection",
                value=self._truncate(challenge.connection_info, _EMBED_FIELD_LIMIT),
                inline=False,
            )

        if challenge.files:
            file_lines = [
                f"[file {index}]({url})" for index, url in enumerate(challenge.files[:8], 1)
            ]
            if len(challenge.files) > 8:
                file_lines.append(f"... and {len(challenge.files) - 8} more")
            embed.add_field(
                name="Files",
                value=self._truncate("\n".join(file_lines), _EMBED_FIELD_LIMIT),
                inline=False,
            )

        if challenge.tags:
            embed.add_field(
                name="Tags",
                value=self._truncate(", ".join(challenge.tags), _EMBED_FIELD_LIMIT),
                inline=False,
            )

        return embed

    async def _create_tracked_challenge_thread(
        self,
        guild: discord.Guild,
        event: CtfEvent,
        channel: discord.TextChannel,
        name: str,
        topic: str,
        embed: discord.Embed,
        ctfd_challenge: CtfdChallenge | None = None,
    ) -> discord.Thread:
        thread = await channel.create_thread(
            name=name,
            type=discord.ChannelType.public_thread,
        )

        message = await thread.send(embed=embed)
        await self.repo.create_challenge(
            guild_id=guild.id,
            ctftime_event_id=event.ctftime_event_id,
            challenge_name=name,
            category=topic,
            thread_id=thread.id,
            channel_id=channel.id,
            ctfd_challenge_id=ctfd_challenge.id if ctfd_challenge is not None else None,
            ctfd_description=(
                ctfd_challenge.description if ctfd_challenge is not None else None
            ),
            ctfd_files=list(ctfd_challenge.files) if ctfd_challenge is not None else None,
            ctfd_message_id=message.id if ctfd_challenge is not None else None,
        )

        return thread

    @staticmethod
    def _summary_block(items: list[str], limit: int = 10) -> str:
        if not items:
            return "None"
        visible = items[:limit]
        text = "\n".join(visible)
        hidden = len(items) - len(visible)
        if hidden > 0:
            text += f"\n... and {hidden} more"
        return text

    # ── /challenge <name> ─────────────────────────────────────────────

    @app_commands.command(
        name="challenge",
        description="Create a thread for a challenge in the current topic channel",
    )
    @app_commands.describe(name="Challenge name")
    async def challenge(self, interaction: discord.Interaction, name: str) -> None:
        name = self._sanitize_challenge_name(name)
        if not name:
            await interaction.response.send_message(
                embed=build_simple_embed("Invalid name", "Challenge name cannot be empty."),
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Wrong channel type",
                    "Use this command in a text channel under a CTF category.",
                ),
                ephemeral=True,
            )
            return

        topic = self._channel_topic(channel)
        if topic is None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Wrong channel",
                    f"Use this in a topic channel ({', '.join(sorted(_TOPIC_CHANNELS))}).",
                ),
                ephemeral=True,
            )
            return

        event = await self._find_event_by_channel(interaction.guild.id, channel)
        if event is None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No CTF found",
                    "This channel does not belong to any joined CTF event.\n"
                    "Use this command in a topic channel under a CTF category.",
                ),
                ephemeral=True,
            )
            return

        existing = await self._existing_challenge_index(
            interaction.guild.id, event.ctftime_event_id
        )
        duplicate = existing.get(self._challenge_key(name, topic))
        if duplicate is not None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Duplicate challenge",
                    f"Challenge **{name}** already exists in {topic} (<#{duplicate.thread_id}>).",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        thread = await self._create_tracked_challenge_thread(
            guild=interaction.guild,
            event=event,
            channel=channel,
            name=name,
            topic=topic,
            embed=self._build_manual_challenge_embed(event, name, topic),
        )

        await interaction.followup.send(
            embed=build_simple_embed(
                "Challenge created",
                f"Thread **{name}** created in {topic} → {thread.mention}",
            )
        )

    # ── /challenge-fetch ─────────────────────────────────────────────

    @app_commands.command(
        name="challenge-fetch",
        description="Fetch CTFd challenges and create topic threads",
    )
    @app_commands.describe(
        event_id="CTFtime event ID created by /ctf join",
        url="CTFd base URL, for example http://localhost:8000",
        auth_token="CTFd API token (optional for public challenges)",
    )
    @app_commands.default_permissions(administrator=True)
    async def challenge_fetch(
        self,
        interaction: discord.Interaction,
        event_id: int,
        url: str,
        auth_token: str | None = None,
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
                    "Admin only", "Only admins can fetch challenges in bulk."
                ),
                ephemeral=True,
            )
            return

        url = url.strip()
        if not url:
            await interaction.response.send_message(
                embed=build_simple_embed("Invalid URL", "CTFd URL cannot be empty."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        event = await self.repo.get_ctf_event(interaction.guild.id, event_id)
        if event is None:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Event not found",
                    f"Event ID {event_id} not found in this server. Run `/ctf join` first.",
                ),
                ephemeral=True,
            )
            return

        try:
            fetched_challenges = await fetch_ctfd_challenges(url, auth_token)
        except Exception as exc:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "CTFd fetch failed",
                    self._truncate(str(exc), 1800),
                ),
                ephemeral=True,
            )
            return

        if not fetched_challenges:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No challenges",
                    "CTFd returned an empty challenge list.",
                ),
                ephemeral=True,
            )
            return

        categories = sorted(
            {_category_label(challenge.category) for challenge in fetched_challenges},
            key=str.casefold,
        )
        mapping_view = CategoryMappingView(
            author_id=interaction.user.id,
            categories=categories,
            challenge_count=len(fetched_challenges),
        )
        mapping_message = await interaction.followup.send(
            embed=mapping_view.build_embed(),
            view=mapping_view,
            ephemeral=True,
            wait=True,
        )
        await mapping_view.wait()

        if mapping_view.cancelled:
            return

        if not mapping_view.confirmed:
            try:
                await mapping_message.edit(
                    embed=build_simple_embed(
                        "Mapping timed out",
                        "No challenge threads were created. Run `/challenge-fetch` again.",
                    ),
                    view=None,
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    embed=build_simple_embed(
                        "Mapping timed out",
                        "No challenge threads were created. Run `/challenge-fetch` again.",
                    ),
                    ephemeral=True,
                )
            return

        category_mapping = dict(mapping_view.mappings)
        existing = await self._existing_challenge_index(
            interaction.guild.id, event.ctftime_event_id
        )
        existing_by_name = {
            challenge.challenge_name.casefold(): challenge
            for challenge in existing.values()
        }
        created: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for ctfd_challenge in fetched_challenges:
            category = _category_label(ctfd_challenge.category)
            topic = category_mapping.get(category, "MISC")
            channel = self._get_topic_channel(interaction.guild, event, topic)
            name = self._sanitize_challenge_name(ctfd_challenge.name)
            if not name:
                failed.append(f"CTFd ID {ctfd_challenge.id}: empty name")
                continue

            key = self._challenge_key(name, topic)
            duplicate = existing.get(key)
            if duplicate is not None:
                try:
                    outcome = await self._maybe_update_existing_ctfd_challenge(
                        event, ctfd_challenge, topic, duplicate
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failed.append(f"{name} ({topic}): update failed: {exc}")
                    continue

                if outcome == "updated":
                    updated.append(f"{name} ({topic}) -> <#{duplicate.thread_id}>")
                elif outcome == "done":
                    skipped.append(f"{name} ({topic}) already done -> <#{duplicate.thread_id}>")
                elif outcome == "unchanged":
                    skipped.append(f"{name} ({topic}) unchanged -> <#{duplicate.thread_id}>")
                elif outcome == "missing-thread":
                    failed.append(f"{name} ({topic}): tracked thread not found")
                else:
                    skipped.append(f"{name} ({topic}) already tracked -> <#{duplicate.thread_id}>")
                continue

            duplicate_by_name = existing_by_name.get(name.casefold())
            if duplicate_by_name is not None:
                skipped.append(
                    f"{name} already tracked in {duplicate_by_name.category} "
                    f"-> <#{duplicate_by_name.thread_id}>"
                )
                continue

            if channel is None:
                failed.append(f"{name} ({topic}): missing topic channel")
                continue

            try:
                thread = await self._create_tracked_challenge_thread(
                    guild=interaction.guild,
                    event=event,
                    channel=channel,
                    name=name,
                    topic=topic,
                    embed=self._build_ctfd_challenge_embed(
                        event, ctfd_challenge, topic
                    ),
                    ctfd_challenge=ctfd_challenge,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                failed.append(f"{name} ({topic}): {exc}")
                continue

            created.append(f"{name} ({topic}) -> {thread.mention}")
            challenge = Challenge(
                id=0,
                guild_id=interaction.guild.id,
                ctftime_event_id=event.ctftime_event_id,
                challenge_name=name,
                category=topic,
                thread_id=thread.id,
                channel_id=channel.id,
                status="open",
                solved_by=[],
                created_at="",
                solved_at=None,
                ctfd_challenge_id=ctfd_challenge.id,
                ctfd_description=ctfd_challenge.description,
                ctfd_files=list(ctfd_challenge.files),
            )
            existing[key] = challenge
            existing_by_name[name.casefold()] = challenge

        embed = discord.Embed(
            title=f"CTFd challenge fetch - {event.event_title}",
            color=discord.Color.gold(),
        )
        embed.description = (
            f"Fetched: `{len(fetched_challenges)}` | "
            f"Created: `{len(created)}` | "
            f"Updated: `{len(updated)}` | "
            f"Skipped: `{len(skipped)}` | "
            f"Failed: `{len(failed)}`"
        )
        embed.add_field(
            name="Created",
            value=self._truncate(self._summary_block(created), _EMBED_FIELD_LIMIT),
            inline=False,
        )
        if updated:
            embed.add_field(
                name="Updated",
                value=self._truncate(self._summary_block(updated), _EMBED_FIELD_LIMIT),
                inline=False,
            )
        if skipped:
            embed.add_field(
                name="Skipped",
                value=self._truncate(self._summary_block(skipped), _EMBED_FIELD_LIMIT),
                inline=False,
            )
        if failed:
            embed.add_field(
                name="Failed",
                value=self._truncate(self._summary_block(failed), _EMBED_FIELD_LIMIT),
                inline=False,
            )

        try:
            await mapping_message.edit(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /done @user ... ───────────────────────────────────────────────

    @app_commands.command(
        name="done",
        description="Mark the current challenge thread as solved",
    )
    @app_commands.describe(
        solver="The member who solved this challenge",
        solver2="Additional solver (optional)",
        solver3="Additional solver (optional)",
        solver4="Additional solver (optional)",
    )
    async def done(
        self,
        interaction: discord.Interaction,
        solver: discord.Member,
        solver2: discord.Member | None = None,
        solver3: discord.Member | None = None,
        solver4: discord.Member | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return

        is_admin = interaction.user.guild_permissions.administrator
        has_ctf_role = discord.utils.get(interaction.user.roles, name="ctf") is not None
        if not is_admin and not has_ctf_role:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No permission", "Only admins or @ctf role members can use this command."
                ),
                ephemeral=True,
            )
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Wrong channel",
                    "Use this command inside a challenge thread.",
                ),
                ephemeral=True,
            )
            return

        challenge = await self.repo.get_challenge_by_thread(thread.id)

        # If thread wasn't created by /challenge, auto-register it
        if challenge is None:
            parent = thread.parent
            if not isinstance(parent, discord.TextChannel):
                await interaction.response.send_message(
                    embed=build_simple_embed(
                        "Wrong channel",
                        "Cannot determine parent channel for this thread.",
                    ),
                    ephemeral=True,
                )
                return

            event = await self._find_event_by_channel(interaction.guild.id, parent)
            if event is None:
                await interaction.response.send_message(
                    embed=build_simple_embed(
                        "No CTF found",
                        "This thread's parent channel does not belong to any joined CTF event.",
                    ),
                    ephemeral=True,
                )
                return

            topic = self._channel_topic(parent)
            category_name = topic or parent.name.upper()

            # Strip [DONE] prefix if thread was already renamed
            clean_name = thread.name
            if clean_name.upper().startswith("[DONE]"):
                clean_name = clean_name[6:].strip()

            await self.repo.create_challenge(
                guild_id=interaction.guild.id,
                ctftime_event_id=event.ctftime_event_id,
                challenge_name=clean_name,
                category=category_name,
                thread_id=thread.id,
                channel_id=parent.id,
            )
            challenge = await self.repo.get_challenge_by_thread(thread.id)

        if challenge.status == "done":
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Already solved",
                    "This challenge was already marked as done.",
                ),
                ephemeral=True,
            )
            return

        # Collect unique solver IDs
        solvers = [solver]
        for s in (solver2, solver3, solver4):
            if s is not None and s.id not in [sv.id for sv in solvers]:
                solvers.append(s)
        solver_ids = [s.id for s in solvers]

        await interaction.response.defer()

        # Update DB
        await self.repo.mark_challenge_done(thread.id, solver_ids)

        # Rename thread: "challenge name" → "[DONE] challenge name"
        if thread.name.upper().startswith("[DONE]"):
            new_name = thread.name
        else:
            new_name = f"[DONE] {challenge.challenge_name}"
            await thread.edit(name=new_name)

        solver_mentions = ", ".join(s.mention for s in solvers)
        await interaction.followup.send(
            embed=build_simple_embed(
                "Challenge Solved!",
                f"**Challenge:** {challenge.challenge_name}\n"
                f"**Category:** {challenge.category}\n"
                f"**Solved by:** {solver_mentions}\n\n"
                f"Thread renamed to `{new_name}`.",
            )
        )

    # ── /remove-challenge ─────────────────────────────────────────────

    @app_commands.command(
        name="remove-challenge",
        description="Remove the current challenge from tracking (keeps the thread)",
    )
    @app_commands.default_permissions(administrator=True)
    async def remove_challenge(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Admin only", "Only admins can use this command."
                ),
                ephemeral=True,
            )
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Wrong channel",
                    "Use this command inside a challenge thread.",
                ),
                ephemeral=True,
            )
            return

        challenge = await self.repo.get_challenge_by_thread(thread.id)
        if challenge is None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Not tracked",
                    "This thread is not tracked as a challenge.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        deleted = await self.repo.delete_challenge_by_thread(thread.id)
        if deleted:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Challenge removed",
                    f"**{challenge.challenge_name}** has been removed from tracking.\n"
                    f"The thread is still here for reference.",
                )
            )
        else:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Failed",
                    "Could not remove the challenge. It may have already been removed.",
                ),
            )

    # ── /challenges ───────────────────────────────────────────────────

    @app_commands.command(
        name="challenges",
        description="List all challenges for a CTF event",
    )
    @app_commands.describe(event_id="CTFtime event ID (required if multiple CTFs)")
    async def challenges(
        self, interaction: discord.Interaction, event_id: int | None = None
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
            )
            return

        # Resolve event
        events = await self.repo.list_ctf_events(interaction.guild.id)
        if not events:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No active CTF", "No CTF events joined in this server."
                ),
            )
            return

        if event_id is None:
            # Try to infer from current channel's category
            channel = interaction.channel
            if isinstance(channel, discord.TextChannel) and channel.category:
                matched = next(
                    (e for e in events if e.category_id == channel.category_id),
                    None,
                )
                if matched:
                    event = matched
                elif len(events) == 1:
                    event = events[0]
                else:
                    await interaction.response.send_message(
                        embed=build_simple_embed(
                            "Need event ID",
                            "Multiple CTF events. Please provide event_id.",
                        ),
                    )
                    return
            elif len(events) == 1:
                event = events[0]
            else:
                await interaction.response.send_message(
                    embed=build_simple_embed(
                        "Need event ID",
                        "Multiple CTF events. Please provide event_id.",
                    ),
                )
                return
        else:
            event = next(
                (e for e in events if e.ctftime_event_id == event_id), None
            )
            if event is None:
                await interaction.response.send_message(
                    embed=build_simple_embed(
                        "Event not found",
                        f"Event ID {event_id} not found in this server.",
                    ),
                )
                return

        challs = await self.repo.list_challenges(
            interaction.guild.id, event.ctftime_event_id
        )
        if not challs:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    f"Challenges — {event.event_title}",
                    "No challenges created yet. Use `/challenge` in a topic channel.",
                ),
            )
            return

        # Group by category
        by_cat: dict[str, list] = {}
        for ch in challs:
            by_cat.setdefault(ch.category, []).append(ch)

        embed = discord.Embed(
            title=f"Challenges — {event.event_title}",
            color=discord.Color.gold(),
        )

        total = len(challs)
        solved = sum(1 for c in challs if c.status == "done")
        embed.description = f"**Total:** {total} | **Solved:** {solved} | **Open:** {total - solved}"

        for cat in sorted(by_cat.keys()):
            lines = []
            for ch in by_cat[cat]:
                status_icon = "\u2705" if ch.status == "done" else "\u23f3"
                solver_text = ""
                if ch.status == "done" and ch.solved_by:
                    solver_text = " — " + ", ".join(
                        f"<@{uid}>" for uid in ch.solved_by
                    )
                lines.append(f"{status_icon} <#{ch.thread_id}>{solver_text}")
            embed.add_field(
                name=cat, value="\n".join(lines), inline=False
            )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    repo: Repository = bot.repo  # type: ignore[attr-defined]
    await bot.add_cog(ChallengeCog(bot, repo))
