"""Text presentation helpers shared by the Telegram and Discord adapters."""

from __future__ import annotations

import html
import re
from typing import Any

from .constants import FOOTER_TEXT

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_WHITESPACE_RE = re.compile(r"\s+")
_DISCORD_MARKDOWN_RE = re.compile(r"([\\`*_{}\[\]<>])")
_RATING_LEAD_RE = re.compile(
    r"^(?:[★⭐]+\s*)?(?:рейтинг\s*:\s*)?(?:[—–−-]|[\d]+(?:[.,]\d+)?)"
    r"\s+\d+\s*/\s*\d+\s*%\s*",
    re.IGNORECASE,
)


def strip_rating_lead(text: str) -> str:
    """Drop the rate-module prefix so the excerpt starts on page prose."""
    cleaned = _WHITESPACE_RE.sub(" ", text).strip()
    return _RATING_LEAD_RE.sub("", cleaned, count=1).strip()


def excerpt(text: str, query: str, *, limit: int = 280) -> str:
    """Return a readable excerpt near the first sentence matching the query."""
    if limit <= 1:
        raise ValueError("limit must be greater than 1")

    normalized_text = strip_rating_lead(text)
    if not normalized_text:
        return "Описание на странице не найдено."

    needle = query.casefold().strip()
    sentences = _SENTENCE_SPLIT_RE.split(normalized_text)
    selected = next(
        (
            sentence
            for sentence in sentences
            if needle and needle in sentence.casefold()
        ),
        sentences[0],
    )

    if len(selected) <= limit:
        return selected

    clipped = selected[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;: ")
    return f"{clipped}…"


def highlight_html(text: str, query: str) -> str:
    """Escape Telegram HTML and safely highlight each query term."""
    escaped = html.escape(text)
    words = [word for word in query.split() if word]
    if not words:
        return escaped

    escaped_words = [html.escape(word) for word in words]
    pattern = re.compile(
        "|".join(re.escape(word) for word in escaped_words),
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: f"<b>{match.group(0)}</b>",
        escaped,
    )


def escape_discord(text: str) -> str:
    """Escape Markdown-significant characters before putting text in an embed."""
    return _DISCORD_MARKDOWN_RE.sub(r"\\\1", text)


def display_tags(tags: Any) -> str:
    """Render wiki tags as a short comma-separated list."""
    values = [str(tag) for tag in (tags or []) if str(tag).strip()]
    if not values:
        return "—"
    return ", ".join(sorted(values))


def format_rating(article: Any) -> str:
    rating = getattr(article, "rating", None)
    votes = getattr(article, "votes_count", None)
    popularity = getattr(article, "popularity", None)
    if isinstance(rating, (int, float)):
        rating_s = f"{float(rating):.1f}"
    else:
        rating_s = "—"
    votes_s = "0" if votes is None else str(votes)
    pop_s = "0" if popularity is None else str(popularity)
    return f"рейтинг: {rating_s} {votes_s} / {pop_s} %"


def article_meta_lines(
    article: Any,
    *,
    include_tags: bool = True,
    include_license: bool = False,
) -> list[str]:
    """Author, rating, tags, last edit. License only when asked (Telegram cards)."""
    lines = [
        f"Автор: {getattr(article, 'author', '') or 'неизвестен'}",
        format_rating(article),
    ]
    if include_tags:
        lines.append(f"Теги: {display_tags(getattr(article, 'tags', ()))}")
    lines.append(
        "последнее изменение: "
        f"{getattr(article, 'last_edit', '') or 'неизвестно'}"
    )
    if include_license:
        lines.append(FOOTER_TEXT)
    return lines


def article_card_text(
    article: Any,
    query: str = "",
    *,
    include_license: bool = False,
    preview_limit: int = 280,
) -> str:
    """Title is rendered by the adapter. Body: excerpt, then meta."""
    preview = excerpt(article.text, query, limit=preview_limit)
    meta = "\n".join(
        article_meta_lines(article, include_license=include_license)
    )
    return f"{preview}\n\n{meta}"
