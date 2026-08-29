"""Telegram adapter for the public Castopia wiki client."""

from __future__ import annotations

import html
import logging
import secrets
from dataclasses import dataclass
from time import monotonic

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .page_parsing import (
    Article,
    UpstreamAccessError,
    UpstreamContentError,
    UpstreamNotFoundError,
    UpstreamUnavailableError,
    WikiClient,
    WikiError,
)
from .txt_processing import excerpt, highlight_html, article_meta_lines

logger = logging.getLogger(__name__)

RESULTS_PER_PAGE = 5
SEARCH_TTL_SECONDS = 10 * 60
PENDING_INPUT_TTL_SECONDS = 5 * 60
MAX_QUERY_LENGTH = 160
MAX_TAGS = 5
MAX_TAG_RESULTS = 30
MAX_MESSAGE_LENGTH = 4096


@dataclass(slots=True)
class _SearchState:
    owner_id: int
    results: list[Article]
    query: str
    expires_at: float
    heading: str = "Результаты поиска"
    match_excerpt: bool = True

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


@dataclass(slots=True)
class _PendingInput:
    owner_id: int
    chat_id: int
    action: str
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


class _SearchStore:
    """Short-lived in-memory storage for Telegram pagination state."""

    def __init__(self) -> None:
        self._items: dict[str, _SearchState] = {}

    def save(
        self,
        owner_id: int,
        results: list[Article],
        query: str,
        *,
        heading: str = "Результаты поиска",
        match_excerpt: bool = True,
    ) -> str:
        self._prune()

        token = secrets.token_urlsafe(6)
        self._items[token] = _SearchState(
            owner_id=owner_id,
            results=results,
            query=query,
            expires_at=monotonic() + SEARCH_TTL_SECONDS,
            heading=heading,
            match_excerpt=match_excerpt,
        )
        return token

    def get(self, token: str) -> _SearchState | None:
        state = self._items.get(token)

        if state is None:
            return None

        if state.is_expired(monotonic()):
            self._items.pop(token, None)
            return None

        return state

    def _prune(self) -> None:
        now = monotonic()
        expired_tokens = [
            token
            for token, state in self._items.items()
            if state.is_expired(now)
        ]

        for token in expired_tokens:
            self._items.pop(token, None)


class _PendingInputStore:
    """Per-user, per-chat short-lived state for interactive commands."""

    def __init__(self) -> None:
        self._items: dict[tuple[int, int], _PendingInput] = {}

    def set(
        self,
        owner_id: int,
        chat_id: int,
        action: str,
    ) -> None:
        self._prune()

        self._items[(owner_id, chat_id)] = _PendingInput(
            owner_id=owner_id,
            chat_id=chat_id,
            action=action,
            expires_at=monotonic() + PENDING_INPUT_TTL_SECONDS,
        )

    def get(
        self,
        owner_id: int,
        chat_id: int,
    ) -> _PendingInput | None:
        key = (owner_id, chat_id)
        state = self._items.get(key)

        if state is None:
            return None

        if state.is_expired(monotonic()):
            self._items.pop(key, None)
            return None

        return state

    def clear(
        self,
        owner_id: int,
        chat_id: int,
    ) -> None:
        self._items.pop((owner_id, chat_id), None)

    def _prune(self) -> None:
        now = monotonic()
        expired_keys = [
            key
            for key, state in self._items.items()
            if state.is_expired(now)
        ]

        for key in expired_keys:
            self._items.pop(key, None)


def _argument(message: types.Message) -> str:
    """Return the text after the Telegram command."""
    return (message.text or "").partition(" ")[2].strip()


def _normalise_query(value: str) -> str:
    """Normalize user-entered text without changing its meaning."""
    return " ".join(value.split()).strip()


def _article_message(
    article: Article,
    query: str = "",
) -> str:
    """Render one article preview using Telegram-safe HTML."""
    preview = excerpt(article.text, "", limit=700)
    meta = "\n".join(
        html.escape(line)
        for line in article_meta_lines(article, include_license=True)
    )
    return (
        f"<b>{html.escape(article.title)}</b>\n"
        f"{highlight_html(preview, query)}\n\n"
        f"{meta}"
    )


def _article_keyboard(article: Article) -> InlineKeyboardMarkup:
    """Build the button linking to an article."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть статью",
                    url=article.url,
                )
            ]
        ]
    )


def _search_keyboard(
    token: str,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup | None:
    """Build pagination controls for a search result page."""
    if total_pages <= 1:
        return None

    buttons: list[InlineKeyboardButton] = []

    if page > 1:
        buttons.append(
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"s:{token}:{page - 1}",
            )
        )

    if page < total_pages:
        buttons.append(
            InlineKeyboardButton(
                text="Вперёд →",
                callback_data=f"s:{token}:{page + 1}",
            )
        )

    if not buttons:
        return None

    return InlineKeyboardMarkup(
        inline_keyboard=[buttons]
    )


def _render_search_page(
    results: list[Article],
    query: str,
    page: int,
    *,
    heading: str | None = None,
    match_excerpt: bool = True,
) -> str:
    """Render one bounded full-text search result page."""
    total_pages = max(
        1,
        (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE,
    )
    page = max(1, min(page, total_pages))
    first = (page - 1) * RESULTS_PER_PAGE
    title = heading or "Результаты поиска"
    needle = query if match_excerpt else ""

    lines = [
        f"<b>{html.escape(title)} — {page}/{total_pages}</b>"
    ]

    for article in results[first : first + RESULTS_PER_PAGE]:
        article_title = html.escape(article.title)
        url = html.escape(article.url, quote=True)
        snippet = highlight_html(
            excerpt(article.text, needle, limit=500),
            query,
        )
        meta = "\n".join(
            html.escape(line)
            for line in article_meta_lines(article, include_tags=True)
        )

        lines.append(
            f'• <a href="{url}">{article_title}</a>\n{snippet}\n{meta}'
        )

    return "\n\n".join(lines)[:MAX_MESSAGE_LENGTH]


def _wiki_error_text(error: Exception) -> str:
    """Map WikiClient exceptions to user-facing Telegram text."""
    if isinstance(error, UpstreamAccessError):
        return (
            "Источник запретил автоматический доступ. Для него нужен "
            "официальный API или разрешение владельца сайта."
        )

    if isinstance(error, UpstreamContentError):
        return (
            "Структура источника изменилась. "
            "Администратор уже получил диагностическую запись."
        )

    if isinstance(error, UpstreamNotFoundError):
        return "Статья больше не существует в источнике."

    if isinstance(error, UpstreamUnavailableError):
        return (
            "Источник временно недоступен. "
            "Попробуйте немного позже."
        )

    if isinstance(error, WikiError):
        return (
            "Не удалось получить данные из источника. "
            "Попробуйте позже."
        )

    return "Не удалось обработать команду. Попробуйте ещё раз позже."


async def _report_wiki_error(
    message: types.Message,
    error: Exception,
) -> None:
    """Log an error and send a safe user-facing message."""
    text = _wiki_error_text(error)

    if isinstance(error, UpstreamContentError):
        logger.error(
            "telegram_wiki_content_error error=%s",
            type(error).__name__,
        )
    elif isinstance(error, WikiError):
        logger.warning(
            "telegram_wiki_error error=%s",
            type(error).__name__,
        )
    else:
        logger.exception(
            "telegram_command_unexpected_error error=%s",
            type(error).__name__,
        )

    try:
        await message.answer(text)
    except Exception:
        logger.exception("telegram_error_message_failed")


def _is_valid_query(query: str) -> bool:
    return bool(query) and len(query) <= MAX_QUERY_LENGTH


async def _send_typing_action(message: types.Message) -> None:
    """Show Telegram's typing state without making it a hard dependency."""
    try:
        await message.bot.send_chat_action(
            message.chat.id,
            "typing",
        )
    except Exception:
        logger.debug(
            "telegram_chat_action_failed",
            exc_info=True,
        )


async def _safe_edit_message(
    message: types.Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit a Telegram message without exposing Telegram API exceptions."""
    await message.edit_text(
        text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


def create_router(wiki: WikiClient) -> Router:
    """Create a router bound to one shared, long-lived WikiClient."""
    router = Router(name="castopia")
    searches = _SearchStore()
    pending_inputs = _PendingInputStore()

    async def request_pending_input(
        message: types.Message,
        action: str,
        prompt: str,
    ) -> None:
        """Enter interactive input mode for the current user and chat."""
        if message.from_user is None:
            return

        pending_inputs.set(
            owner_id=message.from_user.id,
            chat_id=message.chat.id,
            action=action,
        )

        await message.answer(
            f"{prompt}\n\n"
            "Для отмены используйте /cancel."
        )

    async def execute_search(
        message: types.Message,
        query: str,
    ) -> None:
        query = _normalise_query(query)

        if not _is_valid_query(query):
            await message.answer(
                f"Запрос должен быть от 1 до "
                f"{MAX_QUERY_LENGTH} символов."
            )
            return

        started_at = monotonic()

        try:
            await _send_typing_action(message)

            article = await wiki.find_by_title(query)

            if article is None:
                await message.answer(
                    f"Статья «{html.escape(query)}» не найдена."
                )
                return

            await message.answer(
                _article_message(article, query),
                reply_markup=_article_keyboard(article),
            )
        except Exception as error:
            await _report_wiki_error(message, error)
        finally:
            logger.info(
                "telegram_command command=search "
                "query_length=%s duration_ms=%s",
                len(query),
                round(
                    (monotonic() - started_at) * 1000
                ),
            )

    async def execute_tags(
        message: types.Message,
        raw_tags: str,
    ) -> None:
        tags = _normalise_query(raw_tags).split()

        if not tags:
            await message.answer(
                "Укажите хотя бы один тег."
            )
            return

        if len(tags) > MAX_TAGS:
            await message.answer(
                f"Можно указать не более {MAX_TAGS} тегов."
            )
            return

        if any(
            len(tag) > MAX_QUERY_LENGTH
            for tag in tags
        ):
            await message.answer(
                "Один из тегов слишком длинный."
            )
            return

        started_at = monotonic()
        articles: list[Article] = []

        try:
            await _send_typing_action(message)

            articles = await wiki.find_by_tags(tags)

            if not articles:
                await message.answer(
                    "По этим тегам ничего не найдено."
                )
                return

            lines = [
                "<b>Статьи с тегами: "
                f"{html.escape(', '.join(tags))}</b>"
            ]

            for article in articles[:MAX_TAG_RESULTS]:
                title = html.escape(article.title)
                url = html.escape(
                    article.url,
                    quote=True,
                )
                lines.append(
                    f'• <a href="{url}">{title}</a>'
                )

            await message.answer(
                "\n".join(lines)[:MAX_MESSAGE_LENGTH],
                disable_web_page_preview=True,
            )
        except Exception as error:
            await _report_wiki_error(message, error)
        finally:
            logger.info(
                "telegram_command command=tags "
                "tags_count=%s result_count=%s duration_ms=%s",
                len(tags),
                len(articles),
                round(
                    (monotonic() - started_at) * 1000
                ),
            )

    async def execute_fullsearch(
        message: types.Message,
        query: str,
    ) -> None:
        query = _normalise_query(query)

        if not _is_valid_query(query):
            await message.answer(
                f"Запрос должен быть от 1 до "
                f"{MAX_QUERY_LENGTH} символов."
            )
            return

        started_at = monotonic()
        results: list[Article] = []

        try:
            await _send_typing_action(message)

            results = await wiki.search_content(query)

            if not results:
                await message.answer(
                    f"По запросу «{html.escape(query)}» "
                    "ничего не найдено."
                )
                return

            if message.from_user is None:
                await message.answer(
                    "Не удалось определить пользователя."
                )
                return

            token = searches.save(
                message.from_user.id,
                results,
                query,
            )

            total_pages = max(
                1,
                (
                    len(results)
                    + RESULTS_PER_PAGE
                    - 1
                )
                // RESULTS_PER_PAGE,
            )

            await message.answer(
                _render_search_page(
                    results,
                    query,
                    1,
                ),
                reply_markup=_search_keyboard(
                    token,
                    1,
                    total_pages,
                ),
                disable_web_page_preview=True,
            )
        except Exception as error:
            await _report_wiki_error(message, error)
        finally:
            logger.info(
                "telegram_command command=fullsearch "
                "query_length=%s result_count=%s duration_ms=%s",
                len(query),
                len(results),
                round(
                    (monotonic() - started_at) * 1000
                ),
            )

    async def execute_author(
        message: types.Message,
        query: str,
    ) -> None:
        query = _normalise_query(query)

        if not _is_valid_query(query):
            await message.answer(
                f"Запрос должен быть от 1 до "
                f"{MAX_QUERY_LENGTH} символов."
            )
            return

        started_at = monotonic()
        results: list[Article] = []

        try:
            await _send_typing_action(message)
            results = await wiki.find_by_author(query)

            if not results:
                await message.answer(
                    f"Автор «{html.escape(query)}» не найден."
                )
                return

            if message.from_user is None:
                await message.answer(
                    "Не удалось определить пользователя."
                )
                return

            token = searches.save(
                message.from_user.id,
                results,
                query,
                heading=f"Статьи автора: {query}",
                match_excerpt=False,
            )
            total_pages = max(
                1,
                (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE,
            )
            await message.answer(
                _render_search_page(
                    results,
                    query,
                    1,
                    heading=f"Статьи автора: {query}",
                    match_excerpt=False,
                ),
                reply_markup=_search_keyboard(token, 1, total_pages),
                disable_web_page_preview=True,
            )
        except Exception as error:
            await _report_wiki_error(message, error)
        finally:
            logger.info(
                "telegram_command command=autor "
                "query_length=%s result_count=%s duration_ms=%s",
                len(query),
                len(results),
                round((monotonic() - started_at) * 1000),
            )

    @router.message(Command("start"))
    async def start_command(message: types.Message) -> None:
        await message.answer(
            "<b>Castopia Bot</b>\n\n"
            "Поиск по публичной Castopia Wiki.\n\n"
            "<b>Основные команды</b>\n"
            "<code>/search</code> — найти статью по названию\n"
            "<code>/tags</code> — найти статьи по тегам\n"
            "<code>/fullsearch</code> — поиск по содержимому\n"
            "<code>/autor</code> — статьи автора\n"
            "<code>/randompage</code> — случайная статья\n"
            "<code>/help</code> — помощь\n"
            "<code>/cancel</code> — отменить текущий ввод\n\n"
            "<b>Можно использовать два режима.</b>\n\n"
            "Сразу указать запрос:\n"
            "<code>/search SCP-173</code>\n\n"
            "Или выбрать команду из меню:\n"
            "<code>/search</code>\n"
            "и бот попросит ввести название статьи."
        )

    @router.message(Command("help"))
    async def help_command(message: types.Message) -> None:
        await message.answer(
            "<b>Команды</b>\n\n"
            "<code>/search название</code>\n"
            "Поиск статьи по названию.\n"
            "Можно отправить просто <code>/search</code>, "
            "после чего бот попросит название.\n\n"
            "<code>/tags тег1 тег2</code>\n"
            "Поиск по тегам. Можно использовать пошаговый режим.\n\n"
            "<code>/fullsearch текст</code>\n"
            "Полнотекстовый поиск. Можно использовать пошаговый режим.\n\n"
            "<code>/autor ник</code>\n"
            "Статьи автора. Можно использовать пошаговый режим.\n\n"
            "<code>/randompage</code>\n"
            "Случайная публичная статья.\n\n"
            "<code>/cancel</code>\n"
            "Отменить ожидаемый ввод.\n\n"
            "Бот работает только с публичным содержимым "
            "и не обходит ограничения источника."
        )

    @router.message(Command("cancel"))
    async def cancel_command(message: types.Message) -> None:
        if message.from_user is None:
            return

        state = pending_inputs.get(
            message.from_user.id,
            message.chat.id,
        )

        pending_inputs.clear(
            message.from_user.id,
            message.chat.id,
        )

        if state is None:
            await message.answer(
                "Сейчас нет активного ввода для отмены."
            )
            return

        await message.answer(
            "Текущий ввод отменён."
        )

    @router.message(Command("randompage"))
    async def random_page(message: types.Message) -> None:
        if message.from_user is not None:
            pending_inputs.clear(
                message.from_user.id,
                message.chat.id,
            )

        started_at = monotonic()

        try:
            await _send_typing_action(message)

            article = await wiki.random_article()

            if article is None:
                await message.answer(
                    "Не нашёл подходящую публичную статью."
                )
                return

            await message.answer(
                _article_message(article),
                reply_markup=_article_keyboard(article),
            )
        except Exception as error:
            await _report_wiki_error(message, error)
        finally:
            logger.info(
                "telegram_command command=randompage "
                "duration_ms=%s",
                round(
                    (monotonic() - started_at) * 1000
                ),
            )

    @router.message(Command("search"))
    async def search_title(message: types.Message) -> None:
        if message.from_user is not None:
            pending_inputs.clear(
                message.from_user.id,
                message.chat.id,
            )

        query = _argument(message)

        if not query:
            await request_pending_input(
                message,
                "search",
                "Введите название статьи:",
            )
            return

        await execute_search(
            message,
            query,
        )

    @router.message(Command("tags"))
    async def search_tags(message: types.Message) -> None:
        if message.from_user is not None:
            pending_inputs.clear(
                message.from_user.id,
                message.chat.id,
            )

        raw_tags = _argument(message)

        if not raw_tags:
            await request_pending_input(
                message,
                "tags",
                "Введите теги через пробел:",
            )
            return

        await execute_tags(
            message,
            raw_tags,
        )

    @router.message(Command("fullsearch"))
    async def search_content(message: types.Message) -> None:
        if message.from_user is not None:
            pending_inputs.clear(
                message.from_user.id,
                message.chat.id,
            )

        query = _argument(message)

        if not query:
            await request_pending_input(
                message,
                "fullsearch",
                "Введите текст для поиска:",
            )
            return

        await execute_fullsearch(
            message,
            query,
        )

    @router.message(Command("autor"))
    async def search_author(message: types.Message) -> None:
        if message.from_user is not None:
            pending_inputs.clear(
                message.from_user.id,
                message.chat.id,
            )

        query = _argument(message)

        if not query:
            await request_pending_input(
                message,
                "autor",
                "Введите ник автора:",
            )
            return

        await execute_author(message, query)

    @router.message(F.text)
    async def pending_text(message: types.Message) -> None:
        if message.from_user is None:
            return

        state = pending_inputs.get(
            message.from_user.id,
            message.chat.id,
        )

        if state is None:
            return

        text = _normalise_query(
            message.text or ""
        )

        if not text:
            await message.answer(
                "Введите непустое значение."
            )
            return

        if text.startswith("/"):
            pending_inputs.clear(
                message.from_user.id,
                message.chat.id,
            )
            return

        pending_inputs.clear(
            message.from_user.id,
            message.chat.id,
        )

        if state.action == "search":
            await execute_search(
                message,
                text,
            )
            return

        if state.action == "tags":
            await execute_tags(
                message,
                text,
            )
            return

        if state.action == "fullsearch":
            await execute_fullsearch(
                message,
                text,
            )
            return

        if state.action == "autor":
            await execute_author(
                message,
                text,
            )
            return

        logger.warning(
            "telegram_state_unknown action=%s",
            state.action,
        )

    @router.callback_query(F.data.startswith("s:"))
    async def paginate(
        callback: types.CallbackQuery,
    ) -> None:
        data = callback.data or ""
        parts = data.split(":", 2)

        if len(parts) != 3 or parts[0] != "s":
            await callback.answer(
                "Некорректная кнопка.",
                show_alert=True,
            )
            return

        _, token, raw_page = parts

        try:
            page = int(raw_page)
        except ValueError:
            await callback.answer(
                "Некорректная страница.",
                show_alert=True,
            )
            return

        state = searches.get(token)

        if state is None:
            await callback.answer(
                "Результаты поиска устарели. "
                "Запустите поиск заново.",
                show_alert=True,
            )
            return

        if callback.from_user.id != state.owner_id:
            await callback.answer(
                "Эти результаты принадлежат другому пользователю.",
                show_alert=True,
            )
            return

        if callback.message is None:
            await callback.answer(
                "Сообщение с результатами недоступно.",
                show_alert=True,
            )
            return

        total_pages = max(
            1,
            (
                len(state.results)
                + RESULTS_PER_PAGE
                - 1
            )
            // RESULTS_PER_PAGE,
        )

        page = max(
            1,
            min(page, total_pages),
        )

        try:
            await callback.message.edit_text(
                _render_search_page(
                    state.results,
                    state.query,
                    page,
                    heading=state.heading,
                    match_excerpt=state.match_excerpt,
                ),
                reply_markup=_search_keyboard(
                    token,
                    page,
                    total_pages,
                ),
                disable_web_page_preview=True,
            )
            await callback.answer()
        except Exception:
            logger.exception(
                "telegram_search_pagination_failed"
            )

            try:
                await callback.answer(
                    "Не удалось обновить результаты.",
                    show_alert=True,
                )
            except Exception:
                logger.exception(
                    "telegram_callback_error_failed"
                )

    return router