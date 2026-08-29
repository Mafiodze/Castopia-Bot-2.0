"""Cerberus-style forum and discussion copy, adapted to Castopia tags."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from cogs.page_parsing import Article


POST_TITLE = "Системное уведомление"
JOURNAL_TITLE = "Re: Журнал удалений"

_DELETION_COMMON = "Критический рейтинг набран, статья будет удалена завтра."
_DELETION_EASTER = (
    (0.2, "Критический рейтинг набран, статья будет съедена завтра."),
    (0.1, "Критический рейтинг набран, статья будет передана в Отдел удалений в течение суток."),
    (0.1, "Критический рейтинг набран, статья будет отправлена в пространство между мирами завтра."),
    (0.1, "Критический рейтинг набран, статья будет удалена из согласованной нормальности завтра."),
    (0.05, "Критический рейтинг набран, вы ещё можете удалить статью самостоятельно."),
)

GRAYZONE = (
    "Популярность спустя месяц с момента публикации: {popularity}% от {votes}.\n"
    "В соответствии с правилами публикации, раздел «Автору», статья переносится в Удалённые."
)
TOO_LONG = (
    "Статья слишком долго находится на проверке.\n"
    "В соответствии с правилами публикации, раздел «Автору», применяются меры "
    "ускорения проверки: статья помечена к тегованию и при отсутствии сдвига "
    "рейтинга будет перенесена в Удалённые."
)
TAGS_PROHIBITED = (
    "В данной категории запрещена простановка тегов, все установленные теги были удалены."
)
WHITMARK = (
    "Проходной рейтинг набран. Статья ожидает переноса в основное пространство."
)
TRANSFER_READY = (
    "Выдержан срок белой метки. Статья помечена к переносу в основное пространство. "
    "Перенос выполняют администраторы."
)
UNMARK = "Тег {tag} снят: рейтинг изменился."
TAG_ADDED = "Добавлен тег {tag}."
TAG_REMOVED = "Снят тег {tag}."


def deletion_phrase(now: datetime) -> str:
    roll = random.random()
    cursor = 0.0
    for weight, text in _DELETION_EASTER:
        cursor += weight
        if roll < cursor:
            return text.format(next_day=(now + timedelta(days=1)).strftime("%d.%m.%Y"))
    return _DELETION_COMMON


def grayzone_phrase(article: Article) -> str:
    return GRAYZONE.format(
        popularity=article.popularity if article.popularity is not None else 0,
        votes=article.votes_count if article.votes_count is not None else 0,
    )


def tag_change_phrase(added: set[str], removed: set[str]) -> str:
    lines: list[str] = []
    if added:
        lines.append(
            "Добавлены теги: " + ", ".join(f"**{tag}**" for tag in sorted(added))
        )
    if removed:
        lines.append(
            "Сняты теги: " + ", ".join(f"**{tag}**" for tag in sorted(removed))
        )
    return "\n".join(lines) if lines else "Теги страницы обновлены."


def discussion_report(
    reason: str | None,
    added: set[str],
    removed: set[str],
) -> str:
    blocks = [reason.strip()] if reason and reason.strip() else []
    change = tag_change_phrase(added, removed)
    if change:
        blocks.append(change)
    return "\n\n".join(blocks)


def journal_body(prepend: str, pages: list[Article]) -> str:
    lines = [prepend]
    for page in pages:
        tags = ", ".join(
            f"**{tag.replace(':', ':**', 1)}" if ":" in tag else tag
            for tag in sorted(page.tags)
        )
        rating = page.rating if page.rating is not None else "—"
        votes = page.votes_count if page.votes_count is not None else "—"
        popularity = page.popularity if page.popularity is not None else "—"
        author = page.author or "неизвестен"
        lines.append(
            f"* [[[{page.page_id}|{page.title}]]] - {rating} ({votes}) / {popularity}% _\n"
            f"  Автор: [[user {author}]] _\n"
            f"  Теги: {tags}"
        )
    return "\n".join(lines)


JOURNAL_CRITICAL = "Удалено по достижении критического рейтинга:"
JOURNAL_GRAY = "Удалено из серой зоны:"
JOURNAL_STALE = "Удалено по истечении срока проверки:"
