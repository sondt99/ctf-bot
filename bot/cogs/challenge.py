from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.config import CTFD_POLL_INTERVAL_MINUTES
from bot.db.repository import Challenge, CtfEvent, PlatformConfig, Repository
from bot.services.ctfd import CtfdChallenge
from bot.services.platform import PlatformAdapter, PlatformChallenge, create_adapter
from bot.utils.embeds import build_simple_embed

_log = logging.getLogger(__name__)


def filter_new_ctfd_challenges(
    fetched: list[CtfdChallenge],
    tracked: list[Challenge],
) -> list[CtfdChallenge]:
    """Return only the fetched challenges that are not yet tracked.

    A challenge is considered tracked when a Challenge record exists with
    a matching ctfd_challenge_id.  This function is intentionally pure
    (no I/O) so it can be unit-tested directly.
    """
    tracked_ids: set[int] = {
        c.ctfd_challenge_id
        for c in tracked
        if c.ctfd_challenge_id is not None
    }
    return [ch for ch in fetched if ch.id not in tracked_ids]


def filter_new_platform_challenges(
    fetched: list[PlatformChallenge],
    tracked: list[Challenge],
) -> list[PlatformChallenge]:
    tracked_ids: set[str] = set()
    for c in tracked:
        if c.platform_challenge_id:
            tracked_ids.add(c.platform_challenge_id)
        elif c.ctfd_challenge_id is not None:
            tracked_ids.add(str(c.ctfd_challenge_id))
    return [ch for ch in fetched if ch.id not in tracked_ids]


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

_CHALLENGES_PAGE_SIZE = 10


def format_challenge_list_line(challenge: Challenge) -> str:
    """Format one challenge row for the /challenges embed.

    Solved challenges include Discord mentions for everyone in ``solved_by``.
    """
    status_icon = "✅" if challenge.status == "done" else "🔓"
    thread_link = f"<#{challenge.thread_id}>"
    line = (
        f"{status_icon} **{challenge.challenge_name}** "
        f"[{challenge.category}] {thread_link}"
    )
    if challenge.status == "done":
        if challenge.solved_by:
            solvers = ", ".join(f"<@{uid}>" for uid in challenge.solved_by)
            line = f"{line}\n  by {solvers}"
        else:
            line = f"{line}\n  by *(unknown)*"
    return line


class ChallengesView(discord.ui.View):
    def __init__(
        self,
        challenges: list,  # list[Challenge]
        event_title: str,
        author_id: int,
        timeout: int = 180,
    ) -> None:
        super().__init__(timeout=timeout)
        self.challenges = challenges
        self.event_title = event_title
        self.author_id = author_id
        self.page = 0
        self.message: discord.Message | None = None
        self._update_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.challenges) + _CHALLENGES_PAGE_SIZE - 1) // _CHALLENGES_PAGE_SIZE)

    def _update_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.label == "Previous":
                    item.disabled = self.page == 0
                elif item.label == "Next":
                    item.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * _CHALLENGES_PAGE_SIZE
        page_challenges = self.challenges[start : start + _CHALLENGES_PAGE_SIZE]
        total = len(self.challenges)
        solved = sum(1 for c in self.challenges if c.status == "done")
        lines = [format_challenge_list_line(c) for c in page_challenges]
        embed = discord.Embed(
            title=f"Challenges — {self.event_title}",
            description="\n".join(lines) or "No challenges.",
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.total_pages} | {solved}/{total} solved"
        )
        return embed

    async def _update(self, interaction: discord.Interaction) -> None:
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Not allowed",
                    description="Only the command author can use these buttons.",
                ),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
        await self._update(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.page < self.total_pages - 1:
            self.page += 1
        await self._update(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


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
        self._message: discord.Message | None = None
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
        if self._message is not None:
            for item in self.children:
                if hasattr(item, "disabled"):
                    item.disabled = True  # type: ignore[union-attr]
            try:
                await self._message.edit(
                    embed=build_simple_embed(
                        "Timed out",
                        "Category mapping expired after 5 minutes. "
                        "Re-run `/challenge-fetch` to try again.",
                    ),
                    view=self,
                )
            except discord.NotFound:
                pass

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
        self._poll_lock = asyncio.Lock()
        self._active_renamed: set[int] = set()
        if CTFD_POLL_INTERVAL_MINUTES > 0:
            self.platform_poll_loop.change_interval(minutes=CTFD_POLL_INTERVAL_MINUTES)
            self.platform_poll_loop.start()

    async def cog_unload(self) -> None:
        self.platform_poll_loop.cancel()

    # ── platform auto-poll ───────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def platform_poll_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._poll_platform_challenges()

    async def _poll_platform_challenges(self) -> None:
        async with self._poll_lock:
            try:
                configs = await self.repo.list_platform_configs()
            except Exception as exc:
                _log.error("Platform poll: failed to list configs: %s", exc)
                return

            if not configs:
                return

            sem = asyncio.Semaphore(3)
            for config in configs:
                async with sem:
                    await self._poll_single_platform(config)

    async def _poll_single_platform(self, config: PlatformConfig) -> None:
        guild = self.bot.get_guild(config.guild_id)
        if guild is None:
            return

        try:
            event = await self.repo.get_ctf_event(config.guild_id, config.ctftime_event_id)
        except Exception as exc:
            _log.error("Platform poll: DB error getting event %s/%s: %s",
                       config.guild_id, config.ctftime_event_id, exc)
            return

        if event is None:
            return

        if event.finish_time:
            try:
                finish = datetime.fromisoformat(event.finish_time)
                if finish.tzinfo is None:
                    finish = finish.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > finish:
                    return
            except ValueError:
                pass

        adapter = create_adapter(
            config.platform_type, config.platform_url, config.team_token,
        )
        try:
            fetched = await adapter.list_challenges()
        except Exception as exc:
            _log.warning("Platform poll: failed to fetch from %s: %s", config.platform_url, exc)
            await self._log_to_guild(
                guild, f"Platform poll error for **{event.event_title}**: {exc}",
            )
            return

        try:
            tracked = await self.repo.list_challenges(config.guild_id, config.ctftime_event_id)
        except Exception as exc:
            _log.error("Platform poll: DB error listing challenges: %s", exc)
            return

        new_challenges = filter_new_platform_challenges(fetched, tracked)
        if not new_challenges:
            return

        _log.info(
            "Platform poll: %d new challenge(s) for event %s in guild %s",
            len(new_challenges), config.ctftime_event_id, config.guild_id,
        )

        category_mapping = config.category_mapping or {}
        created_names: list[str] = []
        failed_names: list[str] = []

        for pc in new_challenges:
            category = _category_label(pc.category)
            topic = category_mapping.get(category) or _default_topic_for_category(category)
            channel = self._get_topic_channel(guild, event, topic)
            name = self._sanitize_challenge_name(pc.name)
            if not name:
                failed_names.append(f"ID {pc.id}: empty name")
                continue
            if channel is None:
                failed_names.append(f"{name} ({topic}): missing topic channel")
                continue

            try:
                embed = self._build_platform_challenge_embed(
                    event, pc, topic, config.platform_type,
                )
                await self._create_tracked_challenge_thread(
                    guild=guild,
                    event=event,
                    channel=channel,
                    name=name,
                    topic=topic,
                    embed=embed,
                    platform_challenge=pc,
                )
                created_names.append(f"{name} ({topic})")
                await asyncio.sleep(0.5)
            except (discord.Forbidden, discord.HTTPException) as exc:
                failed_names.append(f"{name} ({topic}): {exc}")

        if created_names:
            summary = "\n".join(f"• {n}" for n in created_names[:15])
            if len(created_names) > 15:
                summary += f"\n... and {len(created_names) - 15} more"
            await self._notify_new_challenges(guild, event, summary)

        if failed_names:
            _log.warning(
                "Platform poll: %d failure(s) for event %s: %s",
                len(failed_names), config.ctftime_event_id, "; ".join(failed_names),
            )

        await self._poll_solve_feed(config, adapter, event, guild)
        await self._poll_notifications(config, adapter, event, guild)

    # ── solve feed + notification forwarding ────────────────────────

    async def _poll_solve_feed(
        self,
        config: PlatformConfig,
        adapter: PlatformAdapter,
        event: CtfEvent,
        guild: discord.Guild,
    ) -> None:
        try:
            solves = await adapter.get_team_solves()
        except Exception as exc:
            _log.debug("Solve feed poll failed for %s: %s", config.platform_url, exc)
            return

        if not solves:
            return

        current_ids = {s.challenge_id or s.challenge_name for s in solves}
        old_ids = set(config.last_solve_ids or [])
        new_solves = [
            s for s in solves
            if (s.challenge_id or s.challenge_name) not in old_ids
        ]

        if not new_solves:
            if current_ids != old_ids:
                await self.repo.update_platform_poll_state(
                    config.guild_id, config.ctftime_event_id,
                    last_solve_ids=sorted(current_ids),
                )
            return

        channel = self._get_general_channel(guild, event)
        if channel is not None:
            lines: list[str] = []
            for s in new_solves[:15]:
                line = f"- **{s.challenge_name}**"
                if s.solver:
                    line += f" by {s.solver}"
                lines.append(line)
            if len(new_solves) > 15:
                lines.append(f"... and {len(new_solves) - 15} more")

            embed = discord.Embed(
                title=f"New solves — {event.event_title}",
                description="\n".join(lines),
                color=discord.Color.green(),
            )
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException) as exc:
                _log.debug("Could not send solve feed to #%s: %s", channel.name, exc)

        tracked = await self.repo.list_challenges(config.guild_id, config.ctftime_event_id)
        tracked_by_platform_id: dict[str, Challenge] = {}
        for c in tracked:
            if c.platform_challenge_id:
                tracked_by_platform_id[c.platform_challenge_id] = c
            elif c.ctfd_challenge_id is not None:
                tracked_by_platform_id[str(c.ctfd_challenge_id)] = c

        for s in new_solves:
            cid = s.challenge_id or ""
            chall = tracked_by_platform_id.get(cid)
            if chall is None or chall.status == "done":
                continue
            await self.repo.mark_challenge_done(chall.thread_id, [])
            thread = await self._get_thread(chall.thread_id)
            if thread is not None and not thread.name.upper().startswith("[DONE]"):
                try:
                    await thread.edit(name=f"[DONE] {chall.challenge_name}")
                except (discord.Forbidden, discord.HTTPException):
                    pass

        await self.repo.update_platform_poll_state(
            config.guild_id, config.ctftime_event_id,
            last_solve_ids=sorted(current_ids),
        )

    async def _poll_notifications(
        self,
        config: PlatformConfig,
        adapter: PlatformAdapter,
        event: CtfEvent,
        guild: discord.Guild,
    ) -> None:
        try:
            notifications = await adapter.get_notifications(
                since_id=config.last_notification_id,
            )
        except Exception as exc:
            _log.debug("Notification poll failed for %s: %s", config.platform_url, exc)
            return

        if not notifications:
            return

        channel = self._get_general_channel(guild, event)
        latest_id = config.last_notification_id
        for notif in notifications:
            if channel is not None:
                embed = discord.Embed(
                    title=notif.title or "Platform Notification",
                    description=_truncate_text(notif.content, _EMBED_DESCRIPTION_LIMIT) if notif.content else "",
                    color=discord.Color.blue(),
                )
                embed.set_footer(
                    text=f"{config.platform_type.upper()} — {event.event_title}"
                    + (f" | {notif.date}" if notif.date else ""),
                )
                try:
                    await channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    _log.debug("Could not send notification to #%s: %s", channel.name, exc)

            latest_id = notif.id

        if latest_id and latest_id != config.last_notification_id:
            await self.repo.update_platform_poll_state(
                config.guild_id, config.ctftime_event_id,
                last_notification_id=latest_id,
            )

    def _get_general_channel(
        self, guild: discord.Guild, event: CtfEvent,
    ) -> discord.TextChannel | None:
        general_id = event.channels.get("General") or event.channels.get("general")
        if general_id:
            ch = guild.get_channel(int(general_id))
            if isinstance(ch, discord.TextChannel):
                return ch
        cat = guild.get_channel(event.category_id)
        if isinstance(cat, discord.CategoryChannel) and cat.text_channels:
            return cat.text_channels[0]
        return None

    async def _notify_new_challenges(self, guild: discord.Guild, event: CtfEvent, summary: str) -> None:
        """Post a notification in the general channel for this event."""
        general_channel_id = event.channels.get("General") or event.channels.get("general")
        channel: discord.TextChannel | None = None
        if general_channel_id:
            ch = guild.get_channel(int(general_channel_id))
            if isinstance(ch, discord.TextChannel):
                channel = ch

        if channel is None:
            # Fall back to first text channel in the category
            cat = guild.get_channel(event.category_id)
            if isinstance(cat, discord.CategoryChannel) and cat.text_channels:
                channel = cat.text_channels[0]

        if channel is None:
            return

        embed = discord.Embed(
            title=f"New challenges available — {event.event_title}",
            description=summary,
            color=discord.Color.green(),
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log.warning("CTFd poll: could not send notification to #%s: %s", channel.name, exc)

    async def _log_to_guild(self, guild: discord.Guild, message: str) -> None:
        try:
            from bot.services.guild_setup import ensure_bot_admin_category
            _, channels = await ensure_bot_admin_category(guild)
            log_channel = channels.get("log")
            if log_channel is not None:
                await log_channel.send(
                    embed=build_simple_embed("Poll Error", message[:1800])
                )
        except Exception:
            pass

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
    def _is_platform_challenge_embed(embed: discord.Embed) -> bool:
        return any(
            field.name == "Meta" and re.search(r"\bID:\s*`", str(field.value))
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

    async def _maybe_update_existing_platform_challenge(
        self,
        event: CtfEvent,
        pc: PlatformChallenge,
        topic: str,
        existing: Challenge,
        platform_type: str,
    ) -> str:
        if existing.status == "done":
            return "done"

        thread = await self._get_thread(existing.thread_id)
        if thread is None:
            return "missing-thread"

        old_description = existing.ctfd_description
        old_files = existing.ctfd_files

        new_description = pc.description
        new_files = [f.url for f in pc.files]
        if old_description == new_description and (old_files or []) == new_files:
            return "unchanged"

        embed = self._build_platform_challenge_embed(event, pc, topic, platform_type)
        bot_user_id = self.bot.user.id if self.bot.user is not None else None
        message: discord.Message | None = None
        try:
            async for msg in thread.history(limit=50, oldest_first=True):
                if bot_user_id is not None and msg.author.id != bot_user_id:
                    continue
                if any(self._is_platform_challenge_embed(e) for e in msg.embeds):
                    message = msg
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass

        if message is not None:
            await message.edit(embed=embed)
            msg_id = message.id
        else:
            sent = await thread.send(embed=embed)
            msg_id = sent.id

        ctfd_id: int | None = None
        try:
            ctfd_id = int(pc.id)
        except (TypeError, ValueError):
            pass
        await self.repo.update_challenge_ctfd_metadata(
            existing.thread_id,
            ctfd_id or 0,
            new_description,
            new_files,
            msg_id,
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

    def _build_platform_challenge_embed(
        self,
        event: CtfEvent,
        challenge: PlatformChallenge,
        topic: str,
        platform_type: str = "",
    ) -> discord.Embed:
        description = challenge.description or "No description provided."
        embed = discord.Embed(
            title=self._truncate(f"Challenge: {challenge.name}", 256),
            url=challenge.url,
            description=self._truncate(description, _EMBED_DESCRIPTION_LIMIT),
            color=discord.Color.gold(),
        )

        label = platform_type.upper() or "Platform"
        meta = [
            f"CTF: {event.event_title}",
            f"Category: {topic}",
            f"{label} ID: `{challenge.id}`",
        ]
        if challenge.value is not None:
            meta.append(f"Points: `{challenge.value}`")
        if challenge.solves is not None:
            meta.append(f"Solves: `{challenge.solves}`")
        if challenge.author:
            meta.append(f"Author: {challenge.author}")
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
                f"[{f.name}]({f.url})" for f in challenge.files[:8]
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
        platform_challenge: PlatformChallenge | None = None,
    ) -> discord.Thread:
        thread = await channel.create_thread(
            name=name,
            type=discord.ChannelType.public_thread,
        )

        message = await thread.send(embed=embed)

        ctfd_id: int | None = None
        ctfd_desc: str | None = None
        ctfd_files: list[str] | None = None
        ctfd_msg_id: int | None = None
        platform_id: str | None = None

        if platform_challenge is not None:
            platform_id = platform_challenge.id
            ctfd_desc = platform_challenge.description
            ctfd_files = [f.url for f in platform_challenge.files]
            ctfd_msg_id = message.id
            try:
                ctfd_id = int(platform_challenge.id)
            except (TypeError, ValueError):
                pass
        elif ctfd_challenge is not None:
            ctfd_id = ctfd_challenge.id
            ctfd_desc = ctfd_challenge.description
            ctfd_files = list(ctfd_challenge.files)
            ctfd_msg_id = message.id

        await self.repo.create_challenge(
            guild_id=guild.id,
            ctftime_event_id=event.ctftime_event_id,
            challenge_name=name,
            category=topic,
            thread_id=thread.id,
            channel_id=channel.id,
            ctfd_challenge_id=ctfd_id,
            ctfd_description=ctfd_desc,
            ctfd_files=ctfd_files,
            ctfd_message_id=ctfd_msg_id,
            platform_challenge_id=platform_id,
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
        description="Fetch challenges from connected platform and create topic threads",
    )
    @app_commands.describe(
        event_id="CTFtime event ID (auto-detected if single event)",
        url="Platform URL (uses /ctf connect config if omitted)",
        auth_token="API token (uses saved config if omitted)",
    )
    @app_commands.default_permissions(administrator=True)
    async def challenge_fetch(
        self,
        interaction: discord.Interaction,
        event_id: int | None = None,
        url: str | None = None,
        auth_token: str | None = None,
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
                    "Admin only", "Only admins can fetch challenges in bulk."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        event = await self._resolve_event_for_fetch(interaction, event_id)
        if event is None:
            return

        platform_config = await self.repo.get_platform_config(
            interaction.guild.id, event.ctftime_event_id
        )

        platform_type = "ctfd"
        fetch_url: str | None = None
        fetch_token: str | None = auth_token

        if url:
            fetch_url = url.strip()
            if platform_config:
                platform_type = platform_config.platform_type
        elif platform_config:
            fetch_url = platform_config.platform_url
            platform_type = platform_config.platform_type
            if fetch_token is None:
                fetch_token = platform_config.team_token
        else:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No platform configured",
                    "Run `/ctf connect` first, or provide a URL.",
                ),
                ephemeral=True,
            )
            return

        if not fetch_url:
            await interaction.followup.send(
                embed=build_simple_embed("Invalid URL", "Platform URL cannot be empty."),
                ephemeral=True,
            )
            return

        adapter = create_adapter(platform_type, fetch_url, fetch_token)
        try:
            fetched_challenges = await adapter.list_challenges()
        except Exception as exc:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Fetch failed",
                    self._truncate(str(exc), 1800),
                ),
                ephemeral=True,
            )
            return

        if not fetched_challenges:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No challenges",
                    f"{platform_type.upper()} returned an empty challenge list.",
                ),
                ephemeral=True,
            )
            return

        categories = sorted(
            {_category_label(ch.category) for ch in fetched_challenges},
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
        mapping_view._message = mapping_message
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

        for pc in fetched_challenges:
            category = _category_label(pc.category)
            topic = category_mapping.get(category, "MISC")
            channel = self._get_topic_channel(interaction.guild, event, topic)
            name = self._sanitize_challenge_name(pc.name)
            if not name:
                failed.append(f"ID {pc.id}: empty name")
                continue

            key = self._challenge_key(name, topic)
            duplicate = existing.get(key)
            if duplicate is not None:
                try:
                    outcome = await self._maybe_update_existing_platform_challenge(
                        event, pc, topic, duplicate, platform_type,
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
                    embed=self._build_platform_challenge_embed(
                        event, pc, topic, platform_type,
                    ),
                    platform_challenge=pc,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                failed.append(f"{name} ({topic}): {exc}")
                continue

            created.append(f"{name} ({topic}) -> {thread.mention}")
            await asyncio.sleep(0.5)
            new_challenge = Challenge(
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
                platform_challenge_id=pc.id,
            )
            existing[key] = new_challenge
            existing_by_name[name.casefold()] = new_challenge

        label = platform_type.upper()
        embed = discord.Embed(
            title=f"{label} challenge fetch — {event.event_title}",
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

    async def _resolve_event_for_fetch(
        self, interaction: discord.Interaction, event_id: int | None,
    ) -> CtfEvent | None:
        assert interaction.guild is not None

        if event_id is not None:
            event = await self.repo.get_ctf_event(interaction.guild.id, event_id)
            if event is None:
                await interaction.followup.send(
                    embed=build_simple_embed(
                        "Event not found",
                        f"Event ID {event_id} not found. Run `/ctf join` first.",
                    ),
                    ephemeral=True,
                )
            return event

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

        if len(events) == 1:
            return events[0]

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel) and channel.category_id:
            matched = next(
                (e for e in events if e.category_id == channel.category_id), None,
            )
            if matched:
                return matched

        await interaction.followup.send(
            embed=build_simple_embed(
                "Need event ID",
                "Multiple CTF events active. Provide `event_id` or run from a CTF channel.",
            ),
            ephemeral=True,
        )
        return None

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

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        is_admin = member is not None and member.guild_permissions.administrator
        has_ctf_role = member is not None and discord.utils.get(member.roles, name="ctf") is not None
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

        if challenge is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Error", "Challenge not found in database."),
                ephemeral=True,
            )
            return
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

    # ── /undone ──────────────────────────────────────────────────────

    @app_commands.command(
        name="undone",
        description="Reopen the current challenge thread after a mistaken /done",
    )
    async def undone(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        is_admin = member is not None and member.guild_permissions.administrator
        has_ctf_role = member is not None and discord.utils.get(member.roles, name="ctf") is not None
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
        if challenge is None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Not tracked",
                    "This thread is not tracked as a challenge.",
                ),
                ephemeral=True,
            )
            return
        if challenge.status != "done":
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Already open",
                    "This challenge is already marked as open.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        updated = await self.repo.mark_challenge_open(thread.id)
        if not updated:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Failed",
                    "Could not reopen the challenge. It may have been removed.",
                ),
            )
            return

        new_name = f"[ACTIVE] {challenge.challenge_name}"
        self._active_renamed.add(thread.id)
        try:
            await thread.edit(name=new_name)
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log.debug("Could not rename thread %s to ACTIVE: %s", thread.id, exc)

        await interaction.followup.send(
            embed=build_simple_embed(
                "Challenge Reopened",
                f"**Challenge:** {challenge.challenge_name}\n"
                f"**Category:** {challenge.category}\n\n"
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

        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
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

        view = ChallengesView(
            challenges=challs,
            event_title=event.event_title,
            author_id=interaction.user.id,
        )
        await interaction.response.send_message(embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()

    # ── /ping ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="ping",
        description="Ping all @ctf members in every open challenge thread for this event",
    )
    @app_commands.describe(
        message="Optional message to include with the ping",
        event_id="CTFtime event ID (required if multiple CTFs)",
    )
    async def ping(
        self,
        interaction: discord.Interaction,
        message: str | None = None,
        event_id: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
                ephemeral=True,
            )
            return

        is_admin = interaction.user.guild_permissions.administrator  # type: ignore[union-attr]
        has_ctf_role = discord.utils.get(interaction.user.roles, name="ctf") is not None  # type: ignore[union-attr]
        if not is_admin and not has_ctf_role:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No permission",
                    "Only admins or @ctf role members can use this command.",
                ),
                ephemeral=True,
            )
            return

        # Resolve event from parameter, current channel category, or sole event
        events = await self.repo.list_ctf_events(interaction.guild.id)
        if not events:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No active CTF", "No CTF events joined in this server."
                ),
                ephemeral=True,
            )
            return

        if event_id is not None:
            event = next((e for e in events if e.ctftime_event_id == event_id), None)
            if event is None:
                await interaction.response.send_message(
                    embed=build_simple_embed(
                        "Event not found", f"Event ID {event_id} not found in this server."
                    ),
                    ephemeral=True,
                )
                return
        else:
            channel = interaction.channel
            category_id: int | None = None
            if isinstance(channel, discord.TextChannel):
                category_id = channel.category_id
            elif isinstance(channel, discord.Thread) and isinstance(
                channel.parent, discord.TextChannel
            ):
                category_id = channel.parent.category_id

            matched = (
                next((e for e in events if e.category_id == category_id), None)
                if category_id is not None
                else None
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
                    ephemeral=True,
                )
                return

        # Resolve @ctf role
        ctf_role = discord.utils.get(interaction.guild.roles, name="ctf")
        if ctf_role is None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Role not found",
                    'No role named "ctf" exists in this server.',
                ),
                ephemeral=True,
            )
            return

        ping_content = f"{ctf_role.mention}\n{message}" if message else ctf_role.mention

        # Find all open challenge threads for the event
        challenges = await self.repo.list_challenges(
            interaction.guild.id, event.ctftime_event_id
        )
        open_challenges = [c for c in challenges if c.status == "open"]

        if not open_challenges:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No open challenges",
                    "All challenges are solved or no challenges have been created yet.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        pinged = 0
        failed = 0
        for challenge in open_challenges:
            thread = await self._get_thread(challenge.thread_id)
            if thread is None:
                failed += 1
                continue
            try:
                await thread.send(ping_content)
                pinged += 1
                await asyncio.sleep(0.3)
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

        summary_lines = [
            f"Pinged **@ctf** in **{pinged}/{len(open_challenges)}** open challenge threads.",
        ]
        if failed:
            summary_lines.append(f"Could not reach **{failed}** thread(s).")

        await interaction.followup.send(
            embed=build_simple_embed("Ping sent", " ".join(summary_lines)),
            ephemeral=True,
        )

    # ── /submit ──────────────────────────────────────────────────────

    @app_commands.command(
        name="submit",
        description="Submit a flag for the current challenge thread",
    )
    @app_commands.describe(flag="The flag to submit")
    async def submit(self, interaction: discord.Interaction, flag: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=build_simple_embed("Guild only", "Use this in a server."),
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

        if challenge.status == "done":
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "Already solved",
                    "This challenge is already marked as done.",
                ),
                ephemeral=True,
            )
            return

        challenge_id = challenge.platform_challenge_id
        if challenge_id is None and challenge.ctfd_challenge_id is not None:
            challenge_id = str(challenge.ctfd_challenge_id)
        if not challenge_id:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No platform ID",
                    "This challenge has no platform ID. It may have been created manually.",
                ),
                ephemeral=True,
            )
            return

        config = await self.repo.get_platform_config(
            interaction.guild.id, challenge.ctftime_event_id,
        )
        if config is None:
            await interaction.response.send_message(
                embed=build_simple_embed(
                    "No platform",
                    "No platform connected for this event. Run `/ctf connect` first.",
                ),
                ephemeral=True,
            )
            return

        user_token_record = await self.repo.get_user_token(
            interaction.guild.id, challenge.ctftime_event_id, interaction.user.id,
        )
        submit_token = user_token_record.auth_token if user_token_record else config.team_token

        adapter = create_adapter(config.platform_type, config.platform_url, submit_token)

        await interaction.response.defer(ephemeral=True)

        try:
            result = await adapter.submit_flag(challenge_id, flag)
        except Exception as exc:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Submit failed",
                    f"Could not submit flag: {str(exc)[:300]}",
                ),
                ephemeral=True,
            )
            return

        if result.correct:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Correct!",
                    description=result.message or "Flag accepted.",
                    color=discord.Color.green(),
                ),
                ephemeral=True,
            )
            member = interaction.user
            solver_ids = [member.id]
            await self.repo.mark_challenge_done(thread.id, solver_ids)
            if not thread.name.upper().startswith("[DONE]"):
                try:
                    await thread.edit(name=f"[DONE] {challenge.challenge_name}")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            solver_mention = member.mention
            await thread.send(
                embed=build_simple_embed(
                    "Challenge Solved!",
                    f"**{challenge.challenge_name}** solved by {solver_mention}!",
                ),
            )
        else:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Incorrect",
                    description=result.message or "Flag rejected.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

    # ── /challenge-sync ──────────────────────────────────────────────

    @app_commands.command(
        name="challenge-sync",
        description="Sync solve status from platform — auto-mark solved challenges as done",
    )
    @app_commands.describe(event_id="CTFtime event ID (auto-detected if single event)")
    @app_commands.default_permissions(administrator=True)
    async def challenge_sync(
        self,
        interaction: discord.Interaction,
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
                embed=build_simple_embed("Admin only", "Only admins can sync challenges."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        event = await self._resolve_event_for_fetch(interaction, event_id)
        if event is None:
            return

        config = await self.repo.get_platform_config(
            interaction.guild.id, event.ctftime_event_id,
        )
        if config is None:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No platform",
                    "Run `/ctf connect` first to link a platform.",
                ),
                ephemeral=True,
            )
            return

        adapter = create_adapter(config.platform_type, config.platform_url, config.team_token)

        try:
            solves = await adapter.get_team_solves()
        except Exception as exc:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Fetch failed",
                    f"Could not fetch solves: {str(exc)[:300]}",
                ),
                ephemeral=True,
            )
            return

        solved_ids: set[str] = set()
        for s in solves:
            if s.challenge_id:
                solved_ids.add(s.challenge_id)

        tracked = await self.repo.list_challenges(
            interaction.guild.id, event.ctftime_event_id,
        )

        synced: list[str] = []
        for chall in tracked:
            if chall.status == "done":
                continue
            pid = chall.platform_challenge_id
            if pid is None and chall.ctfd_challenge_id is not None:
                pid = str(chall.ctfd_challenge_id)
            if pid and pid in solved_ids:
                await self.repo.mark_challenge_done(chall.thread_id, [])
                synced.append(chall.challenge_name)
                thread = await self._get_thread(chall.thread_id)
                if thread is not None and not thread.name.upper().startswith("[DONE]"):
                    try:
                        await thread.edit(name=f"[DONE] {chall.challenge_name}")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                await asyncio.sleep(0.3)

        if synced:
            lines = "\n".join(f"- {n}" for n in synced[:20])
            if len(synced) > 20:
                lines += f"\n... and {len(synced) - 20} more"
            await interaction.followup.send(
                embed=build_simple_embed(
                    f"Synced {len(synced)} challenge(s)",
                    lines,
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Nothing to sync",
                    "All tracked challenges are already up to date.",
                ),
                ephemeral=True,
            )

    # ── /challenge-refresh ───────────────────────────────────────────

    @app_commands.command(
        name="challenge-refresh",
        description="Re-fetch challenge metadata from the platform and update embeds",
    )
    @app_commands.describe(event_id="CTFtime event ID (auto-detected if single event)")
    @app_commands.default_permissions(administrator=True)
    async def challenge_refresh(
        self,
        interaction: discord.Interaction,
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
                embed=build_simple_embed("Admin only", "Only admins can refresh challenges."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        event = await self._resolve_event_for_fetch(interaction, event_id)
        if event is None:
            return

        config = await self.repo.get_platform_config(
            interaction.guild.id, event.ctftime_event_id,
        )
        if config is None:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "No platform",
                    "Run `/ctf connect` first to link a platform.",
                ),
                ephemeral=True,
            )
            return

        adapter = create_adapter(config.platform_type, config.platform_url, config.team_token)

        try:
            fetched = await adapter.list_challenges()
        except Exception as exc:
            await interaction.followup.send(
                embed=build_simple_embed(
                    "Fetch failed",
                    f"Could not fetch challenges: {str(exc)[:300]}",
                ),
                ephemeral=True,
            )
            return

        fetched_by_id: dict[str, PlatformChallenge] = {pc.id: pc for pc in fetched}

        tracked = await self.repo.list_challenges(
            interaction.guild.id, event.ctftime_event_id,
        )
        category_mapping = config.category_mapping or {}

        updated: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for chall in tracked:
            pid = chall.platform_challenge_id
            if pid is None and chall.ctfd_challenge_id is not None:
                pid = str(chall.ctfd_challenge_id)
            if not pid or pid not in fetched_by_id:
                skipped.append(chall.challenge_name)
                continue

            pc = fetched_by_id[pid]
            category = _category_label(pc.category)
            topic = category_mapping.get(category) or _default_topic_for_category(category)

            try:
                outcome = await self._maybe_update_existing_platform_challenge(
                    event, pc, topic, chall, config.platform_type,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                failed.append(f"{chall.challenge_name}: {exc}")
                continue

            if outcome == "updated":
                updated.append(chall.challenge_name)
            else:
                skipped.append(chall.challenge_name)

            await asyncio.sleep(0.3)

        embed = discord.Embed(
            title=f"Challenge refresh — {event.event_title}",
            color=discord.Color.gold(),
        )
        embed.description = (
            f"Updated: `{len(updated)}` | "
            f"Unchanged: `{len(skipped)}` | "
            f"Failed: `{len(failed)}`"
        )
        if updated:
            embed.add_field(
                name="Updated",
                value=self._truncate(
                    self._summary_block(updated), _EMBED_FIELD_LIMIT,
                ),
                inline=False,
            )
        if failed:
            embed.add_field(
                name="Failed",
                value=self._truncate(
                    self._summary_block(failed), _EMBED_FIELD_LIMIT,
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Thread [ACTIVE] lifecycle ────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._scan_existing_threads()

    async def _scan_existing_threads(self) -> None:
        """On startup, scan all tracked open challenges and apply [ACTIVE] if the thread has activity."""
        for guild in self.bot.guilds:
            try:
                events = await self.repo.list_ctf_events(guild.id)
            except Exception:
                continue
            for event in events:
                try:
                    challenges = await self.repo.list_challenges(guild.id, event.ctftime_event_id)
                except Exception:
                    continue
                for chall in challenges:
                    if chall.status == "done":
                        self._active_renamed.add(chall.thread_id)
                        continue
                    thread = self.bot.get_channel(chall.thread_id)
                    if not isinstance(thread, discord.Thread):
                        continue
                    name = thread.name
                    if name.upper().startswith("[DONE]"):
                        self._active_renamed.add(chall.thread_id)
                        continue
                    if name.upper().startswith("[ACTIVE]"):
                        self._active_renamed.add(chall.thread_id)
                        continue
                    has_activity = thread.message_count is not None and thread.message_count > 1
                    if has_activity:
                        self._active_renamed.add(chall.thread_id)
                        try:
                            await thread.edit(name=f"[ACTIVE] {chall.challenge_name}")
                        except (discord.Forbidden, discord.HTTPException) as exc:
                            _log.debug("Startup rename failed for thread %s: %s", chall.thread_id, exc)
                        await asyncio.sleep(0.5)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        thread_id = thread.id

        if thread_id in self._active_renamed:
            return

        name = thread.name
        if name.upper().startswith("[ACTIVE]") or name.upper().startswith("[DONE]"):
            self._active_renamed.add(thread_id)
            return

        challenge = await self.repo.get_challenge_by_thread(thread_id)
        if challenge is None or challenge.status == "done":
            return

        self._active_renamed.add(thread_id)
        new_name = f"[ACTIVE] {challenge.challenge_name}"
        _log.info("Renaming thread %s -> %s", thread_id, new_name)
        try:
            await thread.edit(name=new_name)
        except (discord.Forbidden, discord.HTTPException) as exc:
            _log.warning("Could not rename thread %s to ACTIVE: %s", thread_id, exc)


async def setup(bot: commands.Bot) -> None:
    repo: Repository = bot.repo  # type: ignore[attr-defined]
    await bot.add_cog(ChallengeCog(bot, repo))
