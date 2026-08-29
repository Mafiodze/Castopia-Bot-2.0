# Castopia Bot

Three processes, one Railway service: Discord, Telegram, and FamiliarBot. Shared wiki client: `cogs/page_parsing.py`.

```
start.sh
  dsc/bot.py          → wiki search, no login
  tg/bot.py           → the same in Telegram
  familiarbot/bot.py  → sandbox moderation, FamiliarBot login
```

## Commands (Discord and Telegram)

`/search` `/fullsearch` `/autor` `/randompage` `/tags` `/help`  
On Discord the same commands also work with the `.` prefix.

Card layout (except `/tags` and `/help`): title → first sentence of the article (no rating widget) → author → rating → tags → last edit. CC BY-SA 3.0 appears once: Discord footer, Telegram bottom.

On Discord, a `castopia.site/...` link posts an article card **under that message**. Discord's grey site preview is not attached. Needs Manage Webhooks and Manage Messages. Forum and system channels are ignored.

## FamiliarBot

Only `sandbox:` URLs. Does not move pages into mainspace. No WIP. No JSONL.

| | condition | delay | action |
|---|---|---|---|
| delete | rating **< 3.0**, votes **≥ 4** | 1 day | `sandbox:` → `deleted:` + `статус:удалено` + post in [t-100](https://castopia.site/forum/t-100/zurnal-udalenii) |
| gray zone | **3.0 ≤ rating < 4.0**, votes **≥ 4** | 30 days | same deletion |
| passed | rating **≥ 4.0**, votes **≥ 4** | 1 week | tag `котел:к_переносу`, URL unchanged |
| too few votes | < 4 | — | `статус:проверка` only |

Tags: `статус:проверка` on every sandbox page; `котел:к_удалению` / `котел:к_тегованию` / `котел:рейтинг_набран` / `котел:к_переносу`. If the rating recovers, the tag is removed.

After tag changes, a "System notification" is posted in the article discussion. All tags are stripped from `draft:`. Untouched: `основное_пространство`, `архив`, `удалено`, `18+`, `гайд`, `компонент`, `навигация`, `поиск`, `системный`, `структура_сайта`.

Cycle every 10 minutes. `FAMILIARBOT_DRY_RUN=true` logs only.

## Railway

Variables: `DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, `WIKI_USERNAME`, `WIKI_PASSWORD`. Optional: `DISCORD_GUILD_ID`, `WIKI_USER_AGENT` (`CastopiaBot/2.0 (+https://castopia.site)` — Anubis allowlist), `WIKI_ANUBIS_COOKIE`, `FAMILIARBOT_ENABLED`, `FAMILIARBOT_DRY_RUN`, `LOG_LEVEL`.

Thresholds live in code (`familiarbot/config.py`), not env. Password stays in Railway, never in git.

Build: Dockerfile at the repo root. Start: `./start.sh`. Replicas: 1. Locally: copy `.env.example` → `.env`.

```
python -m unittest discover -s tests -q
```

FamiliarBot account needs: editor, tags, rename into `deleted:`, posts in the article discussion and in t-100.
