from __future__ import annotations

import discord

from bot.utils.embeds import build_events_page_embed


class CtfPaginationView(discord.ui.View):
    def __init__(
        self,
        events: list[dict],
        author_id: int,
        page_size: int = 3,
        timeout: int = 180,
        title: str = "Upcoming CTFs",
    ):
        super().__init__(timeout=timeout)
        self.events = events
        self.author_id = author_id
        self.page_size = page_size
        self.page = 0
        self.message: discord.Message | None = None
        self.title = title

    def build_page_payload(self) -> list[discord.Embed]:
        embed = build_events_page_embed(self.events, self.page, self.page_size, self.title)
        return [embed]

    async def _update(self, interaction: discord.Interaction) -> None:
        embeds = self.build_page_payload()
        await interaction.response.edit_message(embeds=embeds, view=self)

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
        max_page = max(0, (len(self.events) - 1) // self.page_size)
        if self.page < max_page:
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
