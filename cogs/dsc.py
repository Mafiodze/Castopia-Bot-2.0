"""Discord commands for the public Castopia wiki client."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from time import monotonic
from typing import Awaitable, Callable, TypeVar, cast

import discord
from discord import app_commands
from discord.ext import commands

from .constants import FOOTER_TEXT, load_wiki_config
from .page_parsing import (
    Article,
    UpstreamAccessError,
    UpstreamContentError,
    UpstreamNotFoundError,
    UpstreamUnavailableError,
    WikiClient,
    WikiError,
)
from .txt_processing import (
    escape_discord,
    excerpt,
    article_meta_lines,
    article_card_text,
)

logger = logging.getLogger(__name__)

PUBLIC_READER_CONCURRENCY = 8
RESULTS_PER_PAGE = 5
MAX_QUERY_LENGTH = 160
MAX_TAGS = 5
AUTOCOMPLETE_TIMEOUT = 2.5
VIEW_TIMEOUT = 10 * 60
DISCORD_FIELD_VALUE_LIMIT = 1024

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _RateLimit:
    requests: int
    period_seconds: float


class _RateLimiter:
    """In-memory per-user, per-command sliding-window limiter."""

    def __init__(self, limits: dict[str, _RateLimit]) -> None:
        self._limits = limits
        self._uses: dict[tuple[int, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def retry_after(self, user_id: int, command_name: str) -> float:
        """Return seconds until the command is allowed, or zero when allowed."""
        limit = self._limits[command_name]
        now = monotonic()
        key = (user_id, command_name)

        async with self._lock:
            uses = self._uses[key]

            while uses and now - uses[0] >= limit.period_seconds:
                uses.popleft()

            if len(uses) >= limit.requests:
                return max(0.0, limit.period_seconds - (now - uses[0]))

            uses.append(now)

            if not uses:
                self._uses.pop(key, None)

        return 0.0


def _article_embed(article: Article, query: str = "") -> discord.Embed:
    """Build an article embed from sanitized wiki content."""
    body = escape_discord(
        article_card_text(article, query, preview_limit=700)
    )
    embed = discord.Embed(
        title=article.title[:256],
        description=body,
        url=article.url,
        colour=discord.Colour.dark_red(),
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


class SearchResultsView(discord.ui.View):
    """Owner-only pagination for full-text search results."""

    def __init__(
        self,
        owner_id: int,
        results: list[Article],
        query: str,
        *,
        heading: str = "Результаты поиска",
        match_excerpt: bool = True,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.owner_id = owner_id
        self.results = results
        self.query = query
        self.heading = heading
        self.match_excerpt = match_excerpt
        self.page = 1
        self.message: discord.Message | discord.WebhookMessage | None = None
        self._update_buttons()

    @property
    def total_pages(self) -> int:
        """Return the number of result pages."""
        return max(
            1,
            (len(self.results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE,
        )

    def _update_buttons(self) -> None:
        self.previous_page.disabled = self.page <= 1
        self.next_page.disabled = self.page >= self.total_pages

    def create_embed(self) -> discord.Embed:
        """Build the embed for the current result page."""
        start = (self.page - 1) * RESULTS_PER_PAGE
        embed = discord.Embed(
            title=self.heading[:256],
            description=(
                f"Найдено: {len(self.results)} • "
                f"страница {self.page}/{self.total_pages}"
            ),
            colour=discord.Colour.dark_red(),
        )

        needle = self.query if self.match_excerpt else ""
        for article in self.results[start : start + RESULTS_PER_PAGE]:
            title = escape_discord(article.title)[:256]
            preview = escape_discord(excerpt(article.text, needle, limit=220))
            meta = "\n".join(
                escape_discord(line)
                for line in article_meta_lines(article, include_tags=True)
            )
            value = (
                f"{preview}\n{meta}\n"
                f"[Открыть статью]({article.url})"
            )
            if len(value) > DISCORD_FIELD_VALUE_LIMIT:
                value = value[: DISCORD_FIELD_VALUE_LIMIT - 1] + "…"
            embed.add_field(
                name=title,
                value=value,
                inline=False,
            )

        embed.set_footer(text=FOOTER_TEXT)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Allow pagination only for the user who started the search."""
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "Эти результаты принадлежат другому пользователю.",
            ephemeral=True,
        )
        return False

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._update_buttons()
        await interaction.response.edit_message(
            embed=self.create_embed(),
            view=self,
        )

    @discord.ui.button(label="← Назад", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.page = max(1, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Вперёд →", style=discord.ButtonStyle.primary)
    async def next_page(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.page = min(self.total_pages, self.page + 1)
        await self._refresh(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

        if self.message is None:
            return

        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            logger.debug("discord_search_view_timeout_edit_failed")


class DscCog(commands.Cog):
    """Prefix and slash commands backed by one shared WikiClient."""

    RATE_LIMITS = {
        "search": _RateLimit(3, 20),
        "tags": _RateLimit(2, 30),
        "randompage": _RateLimit(2, 20),
        "fullsearch": _RateLimit(1, 30),
        "autor": _RateLimit(1, 30),
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.wiki = WikiClient(
            load_wiki_config(),
            reader_concurrency=PUBLIC_READER_CONCURRENCY,
        )
        self.rate_limiter = _RateLimiter(self.RATE_LIMITS)

    async def cog_load(self) -> None:
        await self.wiki.start()
        asyncio.create_task(
            self.wiki.warmup_public_index(),
            name="wiki-public-warmup",
        )

    async def cog_unload(self) -> None:
        await self.wiki.close()

    @staticmethod
    def _error_text(error: Exception) -> str:
        if isinstance(error, UpstreamAccessError):
            return (
                "Источник запретил автоматический доступ. Нужен официальный API "
                "или разрешение владельца сайта."
            )
        if isinstance(error, UpstreamContentError):
            return (
                "Структура источника изменилась. "
                "Администратор уже получил диагностическую запись."
            )
        if isinstance(error, UpstreamNotFoundError):
            return "Статья больше не существует в источнике."
        if isinstance(error, UpstreamUnavailableError):
            return "Источник временно недоступен. Попробуйте немного позже."
        if isinstance(error, WikiError):
            return "Не удалось получить данные из источника."
        return "Не удалось обработать команду. Попробуйте ещё раз позже."

    @staticmethod
    async def _send_interaction_error(
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            logger.exception("discord_error_response_failed")

    async def _send_error(
        self,
        ctx: commands.Context,
        error: Exception,
    ) -> None:
        if not isinstance(error, WikiError):
            logger.error(
                "discord_command_failed error=%s",
                type(error).__name__,
                exc_info=error,
            )

        text = self._error_text(error)
        interaction = ctx.interaction

        if interaction is not None:
            await self._send_interaction_error(interaction, text)
            return

        try:
            await ctx.send(text)
        except discord.HTTPException:
            logger.exception("discord_command_error_message_failed")

    async def _prepare_command(
        self,
        ctx: commands.Context,
        command_name: str,
    ) -> bool:
        """Defer interactions before slow work and enforce the command limit."""
        interaction = ctx.interaction

        if interaction is not None and not interaction.response.is_done():
            try:
                await interaction.response.defer(thinking=True)
            except discord.HTTPException:
                logger.exception("discord_interaction_defer_failed")
                return False

        retry_after = await self.rate_limiter.retry_after(
            ctx.author.id,
            command_name,
        )
        if not retry_after:
            return True

        message = f"Подождите {retry_after:.0f} с перед следующим запросом."

        if interaction is not None:
            await self._send_interaction_error(interaction, message)
        else:
            try:
                await ctx.send(message)
            except discord.HTTPException:
                logger.exception("discord_rate_limit_message_failed")

        return False

    async def _invoke(
        self,
        ctx: commands.Context,
        command_name: str,
        operation: Callable[[], Awaitable[T]],
    ) -> tuple[bool, T | None]:
        """Run a wiki operation with rate limiting, error handling and timing."""
        if not await self._prepare_command(ctx, command_name):
            return False, None

        started_at = monotonic()

        try:
            if ctx.interaction is None:
                async with ctx.typing():
                    result = await operation()
            else:
                result = await operation()
        except Exception as error:
            await self._send_error(ctx, error)
            return False, None
        finally:
            logger.info(
                "discord_command command=%s mode=%s duration_ms=%s",
                command_name,
                "slash" if ctx.interaction is not None else "prefix",
                round((monotonic() - started_at) * 1000),
            )

        return True, result

    async def cog_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        original = getattr(error, "original", error)
        await self._send_error(ctx, original)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        text = self._error_text(original)

        if not isinstance(original, WikiError):
            logger.error(
                "discord_app_command_failed error=%s",
                type(original).__name__,
                exc_info=original,
            )

        await self._send_interaction_error(interaction, text)

    @commands.hybrid_command(
        name="help",
        description="Показать команды Castopia",
    )
    async def show_help(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="Castopia — команды",
            description=(
                "`.search <название>` или `/search` — найти статью по названию\n"
                "`.fullsearch <текст>` или `/fullsearch` — поиск по содержимому\n"
                "`.autor <ник>` или `/autor` — статьи автора\n"
                "`.tags <тег> [тег…]` или `/tags` — статьи с тегами\n"
                "`.randompage` или `/randompage` — случайная статья\n\n"
                "Бот работает только с публичными страницами и не обходит "
                "ограничения источника."
            ),
            colour=discord.Colour.dark_red(),
        )
        embed.set_footer(text=FOOTER_TEXT)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="randompage",
        description="Показать случайную статью",
    )
    async def random_page(self, ctx: commands.Context) -> None:
        ok, result = await self._invoke(
            ctx,
            "randompage",
            self.wiki.random_article,
        )
        if not ok:
            return

        article = cast(Article | None, result)
        if article is None:
            await self._send_command_result(
                ctx,
                "Не нашёл подходящую публичную статью.",
            )
            return

        await self._send_command_result(
            ctx,
            embed=_article_embed(article),
        )

    @commands.hybrid_command(
        name="search",
        description="Найти статью по названию",
    )
    @app_commands.describe(query="Название статьи")
    async def search_title(
        self,
        ctx: commands.Context,
        *,
        query: str = "",
    ) -> None:
        query = query.strip()

        if not query:
            await self._send_command_result(
                ctx,
                "Укажите название: `.search название статьи` или `/search`.",
            )
            return

        if len(query) > MAX_QUERY_LENGTH:
            await self._send_command_result(
                ctx,
                f"Запрос должен быть не длиннее {MAX_QUERY_LENGTH} символов.",
            )
            return

        ok, result = await self._invoke(
            ctx,
            "search",
            lambda: self.wiki.find_by_title(query),
        )
        if not ok:
            return

        article = cast(Article | None, result)
        if article is None:
            await self._send_command_result(
                ctx,
                f"Статья «{escape_discord(query)}» не найдена.",
            )
            return

        await self._send_command_result(
            ctx,
            embed=_article_embed(article),
        )

    @search_title.autocomplete("query")
    async def search_title_autocomplete(
        self,
        _: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Return up to Discord's 25 title suggestions within a short timeout."""
        normalized = current.strip()
        if len(normalized) < 2:
            return []

        try:
            suggestions = await asyncio.wait_for(
                self.wiki.title_suggestions(normalized, limit=25),
                timeout=AUTOCOMPLETE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.debug(
                "discord_autocomplete_timeout query_length=%s",
                len(normalized),
            )
            return []
        except WikiError as error:
            logger.debug(
                "discord_autocomplete_wiki_error error=%s",
                type(error).__name__,
            )
            return []
        except Exception:
            logger.exception("discord_autocomplete_failed")
            return []

        return [
            app_commands.Choice(name=title[:100], value=title[:100])
            for title in suggestions[:25]
        ]

    @commands.hybrid_command(
        name="tags",
        description="Найти статьи с тегами",
    )
    @app_commands.describe(tags="Теги через пробел")
    async def search_tags(
        self,
        ctx: commands.Context,
        *,
        tags: str = "",
    ) -> None:
        tag_values = tags.split()

        if not tag_values:
            await self._send_command_result(
                ctx,
                "Укажите хотя бы один тег: `.tags тег1 тег2` или `/tags`.",
            )
            return

        if len(tag_values) > MAX_TAGS:
            await self._send_command_result(
                ctx,
                f"Можно указать не более {MAX_TAGS} тегов.",
            )
            return

        if any(len(tag) > MAX_QUERY_LENGTH for tag in tag_values):
            await self._send_command_result(ctx, "Тег слишком длинный.")
            return

        ok, result = await self._invoke(
            ctx,
            "tags",
            lambda: self.wiki.find_by_tags(tag_values),
        )
        if not ok:
            return

        articles = cast(list[Article], result)
        if not articles:
            await self._send_command_result(
                ctx,
                "По этим тегам ничего не найдено.",
            )
            return

        embed = discord.Embed(
            title=f"Статьи с тегами: {', '.join(tag_values)}"[:256],
            description=f"Найдено: {len(articles)}",
            colour=discord.Colour.dark_red(),
        )

        for article in articles[:20]:
            embed.add_field(
                name=escape_discord(article.title)[:256],
                value=f"[Открыть статью]({article.url})",
                inline=False,
            )

        embed.set_footer(text=FOOTER_TEXT)
        await self._send_command_result(ctx, embed=embed)

    @commands.hybrid_command(
        name="fullsearch",
        description="Искать текст во всех статьях",
    )
    @app_commands.describe(query="Текст для поиска")
    async def search_content(
        self,
        ctx: commands.Context,
        *,
        query: str = "",
    ) -> None:
        query = query.strip()

        if not query:
            await self._send_command_result(
                ctx,
                "Укажите текст: `.fullsearch запрос` или `/fullsearch`.",
            )
            return

        if len(query) > MAX_QUERY_LENGTH:
            await self._send_command_result(
                ctx,
                f"Запрос должен быть не длиннее {MAX_QUERY_LENGTH} символов.",
            )
            return

        ok, result = await self._invoke(
            ctx,
            "fullsearch",
            lambda: self.wiki.search_content(query),
        )
        if not ok:
            return

        results = cast(list[Article], result)
        if not results:
            await self._send_command_result(
                ctx,
                f"По запросу «{escape_discord(query)}» ничего не найдено.",
            )
            return

        view = SearchResultsView(ctx.author.id, results, query)
        message = await self._send_command_result(
            ctx,
            embed=view.create_embed(),
            view=view,
        )

        if isinstance(message, (discord.Message, discord.WebhookMessage)):
            view.message = message

    @commands.hybrid_command(
        name="autor",
        description="Найти статьи автора",
    )
    @app_commands.describe(query="Ник автора")
    async def search_author(
        self,
        ctx: commands.Context,
        *,
        query: str = "",
    ) -> None:
        query = query.strip()

        if not query:
            await self._send_command_result(
                ctx,
                "Укажите автора: `.autor ник` или `/autor`.",
            )
            return

        if len(query) > MAX_QUERY_LENGTH:
            await self._send_command_result(
                ctx,
                f"Запрос должен быть не длиннее {MAX_QUERY_LENGTH} символов.",
            )
            return

        ok, result = await self._invoke(
            ctx,
            "autor",
            lambda: self.wiki.find_by_author(query),
        )
        if not ok:
            return

        results = cast(list[Article], result)
        if not results:
            await self._send_command_result(
                ctx,
                f"Автор «{escape_discord(query)}» не найден.",
            )
            return

        view = SearchResultsView(
            ctx.author.id,
            results,
            query,
            heading=f"Статьи автора: {query}",
            match_excerpt=False,
        )
        message = await self._send_command_result(
            ctx,
            embed=view.create_embed(),
            view=view,
        )

        if isinstance(message, (discord.Message, discord.WebhookMessage)):
            view.message = message

    async def _send_command_result(
        self,
        ctx: commands.Context,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> discord.Message | discord.WebhookMessage | None:
        """Send a command result without passing an invalid null View."""
        interaction = ctx.interaction
        send_kwargs: dict[str, object] = {
            "embed": embed,
        }

        if content is not None:
            send_kwargs["content"] = content

        if view is not None:
            send_kwargs["view"] = view

        try:
            if interaction is not None:
                if interaction.response.is_done():
                    return await interaction.followup.send(
                        wait=True,
                        **send_kwargs,
                    )

                await interaction.response.send_message(**send_kwargs)
                return None

            return await ctx.send(**send_kwargs)
        except discord.HTTPException:
            logger.exception("discord_command_result_send_failed")
            return None


async def setup(bot: commands.Bot) -> None:
    """Register the Discord command cog."""
    await bot.add_cog(DscCog(bot))