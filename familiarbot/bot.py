"""FamiliarBot entrypoint — third process next to Discord and Telegram."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from cogs.constants import ConfigurationError, load_wiki_config  # noqa: E402
from cogs.page_parsing import WikiClient, WikiError  # noqa: E402

from familiarbot.config import load_familiar_config  # noqa: E402
from familiarbot.moderator import Moderator  # noqa: E402


async def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    familiar = load_familiar_config()
    if not familiar.enabled:
        logging.getLogger(__name__).info("FamiliarBot disabled")
        return

    config = load_wiki_config()
    wiki = WikiClient(config)
    await wiki.start()
    try:
        try:
            user = await wiki.login()
        except WikiError as error:
            logging.getLogger(__name__).error(
                "familiarbot_login_failed error=%s",
                error,
            )
            raise SystemExit(f"FamiliarBot did not log in: {error}") from error
        logging.getLogger(__name__).info(
            "familiarbot_online user=%s dry_run=%s period_s=%s",
            user,
            familiar.dry_run,
            int(familiar.work_period.total_seconds()),
        )
        moderator = Moderator(wiki, familiar)
        while True:
            try:
                await moderator.run_cycle()
                logging.getLogger(__name__).info(
                    "familiarbot_cycle_ok dry_run=%s",
                    familiar.dry_run,
                )
            except Exception:
                logging.getLogger(__name__).exception("familiarbot_cycle_failed")
            await asyncio.sleep(familiar.work_period.total_seconds())
    finally:
        await wiki.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except (ConfigurationError, WikiError, RuntimeError) as error:
        logging.getLogger("familiarbot").error(
            "familiarbot_login_failed error=%s",
            error,
        )
        raise SystemExit(f"FamiliarBot did not log in: {error}") from error
