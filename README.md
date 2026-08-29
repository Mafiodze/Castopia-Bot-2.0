# Castopia Bot

Три процесса, один Railway-сервис: Discord, Telegram и FamiliarBot. Общий клиент вики — `cogs/page_parsing.py`.

```
start.sh
  dsc/bot.py          → поиск по вики, без логина
  tg/bot.py           → то же в Telegram
  familiarbot/bot.py  → модерация котла, логин FamiliarBot
```

## Команды (Discord и Telegram)

`/search` `/fullsearch` `/autor` `/randompage` `/tags` `/help`  
В Discord те же команды с префиксом `.`

Карточка (кроме `/tags` и `/help`): заголовок → первое предложение статьи (без виджета рейтинга) → автор → рейтинг → теги → последнее изменение. Лицензия CC BY-SA 3.0 один раз: в Discord в футере, в Telegram внизу.

В Discord ссылка на `castopia.site/...` в обычном сообщении даёт ту же карточку автоматически (не форум и не system).

## FamiliarBot

Только URL с `sandbox:`. Не переносит в основное пространство. Нет WIP. Нет JSONL.

| | условие | срок | действие |
|---|---|---|---|
| удаление | рейтинг **< 3.0**, голосов **≥ 4** | сутки | `sandbox:` → `deleted:` + `статус:удалено` + пост в [t-100](https://castopia.site/forum/t-100/zurnal-udalenii) |
| серая зона | **3.0 ≤ рейтинг < 4.0**, голосов **≥ 4** | 30 дней | то же удаление |
| набран | рейтинг **≥ 4.0**, голосов **≥ 4** | неделя | тег `котел:к_переносу`, URL не трогает |
| мало голосов | < 4 | — | только `статус:проверка` |

Теги: `статус:проверка` всем в котле; `котел:к_удалению` / `котел:к_тегованию` / `котел:рейтинг_набран` / `котел:к_переносу`. Если рейтинг отыграл — тег снимается.

После смены тегов — «Системное уведомление» в обсуждении статьи. С `draft:` снимаются все теги. Не трогает: `основное_пространство`, `архив`, `удалено`, `18+`, `гайд`, `компонент`, `навигация`, `поиск`, `системный`, `структура_сайта`.

Цикл каждые 10 минут. `FAMILIARBOT_DRY_RUN=true` — только логи.

## Railway

Переменные: `DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, `WIKI_USERNAME`, `WIKI_PASSWORD`. По желанию: `DISCORD_GUILD_ID`, `WIKI_USER_AGENT` (`CastopiaBot/2.0 (+https://castopia.site)` — в allowlist Anubis), `WIKI_ANUBIS_COOKIE`, `FAMILIARBOT_ENABLED`, `FAMILIARBOT_DRY_RUN`, `LOG_LEVEL`.

Пороги в коде (`familiarbot/config.py`), не в env. Пароль только в Railway, не в git.

Сборка: Dockerfile в корне. Старт: `./start.sh`. Реплика: 1. Локально: скопировать `.env.example` → `.env`.

```
python -m unittest discover -s tests -q
```

Права аккаунта FamiliarBot: editor, теги, переименование в `deleted:`, посты в обсуждении статьи и в t-100.
