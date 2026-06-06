import asyncio
import logging

import discord
from discord.ext import commands

from bot.config import DATABASE_PATH, DISCORD_GUILD_ID, DISCORD_TOKEN
from bot.db.database import init_db
from bot.db.repository import Repository


logging.basicConfig(level=logging.INFO)


class CtfBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        super().__init__(command_prefix="!", intents=intents)
        self.repo = Repository(DATABASE_PATH)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        await self.process_commands(message)

    async def setup_hook(self) -> None:
        await init_db(DATABASE_PATH)
        await self.load_extension("bot.cogs.ctf")
        await self.load_extension("bot.cogs.challenge")
        await self.load_extension("bot.cogs.scoreboard_cog")
        await self.load_extension("bot.cogs.audit")
        await self.load_extension("bot.cogs.stats")

        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                logging.info("Synced commands to guild %s", DISCORD_GUILD_ID)
                return
            except (discord.Forbidden, discord.HTTPException) as exc:
                logging.warning(
                    "Guild sync failed (%s). Falling back to global sync.", exc
                )

        await self.tree.sync()


async def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot = CtfBot()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
