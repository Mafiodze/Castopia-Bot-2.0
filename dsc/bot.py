"""Entrypoint for the Discord bot."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


class CastopiaBot(commands.Bot):
    async def setup_hook(self) -> None:
        """Load hybrid commands and publish them to Discord."""
        await self.load_extension("cogs.dsc")
        raw_guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if raw_guild_id:
            try:
                guild_id = int(raw_guild_id)
            except ValueError as exc:
                raise RuntimeError("DISCORD_GUILD_ID must be a numeric Discord guild ID") from exc
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.getLogger(__name__).info(
                "Synced %s application commands to development guild %s.", len(synced), guild_id
            )
        else:
            synced = await self.tree.sync()
            logging.getLogger(__name__).info(
                "Synced %s global application commands.", len(synced)
            )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing in .env")
    return value


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    token = _required_env("DISCORD_BOT_TOKEN")
    intents = discord.Intents.default()
    # Prefix commands require Message Content Intent to be enabled in the Discord portal.
    intents.message_content = True
    bot = CastopiaBot(
        command_prefix=".",
        intents=intents,
        help_command=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f"Configuration error: {error}") from error
