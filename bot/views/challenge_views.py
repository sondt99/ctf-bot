from __future__ import annotations

import discord

from bot.db.repository import Challenge
from bot.utils.embeds import build_simple_embed

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
_CHALLENGES_PAGE_SIZE = 10
_CATEGORY_PAGE_SIZE = 4
_CATEGORY_MAPPING_TIMEOUT_SECONDS = 300
_UNCATEGORIZED = "Uncategorized"


def truncate_text(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def category_label(category: str | None) -> str:
    label = " ".join((category or "").strip().split())
    return label or _UNCATEGORIZED


def default_topic_for_category(category: str) -> str:
    key = category.replace("_", " ").replace("-", " ")
    key = " ".join(key.split()).casefold()
    return _CATEGORY_DEFAULTS.get(key, "MISC")


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
            placeholder=truncate_text(f"{category} -> {current_topic}", 150),
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
            category: default_topic_for_category(category) for category in categories
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
