"""Entrypoint for the Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from cogs.constants import ConfigurationError, load_wiki_config  # noqa: E402
from cogs.page_parsing import WikiClient  # noqa: E402
from cogs.tg import create_router  # noqa: E402

PUBLIC_READER_CONCURRENCY = 8


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing in .env")
    return value


async def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    token = _required_env("TELEGRAM_BOT_TOKEN")
    config = load_wiki_config()
    wiki = WikiClient(config, reader_concurrency=PUBLIC_READER_CONCURRENCY)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(wiki))

    await wiki.start()
    warmup = asyncio.create_task(wiki.warmup_public_index())
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        warmup.cancel()
        await wiki.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except (ConfigurationError, RuntimeError) as error:
        raise SystemExit(f"Configuration error: {error}") from error
