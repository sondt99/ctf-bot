from __future__ import annotations

import re
import unicodedata

import discord

from bot.db.repository import Challenge
from bot.utils.embeds import build_simple_embed

_CHALLENGES_PAGE_SIZE = 10
_CHANNEL_SYNC_TIMEOUT_SECONDS = 300
_EMBED_FIELD_LIMIT = 1024
_DISCORD_CHANNEL_NAME_LIMIT = 100
_UNCATEGORIZED = "Uncategorized"


def truncate_text(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def category_label(category: str | None) -> str:
    label = " ".join((category or "").strip().split())
    return label or _UNCATEGORIZED


def channel_name_for_category(category: str | None) -> str:
    """Discord channel name for a platform category — one category, one channel.

    Discord silently rewrites names it dislikes, which would break the lookup
    back from category to channel, so the slug is built here instead: accents
    folded, everything outside ``[a-z0-9]`` collapsed to a single dash.
    """
    label = category_label(category)
    folded = unicodedata.normalize("NFKD", label)
    folded = folded.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.casefold()).strip("-")
    return (slug or "misc")[:_DISCORD_CHANNEL_NAME_LIMIT].rstrip("-")


def format_challenge_list_line(challenge: Challenge) -> str:
    status_icon = "✅" if challenge.status == "done" else "\U0001f513"
    thread_link = f"<#{challenge.thread_id}>"
    line = (
        f"{status_icon} **{challenge.challenge_name}** "
        f"[{challenge.category}] {thread_link}"
    )
    if challenge.status == "done":
        if challenge.solved_by:
            solvers = ", ".join(f"<@{uid}>" for uid in challenge.solved_by)
            line = f"{line}\n  by {solvers}"
        else:
            line = f"{line}\n  by *(unknown)*"
    return line


class ChallengesView(discord.ui.View):
    def __init__(
        self,
        challenges: list,
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


def _format_channel_list(names: list[str], empty: str) -> str:
    if not names:
        return empty
    text = " ".join(f"`#{name}`" for name in names)
    if len(text) <= _EMBED_FIELD_LIMIT:
        return text
    return text[: _EMBED_FIELD_LIMIT - 4].rstrip() + " ..."


class ChannelSyncView(discord.ui.View):
    """Confirm the channel plan before /challenge-fetch touches the server.

    Deleting a channel is permanent and takes its messages with it, so the
    admin sees the exact list — and which of those channels have been used —
    before anything runs.
    """

    def __init__(
        self,
        author_id: int,
        challenge_count: int,
        categories: list[str],
        to_create: list[str],
        to_delete: list[discord.TextChannel],
    ) -> None:
        super().__init__(timeout=_CHANNEL_SYNC_TIMEOUT_SECONDS)
        self.author_id = author_id
        self.challenge_count = challenge_count
        self.categories = categories
        self.to_create = to_create
        self.to_delete = to_delete
        self.confirmed = False
        self.cancelled = False
        self.timed_out = False
        self._message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            embed=build_simple_embed(
                "Not your import",
                "Only the admin who ran `/challenge-fetch` can confirm this.",
            ),
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.timed_out = True
        self.stop()
        if self._message is None:
            return
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[union-attr]
        try:
            await self._message.edit(
                embed=build_simple_embed(
                    "Timed out",
                    "Channel sync expired after 5 minutes. Nothing was changed. "
                    "Re-run `/challenge-fetch` to try again.",
                ),
                view=self,
            )
        except discord.NotFound:
            pass

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Sync topic channels",
            color=discord.Color.orange() if self.to_delete else discord.Color.gold(),
        )
        embed.description = (
            f"Fetched `{self.challenge_count}` challenges across "
            f"`{len(self.categories)}` categories.\n"
            "Each category gets its own channel."
        )
        embed.add_field(
            name=f"Create ({len(self.to_create)})",
            value=_format_channel_list(self.to_create, "Nothing to create."),
            inline=False,
        )

        used = [
            channel for channel in self.to_delete
            if channel.last_message_id is not None
        ]
        embed.add_field(
            name=f"Delete ({len(self.to_delete)})",
            value=_format_channel_list(
                [channel.name for channel in self.to_delete], "Nothing to delete."
            ),
            inline=False,
        )
        if used:
            embed.add_field(
                name=(
                    "\u26a0\ufe0f "
                    + (
                        "1 of those has messages"
                        if len(used) == 1
                        else f"{len(used)} of those have messages"
                    )
                ),
                value=_format_channel_list([channel.name for channel in used], ""),
                inline=False,
            )
        embed.set_footer(
            text="Deleting a channel is permanent and removes its messages."
            if self.to_delete
            else "account, general and scoreboard are never touched."
        )
        return embed

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        self.confirmed = True
        await interaction.response.edit_message(
            embed=build_simple_embed(
                "Importing challenges",
                "Syncing channels and creating threads now...",
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        self.cancelled = True
        await interaction.response.edit_message(
            embed=build_simple_embed(
                "Import cancelled",
                "No channels were changed and no threads were created.",
            ),
            view=None,
        )
        self.stop()
