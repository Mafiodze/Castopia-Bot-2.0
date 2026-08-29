"""Cached, bounded client for the public Castopia wiki.

The client intentionally does not bypass access controls. HTTP 401/403 responses
are surfaced to callers so access must be obtained through an official API or the
site owner's permission.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import logging
import random
import re
import socket
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Generic, TypeVar
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from .constants import SYSTEM_TAGS, WikiConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


class WikiError(RuntimeError):
    """Base error for a public-wiki request."""


class UpstreamAccessError(WikiError):
    """The source disallows this automated request."""


class UpstreamUnavailableError(WikiError):
    """The source could not be reached reliably."""


class UpstreamNotFoundError(WikiError):
    """A page listed by the source no longer exists."""


class UpstreamContentError(WikiError):
    """The source HTML no longer matches the expected public wiki structure."""


class ConfigurationErrorProxy(WikiError):
    """Raised when FamiliarBot is missing wiki credentials."""


@dataclass(frozen=True, slots=True)
class Article:
    title: str
    url: str
    text: str
    tags: frozenset[str]
    author: str = ""
    last_edit: str = ""
    last_edit_at: datetime | None = None
    page_id: str = ""
    rating: float | None = None
    votes_count: int | None = None
    popularity: int | None = None
    version: int | None = None
    comment_thread: str = ""


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    kind: str
    created_at: datetime
    user: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float

    def is_fresh(self) -> bool:
        return monotonic() < self.expires_at


@dataclass(frozen=True, slots=True)
class _TagReference:
    identifier: str
    url: str


@dataclass(slots=True)
class _UrlLockEntry:
    lock: asyncio.Lock
    users: int = 0


class WikiClient:
    """Fetch and parse wiki content with bounded concurrency and TTL caches."""

    PAGE_CACHE_TTL = timedelta(minutes=10)
    LINK_CACHE_TTL = timedelta(minutes=5)
    SEARCH_CACHE_TTL = timedelta(minutes=5)
    CORPUS_TTL = timedelta(minutes=10)
    REQUEST_ATTEMPTS = 4
    EDIT_LABELS = frozenset({"edit", "редактировать"})

    MAX_PAGE_CACHE_ENTRIES = 512
    MAX_ARTICLE_CACHE_ENTRIES = 512
    MAX_SEARCH_CACHE_ENTRIES = 64

    def __init__(
        self,
        config: WikiConfig,
        *,
        reader_concurrency: int | None = None,
    ) -> None:
        self.config = config
        self.base_url = config.base_url
        self.all_pages_url = config.all_pages_url
        self.tags_url = config.tags_url

        parsed_base_url = urlsplit(self.base_url)
        self._origin = (parsed_base_url.scheme, parsed_base_url.netloc)
        self._reader_concurrency = max(
            1,
            reader_concurrency or config.max_concurrent_requests,
        )

        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(self._reader_concurrency)
        self._full_search_lock = asyncio.Lock()

        self._page_cache: dict[str, _CacheEntry[str]] = {}
        self._article_cache: dict[str, _CacheEntry[Article]] = {}
        self._search_cache: dict[str, _CacheEntry[list[Article]]] = {}
        self._corpus_cache: _CacheEntry[list[Article]] | None = None

        self._url_locks: dict[str, _UrlLockEntry] = {}
        self._links_cache: _CacheEntry[list[tuple[str, str]]] | None = None
        self._tag_catalog_cache: _CacheEntry[
            dict[str, list[_TagReference]]
        ] | None = None
        self._csrf_token: str = ""
        self._logged_in: bool = False
        self._login_username: str = ""

    async def start(self) -> None:
        """Create the shared HTTP session if it is not already open."""
        if self._session is not None and not self._session.closed:
            return

        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            limit=max(8, self._reader_concurrency * 2),
            limit_per_host=self._reader_concurrency,
            ttl_dns_cache=300,
            keepalive_timeout=45,
            enable_cleanup_closed=True,
        )

        timeout = aiohttp.ClientTimeout(
            total=self.config.timeout_seconds,
            connect=min(15.0, self.config.timeout_seconds / 2),
            sock_connect=min(15.0, self.config.timeout_seconds / 2),
            sock_read=self.config.timeout_seconds,
        )

        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Accept-Language": "ru,en;q=0.8",
        }

        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            cookie_jar=aiohttp.CookieJar(),
            raise_for_status=False,
        )

        if self.config.anubis_cookie:
            from yarl import URL

            self._session.cookie_jar.update_cookies(
                {"techaro.lol-anubis-auth": self.config.anubis_cookie},
                response_url=URL(f"{self.base_url}/"),
            )

    async def close(self) -> None:
        """Close the shared HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _normalise_url(self, url: str) -> str:
        """Return an absolute URL and reject requests outside the wiki origin."""
        absolute = urljoin(f"{self.base_url}/", url)
        parsed = urlsplit(absolute)

        if (parsed.scheme, parsed.netloc) != self._origin:
            raise ValueError(
                "Refusing to request a URL outside the configured wiki origin"
            )

        return absolute

    @staticmethod
    def _is_edit_link(title: str, href: str) -> bool:
        """Return whether a link points to an edit control rather than an article."""
        path = urlsplit(href).path.casefold().rstrip("/")

        return (
            title.casefold() in WikiClient.EDIT_LABELS
            or "/edit/" in path
            or path.endswith("/edit")
        )

    @staticmethod
    def _tag_identifier(anchor: object) -> str:
        """Read Wikidot's full category:value tag from a tag-link href."""
        href = getattr(anchor, "get", lambda *_: None)("href")
        if not href:
            return ""

        path = urlsplit(href).path
        marker = "/tag/"

        if marker not in path:
            return ""

        return unquote(path.split(marker, 1)[1]).casefold().strip()

    @staticmethod
    def _prune_cache(
        cache: dict[str, _CacheEntry[T]],
        max_entries: int,
    ) -> None:
        """Drop expired entries and then oldest entries above the size cap."""
        now = monotonic()

        expired = [
            key
            for key, entry in cache.items()
            if entry.expires_at <= now
        ]

        for key in expired:
            cache.pop(key, None)

        overflow = len(cache) - max_entries

        if overflow > 0:
            for key in list(cache)[:overflow]:
                cache.pop(key, None)

    def _store_cache(
        self,
        cache: dict[str, _CacheEntry[T]],
        key: str,
        value: T,
        ttl: timedelta,
        max_entries: int,
    ) -> None:
        """Store a cache value and keep the cache bounded."""
        cache[key] = _CacheEntry(
            value,
            monotonic() + ttl.total_seconds(),
        )
        self._prune_cache(cache, max_entries)

    async def fetch_html(self, url: str) -> str:
        """Fetch one same-origin page using a short-lived response cache."""
        url = self._normalise_url(url)

        cached = self._page_cache.get(url)
        if cached and cached.is_fresh():
            logger.debug("wiki_fetch cache_hit=true")
            return cached.value

        lock_entry = self._url_locks.get(url)

        if lock_entry is None:
            lock_entry = _UrlLockEntry(asyncio.Lock())
            self._url_locks[url] = lock_entry

        lock_entry.users += 1

        try:
            async with lock_entry.lock:
                cached = self._page_cache.get(url)

                if cached and cached.is_fresh():
                    logger.debug("wiki_fetch cache_hit=true")
                    return cached.value

                html = await self._request_html(url)

                self._store_cache(
                    self._page_cache,
                    url,
                    html,
                    self.PAGE_CACHE_TTL,
                    self.MAX_PAGE_CACHE_ENTRIES,
                )

                logger.debug("wiki_fetch cache_hit=false")
                return html
        finally:
            lock_entry.users -= 1

            if lock_entry.users == 0:
                self._url_locks.pop(url, None)

    async def _request_html(self, url: str) -> str:
        """Fetch HTML with bounded concurrency, retries and structured errors."""
        await self.start()

        if self._session is None:
            raise UpstreamUnavailableError(
                "HTTP client session is unavailable."
            )

        last_error: Exception | None = None

        for attempt in range(1, self.REQUEST_ATTEMPTS + 1):
            try:
                started_at = monotonic()

                got_ok = False
                body = ""
                async with self._semaphore:
                    async with self._session.get(
                        url,
                        allow_redirects=True,
                    ) as response:
                        elapsed_ms = round(
                            (monotonic() - started_at) * 1000
                        )

                        logger.debug(
                            "wiki_request status=%s duration_ms=%s attempt=%s",
                            response.status,
                            elapsed_ms,
                            attempt,
                        )

                        if response.status in {401, 403}:
                            raise UpstreamAccessError(
                                "Источник не разрешает автоматический доступ. "
                                "Используйте официальный API или запросите "
                                "разрешение владельца сайта."
                            )

                        if response.status == 404:
                            raise UpstreamNotFoundError(
                                "Страница больше не существует в источнике."
                            )

                        if response.status == 429 or 500 <= response.status < 600:
                            if attempt == self.REQUEST_ATTEMPTS:
                                raise UpstreamUnavailableError(
                                    f"Источник временно недоступен "
                                    f"(HTTP {response.status})."
                                )
                            delay = self._retry_delay(response, attempt)

                        elif 400 <= response.status < 500:
                            raise UpstreamUnavailableError(
                                f"Источник отклонил запрос "
                                f"(HTTP {response.status})."
                            )
                        else:
                            body = await response.text(errors="replace")
                            got_ok = True

                if not got_ok:
                    await asyncio.sleep(delay)
                    continue

                if self._is_anubis_page(body):
                    await self._pass_anubis(body, redir=url)
                    async with self._semaphore:
                        async with self._session.get(
                            url,
                            allow_redirects=True,
                        ) as retry:
                            body = await retry.text(errors="replace")
                    if self._is_anubis_page(body):
                        raise UpstreamAccessError(
                            "Anubis blocked the request after a challenge. "
                            "Allowlist the bot User-Agent or set WIKI_ANUBIS_COOKIE."
                        )
                return body

            except (
                UpstreamAccessError,
                UpstreamNotFoundError,
                UpstreamUnavailableError,
            ):
                raise

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                elapsed_ms = round((monotonic() - started_at) * 1000)
                log = (
                    logger.warning
                    if attempt == self.REQUEST_ATTEMPTS
                    else logger.info
                )
                log(
                    "wiki_request_retry attempt=%s/%s error=%s "
                    "duration_ms=%s url=%s",
                    attempt,
                    self.REQUEST_ATTEMPTS,
                    type(exc).__name__,
                    elapsed_ms,
                    url,
                )

                if attempt < self.REQUEST_ATTEMPTS:
                    delay = min(
                        1.5 * (2 ** (attempt - 1))
                        + random.random() / 2,
                        20.0,
                    )
                    await asyncio.sleep(delay)

        raise UpstreamUnavailableError(
            "Не удалось связаться с источником."
        ) from last_error

    @staticmethod
    def _retry_delay(
        response: aiohttp.ClientResponse,
        attempt: int,
    ) -> float:
        """Return a bounded retry delay, honoring a valid Retry-After header."""
        retry_after = response.headers.get("Retry-After")

        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 30.0)
            except ValueError:
                logger.debug("wiki_retry_after_invalid")

        return min(
            0.75 * (2 ** (attempt - 1)) + random.random() / 4,
            10.0,
        )

    async def all_links(self) -> list[tuple[str, str]]:
        """Return de-duplicated article links from every all-pages listing page."""
        if self._links_cache and self._links_cache.is_fresh():
            return list(self._links_cache.value)

        first_page = await self.fetch_html(self.all_pages_url)
        soup = BeautifulSoup(first_page, "lxml")
        page_content = soup.select_one("#page-content")

        if page_content is None:
            raise UpstreamContentError(
                "Источник вернул страницу списка без блока #page-content. "
                "Структура сайта могла измениться."
            )

        if not page_content.select("div.list-pages-box"):
            raise UpstreamContentError(
                "Источник вернул страницу списка без блоков "
                ".list-pages-box. Структура сайта могла измениться."
            )

        total_pages = self._parse_total_pages(first_page)
        pages = [first_page]

        if total_pages > 1:
            page_urls = [
                f"{self.all_pages_url}/p/{number}"
                for number in range(2, total_pages + 1)
            ]
            pages.extend(await self._fetch_in_batches(page_urls))

        seen: set[str] = set()
        links: list[tuple[str, str]] = []

        for html in pages:
            for title, url in self._parse_list_links(html):
                if url in seen:
                    continue

                seen.add(url)
                links.append((title, url))

        if not links:
            raise UpstreamContentError(
                "Источник вернул страницы списка со структурой "
                ".list-pages-box, но валидных ссылок на статьи не найдено."
            )

        self._links_cache = _CacheEntry(
            links,
            monotonic() + self.LINK_CACHE_TTL.total_seconds(),
        )

        return list(links)

    async def _run_bounded(
        self,
        values: Iterable[T],
        operation: Callable[[T], Awaitable[R]],
        *,
        log_label: str,
    ) -> tuple[list[R | None], list[Exception]]:
        """Run an async operation with bounded workers while preserving input order."""
        items = list(values)

        if not items:
            return [], []

        results: list[R | None] = [None] * len(items)
        failures: list[Exception] = []
        pending = deque(enumerate(items))

        async def worker() -> None:
            while True:
                try:
                    index, value = pending.popleft()
                except IndexError:
                    return

                try:
                    results[index] = await operation(value)
                except Exception as exc:
                    failures.append(exc)
                    logger.debug(
                        "%s item_failed index=%s error=%s",
                        log_label,
                        index,
                        type(exc).__name__,
                    )

        worker_count = min(
            self.config.max_concurrent_requests,
            len(items),
        )

        await asyncio.gather(
            *(asyncio.create_task(worker()) for _ in range(worker_count))
        )

        return results, failures

    async def _fetch_in_batches(
        self,
        urls: Iterable[str],
    ) -> list[str]:
        """Fetch listing pages concurrently without exceeding the configured limit."""
        results, _ = await self._run_bounded(
            urls,
            self.fetch_html,
            log_label="wiki_fetch_batch",
        )

        return [
            page
            for page in results
            if page is not None
        ]

    @staticmethod
    def _parse_total_pages(html: str) -> int:
        soup = BeautifulSoup(html, "lxml")
        pager = soup.find("span", class_="pager-no")

        if not pager:
            return 1

        match = re.search(
            r"(\d+)\s*$",
            pager.get_text(" ", strip=True),
        )

        return max(1, int(match.group(1))) if match else 1

    def _parse_list_links(
        self,
        html: str,
    ) -> list[tuple[str, str]]:
        """Parse article links from all list-page boxes and omit edit controls."""
        soup = BeautifulSoup(html, "lxml")
        scope = soup.select_one("#page-content") or soup
        links: list[tuple[str, str]] = []
        seen: set[str] = set()
        boxes = scope.select("div.list-pages-box")
        tagged = scope.select("#tagged-pages-list")
        containers = boxes or tagged or [scope]

        for box in containers:
            for anchor in box.find_all("a", href=True):
                title = anchor.get_text(" ", strip=True)
                href = anchor["href"]

                if not title or self._is_edit_link(title, href):
                    continue

                try:
                    url = self._normalise_url(href)
                except ValueError:
                    logger.debug("wiki_link_skipped reason=off_origin")
                    continue

                if url in seen:
                    continue
                seen.add(url)
                links.append((title, url))

        return links

    async def get_article(
        self,
        title: str,
        url: str,
    ) -> Article:
        """Fetch, clean and cache one article."""
        url = self._normalise_url(url)

        cached = self._article_cache.get(url)
        if cached and cached.is_fresh():
            return cached.value

        html = await self.fetch_html(url)
        soup = BeautifulSoup(html, "lxml")
        options = self._parse_page_options(soup)
        page_id = str(options.get("pageId") or self._page_id_from_url(url))

        content = soup.find("div", id="page-content")

        if content is None:
            raise UpstreamContentError(
                "Источник вернул страницу без блока #page-content. "
                "Структура сайта могла измениться."
            )

        for element in content.select(
            "script, style, noscript, .no-style, "
            ".footnoteref, #side-bar, "
            ".adult-content-warning-container"
        ):
            element.decompose()

        text = re.sub(
            r"\s+",
            " ",
            content.get_text(" ", strip=True),
        ).strip()

        if not text:
            raise UpstreamContentError(
                f"Страница '{title}' не содержит доступного текста. "
                "Возможно, это служебная страница."
            )

        tags = frozenset(
            self._tag_identifier(item)
            or item.get_text(" ", strip=True).casefold()
            for item in soup.select("div.page-tags a")
            if item.get_text(strip=True)
        )

        title_node = soup.select_one("#page-title")
        resolved_title = (
            title_node.get_text(" ", strip=True) if title_node else title
        ) or title

        author, last_edit, last_edit_at, version = self._parse_article_chrome(
            soup
        )

        article = Article(
            title=resolved_title,
            url=url,
            text=text,
            tags=tags,
            author=author,
            last_edit=last_edit,
            last_edit_at=last_edit_at,
            page_id=page_id,
            rating=self._optional_float(options.get("rating")),
            votes_count=self._optional_int(options.get("ratingVotes")),
            popularity=self._optional_int(options.get("ratingPopularity")),
            version=version,
            comment_thread=str(options.get("commentThread") or ""),
        )

        self._store_cache(
            self._article_cache,
            url,
            article,
            self.PAGE_CACHE_TTL,
            self.MAX_ARTICLE_CACHE_ENTRIES,
        )

        return article

    @staticmethod
    def _is_public_candidate(
        title: str,
        url: str,
    ) -> bool:
        value = f"{title} {url}".casefold()

        return (
            "draft:" not in value
            and "admin:" not in value
            and "/edit/" not in value
            and not value.endswith("/edit")
        )

    def _is_system_listing_url(self, url: str) -> bool:
        page_id = self._page_id_from_url(url).casefold()
        category = self.page_category(page_id)
        return category in {
            "system",
            "nav",
            "forum",
            "admin",
            "search",
            "deleted",
        }

    def _listing_article(
        self,
        title: str,
        url: str,
        tags: Iterable[str] = (),
    ) -> Article:
        return Article(
            title=title,
            url=url,
            text="",
            tags=frozenset(str(tag) for tag in tags if str(tag)),
            page_id=self._page_id_from_url(url),
        )

    def _fresh_corpus(self) -> list[Article] | None:
        if self._corpus_cache and self._corpus_cache.is_fresh():
            return list(self._corpus_cache.value)
        return None

    async def public_articles(self) -> list[Article]:
        """Load public articles once and reuse them for search/random/author."""
        cached = self._fresh_corpus()
        if cached is not None:
            return cached

        async with self._full_search_lock:
            cached = self._fresh_corpus()
            if cached is not None:
                return cached

            started_at = monotonic()
            candidates = [
                item
                for item in await self.all_links()
                if self._is_public_candidate(*item)
            ]
            articles = await self._get_articles_in_batches(candidates)
            public = [
                article
                for article in articles
                if article.text and not (article.tags & SYSTEM_TAGS)
            ]
            self._corpus_cache = _CacheEntry(
                public,
                monotonic() + self.CORPUS_TTL.total_seconds(),
            )
            logger.info(
                "wiki_corpus_ready count=%s duration_ms=%s",
                len(public),
                round((monotonic() - started_at) * 1000),
            )
            return list(public)

    async def warmup_public_index(self) -> None:
        """Prefetch the public corpus for Discord/Telegram. Unused by FamiliarBot."""
        try:
            await self.public_articles()
        except Exception:
            logger.exception("wiki_corpus_warmup_failed")

    async def _get_articles_in_batches(
        self,
        candidates: Iterable[tuple[str, str]],
    ) -> list[Article]:
        """Fetch and parse articles concurrently with bounded workers."""
        values = list(candidates)

        if not values:
            return []

        async def fetch_article(
            item: tuple[str, str],
        ) -> Article:
            return await self.get_article(*item)

        results, failures = await self._run_bounded(
            values,
            fetch_article,
            log_label="wiki_article_batch",
        )

        articles = [
            article
            for article in results
            if article is not None
        ]

        if not articles and failures:
            first_error = failures[0]

            if isinstance(first_error, WikiError):
                raise first_error

            raise UpstreamUnavailableError(
                "Не удалось загрузить статьи из источника."
            ) from first_error

        return articles

    async def find_by_title(
        self,
        query: str,
    ) -> Article | None:
        """Find an article by exact title first, then by partial title match."""
        normalized = query.casefold().strip()

        if not normalized:
            return None

        candidates = [
            (title, url)
            for title, url in await self.all_links()
            if self._is_public_candidate(title, url)
        ]

        exact = next(
            (
                (title, url)
                for title, url in candidates
                if title.casefold() == normalized
            ),
            None,
        )

        if exact is None:
            exact = next(
                (
                    (title, url)
                    for title, url in candidates
                    if normalized in title.casefold()
                ),
                None,
            )

        if exact is None:
            return None

        corpus = self._fresh_corpus()
        if corpus is not None:
            match = next(
                (
                    article
                    for article in corpus
                    if article.url == exact[1] or article.title.casefold() == exact[0].casefold()
                ),
                None,
            )
            if match is not None:
                return match

        try:
            return await self.get_article(*exact)
        except UpstreamNotFoundError:
            return None

    async def title_suggestions(
        self,
        query: str,
        *,
        limit: int = 25,
    ) -> list[str]:
        """Return up to ``limit`` public article titles matching a query."""
        normalized = query.casefold().strip()

        if not normalized:
            return []

        return [
            title
            for title, url in await self.all_links()
            if (
                self._is_public_candidate(title, url)
                and normalized in title.casefold()
            )
        ][: max(0, limit)]

    async def random_article(self) -> Article | None:
        """Return a random public article, skipping stale or system pages."""
        corpus = self._fresh_corpus()
        if corpus:
            usable = [
                article
                for article in corpus
                if article.text and not (article.tags & SYSTEM_TAGS)
            ]
            if usable:
                return random.choice(usable)

        candidates = [
            item
            for item in await self.all_links()
            if self._is_public_candidate(*item)
        ]

        random.shuffle(candidates)

        for title, url in candidates[:12]:
            try:
                article = await self.get_article(title, url)
            except UpstreamNotFoundError:
                continue

            if article.text and not (article.tags & SYSTEM_TAGS):
                return article

        return None

    async def _tag_catalog(
        self,
    ) -> dict[str, list[_TagReference]]:
        if (
            self._tag_catalog_cache
            and self._tag_catalog_cache.is_fresh()
        ):
            return self._tag_catalog_cache.value

        html = await self.fetch_html(self.tags_url)
        soup = BeautifulSoup(html, "lxml")
        catalog: dict[str, list[_TagReference]] = {}

        for anchor in soup.select("a.tag[href]"):
            display_name = anchor.get_text(
                " ",
                strip=True,
            ).casefold()

            identifier = self._tag_identifier(anchor)

            if not display_name or not identifier:
                continue

            reference = _TagReference(
                identifier,
                self._normalise_url(anchor["href"]),
            )

            for key in {display_name, identifier}:
                catalog.setdefault(key, []).append(reference)

        if not catalog:
            raise UpstreamContentError(
                "Источник не вернул каталог тегов."
            )

        self._tag_catalog_cache = _CacheEntry(
            catalog,
            monotonic() + self.LINK_CACHE_TTL.total_seconds(),
        )

        return catalog

    async def _resolve_tags(
        self,
        tags: Iterable[str],
    ) -> list[_TagReference] | None:
        catalog = await self._tag_catalog()
        resolved: list[_TagReference] = []

        for raw_tag in tags:
            normalized = raw_tag.casefold().strip()
            candidates = catalog.get(normalized)

            if not candidates:
                return None

            resolved.append(candidates[0])

        return resolved

    async def find_by_tags(
        self,
        tags: Iterable[str],
    ) -> list[Article]:
        """Return public articles containing every requested tag.

        Uses tag listings only — no full page fetch. Discord/Telegram /tags
        shows titles and links; FamiliarBot does not call this.
        """
        raw_tags = [
            tag.strip()
            for tag in tags
            if tag.strip()
        ]

        if not raw_tags:
            return []

        references = await self._resolve_tags(raw_tags)

        if references is None:
            return []

        required = {
            reference.identifier
            for reference in references
        }

        common: set[str] | None = None
        titles: dict[str, str] = {}

        for reference in references:
            html = await self.fetch_html(reference.url)
            items = self._parse_tagged_pages(html)
            urls = set()
            for title, url in items:
                if not self._is_public_candidate(title, url):
                    continue
                if self._is_system_listing_url(url):
                    continue
                urls.add(url)
                titles.setdefault(url, title)
            common = urls if common is None else common & urls

        if not common:
            return []

        return [
            self._listing_article(titles[url], url, required)
            for url in sorted(common, key=lambda item: titles[item].casefold())
        ]

    def _parse_tagged_pages(self, html: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(html, "lxml")
        scope = soup.select_one("#tagged-pages-list")

        if scope is None:
            raise UpstreamContentError(
                "Источник не вернул список страниц для выбранного "
                "тега. Структура сайта может измениться."
            )

        candidates: list[tuple[str, str]] = []
        for anchor in scope.select("a[href]"):
            title = anchor.get_text(" ", strip=True)
            href = anchor["href"]
            if not title or self._is_edit_link(title, href):
                continue
            try:
                candidates.append((title, self._normalise_url(href)))
            except ValueError:
                continue
        return candidates

    async def search_content(
        self,
        query: str,
        *,
        limit: int = 50,
    ) -> list[Article]:
        """Search public articles by text and return relevance-ranked results."""
        normalized = query.casefold().strip()

        if not normalized:
            return []

        result_limit = max(0, limit)

        cached = self._search_cache.get(normalized)
        if cached and cached.is_fresh():
            return list(cached.value)[:result_limit]

        started_at = monotonic()
        articles = await self.public_articles()
        found = [
            article
            for article in articles
            if (
                normalized in article.text.casefold()
                or normalized in article.title.casefold()
            )
        ]

        def relevance(article: Article) -> int:
            score = article.text.casefold().count(normalized)
            if normalized in article.title.casefold():
                score += 10
            return score

        found.sort(
            key=lambda article: (
                -relevance(article),
                article.title.casefold(),
            )
        )

        self._store_cache(
            self._search_cache,
            normalized,
            found,
            self.SEARCH_CACHE_TTL,
            self.MAX_SEARCH_CACHE_ENTRIES,
        )

        logger.info(
            "wiki_search cache_hit=false query_length=%s "
            "articles_loaded=%s result_count=%s duration_ms=%s",
            len(normalized),
            len(articles),
            len(found),
            round((monotonic() - started_at) * 1000),
        )

        return found[:result_limit]

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _page_id_from_url(url: str) -> str:
        path = unquote(urlsplit(url).path).strip("/")
        return path.split("/", 1)[0]

    @staticmethod
    def page_category(page_id: str) -> str:
        value = unquote(page_id).strip("/").split("/", 1)[0]
        if ":" in value:
            return value.split(":", 1)[0].casefold()
        return "_default"

    @staticmethod
    def _parse_page_options(soup: BeautifulSoup) -> dict[str, Any]:
        node = soup.select_one("#page-options-container")
        raw = node.get("data-config") if node else None
        if not raw:
            return {}
        try:
            parsed = json.loads(html_lib.unescape(str(raw)))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _parse_russian_datetime(text: str) -> datetime | None:
        months = {
            "январ": 1,
            "феврал": 2,
            "март": 3,
            "апрел": 4,
            "ма": 5,
            "июн": 6,
            "июл": 7,
            "август": 8,
            "сентябр": 9,
            "октябр": 10,
            "ноябр": 11,
            "декабр": 12,
        }
        match = re.search(
            r"(\d{1,2})\s+([A-Za-zА-Яа-я.]+)\s+(\d{4}),\s*(\d{1,2}):(\d{2})",
            text,
        )
        if not match:
            return None
        day, month_raw, year, hour, minute = match.groups()
        month_key = month_raw.casefold().replace(".", "")
        month = next(
            (
                number
                for prefix, number in months.items()
                if month_key.startswith(prefix)
            ),
            None,
        )
        if month is None:
            return None
        try:
            return datetime(
                int(year),
                month,
                int(day),
                int(hour),
                int(minute),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None

    def _parse_article_chrome(
        self,
        soup: BeautifulSoup,
    ) -> tuple[str, str, datetime | None, int | None]:
        author_node = soup.select_one(
            ".author-block-container .printuser[data-user-name]"
        )
        author = ""
        if author_node is not None:
            author = str(author_node.get("data-user-name") or "").strip()
        if not author:
            fallback = soup.select_one(".author-block-container .printuser")
            author = fallback.get_text(" ", strip=True) if fallback else ""

        info = soup.select_one("#page-info")
        info_text = info.get_text(" ", strip=True) if info else ""
        last_edit = ""
        edit_match = re.search(
            r"Последняя правка:\s*(.+?)(?:\s*\(\d+\s+дн|\s*$)",
            info_text,
        )
        if edit_match:
            last_edit = edit_match.group(1).strip(" ,")
        elif info_text:
            last_edit = info_text

        version = None
        version_match = re.search(r"версия страницы:\s*(\d+)", info_text, re.I)
        if version_match:
            version = int(version_match.group(1))

        stamp = None
        odate = soup.select_one("#page-info .odate[data-timestamp]")
        if odate and odate.get("data-timestamp"):
            try:
                millis = int(str(odate.get("data-timestamp")))
                stamp = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                stamp = None
        if stamp is None and last_edit:
            stamp = self._parse_russian_datetime(last_edit)

        return author, last_edit, stamp, version

    def _csrf_from_jar(self) -> str:
        if self._session is None:
            return self._csrf_token
        for cookie in self._session.cookie_jar:
            if cookie.key == "csrftoken" and cookie.value:
                return str(cookie.value)
        return self._csrf_token

    @staticmethod
    def _is_anubis_page(html: str) -> bool:
        if not html:
            return False
        if 'id="anubis_challenge"' in html or "anubis_challenge" in html:
            return True
        plain = html_lib.unescape(html)
        return "making sure you're not a bot" in plain.casefold()

    @staticmethod
    def _anubis_pow(random_data: str, difficulty: int) -> tuple[str, int]:
        """SHA-256(data+nonce) with `difficulty` leading zero hex nibbles."""
        needed_bytes = difficulty // 2
        odd_nibble = difficulty % 2 == 1
        nonce = 0
        prefix = random_data.encode()
        while True:
            digest = hashlib.sha256(prefix + str(nonce).encode()).digest()
            if digest[:needed_bytes] == b"\x00" * needed_bytes and (
                not odd_nibble or digest[needed_bytes] >> 4 == 0
            ):
                return digest.hex(), nonce
            nonce += 1

    def _parse_anubis_challenge(self, html: str) -> dict[str, Any] | None:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("#anubis_challenge")
        raw = node.get_text() if node is not None else ""
        if not raw.strip():
            return None
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(blob, dict):
            return None
        challenge = blob.get("challenge")
        if not isinstance(challenge, dict):
            challenge = blob
        rules = blob.get("rules") if isinstance(blob.get("rules"), dict) else {}
        random_data = str(challenge.get("randomData") or "")
        ident = str(challenge.get("id") or "")
        try:
            difficulty = int(
                challenge.get("difficulty") or rules.get("difficulty") or 0
            )
        except (TypeError, ValueError):
            difficulty = 0
        if not random_data or not ident or difficulty <= 0:
            return None
        return {
            "id": ident,
            "randomData": random_data,
            "difficulty": difficulty,
        }

    async def _pass_anubis(self, html: str, *, redir: str) -> None:
        if self._session is None:
            raise UpstreamUnavailableError("HTTP client session is unavailable.")
        parsed = self._parse_anubis_challenge(html)
        if parsed is None:
            raise UpstreamAccessError(
                "Anubis blocked the page and the challenge could not be parsed. "
                "Allowlist the bot User-Agent or set WIKI_ANUBIS_COOKIE."
            )
        started = monotonic()
        digest, nonce = self._anubis_pow(
            parsed["randomData"], parsed["difficulty"]
        )
        elapsed_ms = round((monotonic() - started) * 1000)
        params = {
            "id": parsed["id"],
            "response": digest,
            "nonce": str(nonce),
            "redir": redir,
            "elapsedTime": str(elapsed_ms),
        }
        pass_url = f"{self.base_url}/.within.website/x/cmd/anubis/api/pass-challenge"
        logger.info(
            "wiki_anubis_solve difficulty=%s nonce=%s duration_ms=%s",
            parsed["difficulty"],
            nonce,
            elapsed_ms,
        )
        async with self._session.get(
            pass_url,
            params=params,
            allow_redirects=True,
        ) as response:
            await response.read()
            if response.status >= 400:
                raise UpstreamAccessError(
                    f"Anubis rejected the solved challenge (HTTP {response.status}). "
                    "Allowlist the bot User-Agent or set WIKI_ANUBIS_COOKIE."
                )
        logger.info("wiki_anubis_ok")

    def _auth_headers(self, *, referer: str | None = None) -> dict[str, str]:
        token = self._csrf_from_jar() or self._csrf_token
        headers = {
            "Referer": referer or f"{self.base_url}/",
            "Origin": self.base_url,
        }
        if token:
            headers["X-CSRFToken"] = token
        return headers

    async def login(self) -> str:
        """Sign in through the RuFoundation HTML form. Returns the username."""
        if not self.config.has_credentials:
            raise ConfigurationErrorProxy(
                "WIKI_USERNAME and WIKI_PASSWORD are required for FamiliarBot"
            )

        await self.start()
        if self._session is None:
            raise UpstreamUnavailableError("HTTP client session is unavailable.")

        login_url = f"{self.config.login_url}?to={self.base_url}/"
        logger.info("wiki_login_start user=%s", self.config.username)
        async with self._session.get(login_url, allow_redirects=True) as response:
            body = await response.text(errors="replace")
            status = response.status
        if self._is_anubis_page(body):
            await self._pass_anubis(body, redir=login_url)
            async with self._session.get(login_url, allow_redirects=True) as response:
                body = await response.text(errors="replace")
                status = response.status
        if self._is_anubis_page(body):
            logger.error(
                "wiki_login_failed user=%s reason=anubis_challenge http=%s",
                self.config.username,
                status,
            )
            raise UpstreamAccessError(
                "Anubis blocked the login page. Set WIKI_ANUBIS_COOKIE "
                "or allowlist the bot User-Agent."
            )
        if status >= 400:
            logger.error(
                "wiki_login_failed user=%s reason=login_page_http http=%s",
                self.config.username,
                status,
            )
            raise UpstreamUnavailableError(
                f"Login page unavailable (HTTP {status})."
            )

        soup = BeautifulSoup(body, "lxml")
        token_node = soup.select_one('input[name="csrfmiddlewaretoken"]')
        form_token = str(token_node.get("value") or "") if token_node else ""
        cookie_token = self._csrf_from_jar()
        if not form_token and not cookie_token:
            logger.error(
                "wiki_login_failed user=%s reason=missing_csrf",
                self.config.username,
            )
            raise UpstreamContentError(
                "Login form has no csrfmiddlewaretoken."
            )
        self._csrf_token = cookie_token or form_token

        payload = {
            "csrfmiddlewaretoken": form_token or cookie_token,
            "username": self.config.username,
            "password": self.config.password,
        }
        async with self._session.post(
            login_url,
            data=payload,
            headers=self._auth_headers(referer=login_url),
            allow_redirects=True,
        ) as response:
            landed = await response.text(errors="replace")
            if response.status >= 400:
                logger.error(
                    "wiki_login_failed user=%s reason=login_post_http http=%s",
                    self.config.username,
                    response.status,
                )
                raise UpstreamAccessError(
                    f"Wiki login failed (HTTP {response.status})."
                )

        username = self._read_login_status(landed)
        if not username:
            async with self._session.get(
                self.base_url + "/",
                headers=self._auth_headers(),
            ) as home:
                landed = await home.text(errors="replace")
                username = self._read_login_status(landed)

        if not username:
            reason = self._diagnose_login_failure(landed)
            logger.error(
                "wiki_login_failed user=%s reason=%s",
                self.config.username,
                reason,
            )
            raise UpstreamAccessError(
                "Wiki login did not produce a session. Check WIKI_USERNAME "
                "and WIKI_PASSWORD."
            )

        self._csrf_token = self._csrf_from_jar() or self._csrf_token
        self._logged_in = True
        self._login_username = username
        logger.info("wiki_login_ok user=%s", username)
        return username

    @staticmethod
    def _diagnose_login_failure(html: str) -> str:
        if self._is_anubis_page(html):
            return "anubis_challenge"
        soup = BeautifulSoup(html, "lxml")
        if soup.select_one('form input[name="password"]'):
            return "wrong_credentials"
        text = soup.get_text(" ", strip=True).casefold()
        if "неверн" in text or "incorrect" in text or "invalid password" in text:
            return "wrong_credentials"
        return "no_session"

    @staticmethod
    def _read_login_status(html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("#login-status")
        raw = node.get("data-config") if node else None
        if not raw:
            return ""
        try:
            data = json.loads(html_lib.unescape(str(raw)))
        except json.JSONDecodeError:
            return ""
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            return ""
        return str(user.get("username") or user.get("name") or "").strip()

    async def fetch_history(self, page_id: str) -> list[HistoryEntry]:
        """Load article history the same way the History control does."""
        await self.start()
        if self._session is None:
            raise UpstreamUnavailableError("HTTP client session is unavailable.")

        url = f"{self.base_url}/api/articles/{page_id}/log?from=0&to=250"
        async with self._session.get(url, headers=self._auth_headers()) as response:
            if response.status in {401, 403}:
                raise UpstreamAccessError(
                    "History endpoint refused the session."
                )
            if response.status == 404:
                return []
            if response.status >= 400:
                raise UpstreamUnavailableError(
                    f"History unavailable (HTTP {response.status})."
                )
            payload = await response.json(content_type=None)

        entries: list[HistoryEntry] = []
        for raw in payload.get("entries") or []:
            created = raw.get("createdAt")
            stamp: datetime | None = None
            if isinstance(created, str):
                try:
                    stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    stamp = None
            if stamp is None:
                continue
            user = raw.get("user") or {}
            username = ""
            if isinstance(user, dict):
                username = str(user.get("username") or user.get("name") or "")
            entries.append(
                HistoryEntry(
                    kind=str(raw.get("type") or ""),
                    created_at=stamp,
                    user=username,
                    meta=raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
                )
            )
        return entries

    def last_source_edit(self, entries: list[HistoryEntry]) -> datetime | None:
        for entry in entries:
            if entry.kind in {"source", "new"}:
                return entry.created_at
        return entries[0].created_at if entries else None

    def last_category_move(
        self,
        entries: list[HistoryEntry],
        page_id: str,
    ) -> datetime | None:
        current = self.page_category(page_id)
        for entry in entries:
            if entry.kind != "name":
                continue
            new_name = str(entry.meta.get("name") or "")
            prev_name = str(entry.meta.get("prev_name") or "")
            if (
                self.page_category(new_name) == current
                and self.page_category(prev_name) != current
            ):
                return entry.created_at
        return entries[-1].created_at if entries else None

    def tag_added_at(
        self,
        entries: list[HistoryEntry],
        tag: str,
    ) -> datetime | None:
        needle = tag.casefold()
        for entry in entries:
            if entry.kind != "tags":
                continue
            added = entry.meta.get("added_tags") or []
            names = []
            for item in added:
                if isinstance(item, dict):
                    names.append(str(item.get("name") or "").casefold())
                else:
                    names.append(str(item).casefold())
            if needle in names:
                return entry.created_at
        return None

    async def set_article_tags(self, page_id: str, tags: Iterable[str]) -> None:
        """Save tags with the same PUT the site editor uses."""
        if not self._logged_in:
            raise UpstreamAccessError("Wiki session is not signed in.")
        await self.start()
        if self._session is None:
            raise UpstreamUnavailableError("HTTP client session is unavailable.")

        unique_tags = list(dict.fromkeys(str(tag) for tag in tags if str(tag)))
        url = f"{self.base_url}/api/articles/{page_id}"
        self._csrf_token = self._csrf_from_jar() or self._csrf_token
        async with self._session.put(
            url,
            json={"pageId": page_id, "tags": unique_tags},
            headers={
                **self._auth_headers(referer=f"{self.base_url}/{page_id}"),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        ) as response:
            if response.status in {401, 403}:
                raise UpstreamAccessError(
                    f"Tag save refused (HTTP {response.status}). "
                    "The FamiliarBot account needs editor rights."
                )
            if response.status >= 400:
                body = await response.text(errors="replace")
                logger.error(
                    "wiki_tag_save_failed page=%s status=%s",
                    page_id,
                    response.status,
                )
                raise UpstreamUnavailableError(
                    f"Tag save failed (HTTP {response.status}): {body[:180]}"
                )
        self._article_cache.pop(f"{self.base_url}/{page_id}", None)
        self._article_cache.pop(f"{self.base_url}/{page_id.lstrip('/')}", None)
        logger.info("wiki_tags_saved page=%s count=%s", page_id, len(unique_tags))

    @staticmethod
    def recategorize_page_id(page_id: str, category: str | None) -> str:
        """sandbox:ritual-6 → deleted:ritual-6 or ritual-6."""
        value = unquote(page_id).strip().strip("/")
        slug = value.split(":", 1)[1] if ":" in value else value
        if category:
            return f"{category}:{slug}"
        return slug

    async def _json_call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        referer: str | None = None,
    ) -> Any:
        if not self._logged_in:
            raise UpstreamAccessError("Wiki session is not signed in.")
        await self.start()
        if self._session is None:
            raise UpstreamUnavailableError("HTTP client session is unavailable.")
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        self._csrf_token = self._csrf_from_jar() or self._csrf_token
        headers = {
            **self._auth_headers(referer=referer or f"{self.base_url}/"),
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        async with self._session.request(
            method,
            url,
            json=payload,
            headers=headers,
        ) as response:
            body = await response.text(errors="replace")
            if response.status in {401, 403}:
                raise UpstreamAccessError(
                    f"Wiki write refused (HTTP {response.status}) {method} {path}."
                )
            if response.status >= 400:
                logger.error(
                    "wiki_json_failed method=%s path=%s status=%s",
                    method,
                    path,
                    response.status,
                )
                raise UpstreamUnavailableError(
                    f"Wiki write failed (HTTP {response.status}): {body[:180]}"
                )
            if not body.strip():
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError as error:
                raise UpstreamContentError("Wiki JSON response is not valid.") from error

    async def fetch_article_record(self, page_id: str) -> dict[str, Any]:
        data = await self._json_call(
            "GET",
            f"/api/articles/{page_id}",
            referer=f"{self.base_url}/{page_id}",
        )
        if not isinstance(data, dict):
            raise UpstreamContentError("Article record is not an object.")
        return data

    async def create_article(
        self,
        page_id: str,
        *,
        title: str,
        source: str,
        tags: Iterable[str] = (),
        comment: str = "",
    ) -> str:
        unique_tags = list(dict.fromkeys(str(tag) for tag in tags if str(tag)))
        data = await self._json_call(
            "POST",
            "/api/articles/new",
            {
                "pageId": page_id,
                "title": title,
                "source": source,
                "tags": unique_tags,
                "comment": comment,
                "isNew": True,
            },
            referer=f"{self.base_url}/",
        )
        created = str(data.get("pageId") or page_id)
        logger.info("wiki_article_created page=%s", created)
        return created

    async def move_article(
        self,
        page_id: str,
        new_page_id: str,
        tags: Iterable[str],
    ) -> str:
        """Same PUT the site rename/delete dialog uses (forcePageId)."""
        unique_tags = list(dict.fromkeys(str(tag) for tag in tags if str(tag)))
        data = await self._json_call(
            "PUT",
            f"/api/articles/{page_id}",
            {
                "pageId": new_page_id,
                "tags": unique_tags,
                "forcePageId": True,
            },
            referer=f"{self.base_url}/{page_id}",
        )
        moved = str(data.get("pageId") or new_page_id)
        self._article_cache.pop(f"{self.base_url}/{page_id}", None)
        self._article_cache.pop(f"{self.base_url}/{moved}", None)
        logger.info("wiki_article_moved from=%s to=%s", page_id, moved)
        return moved

    async def delete_article(self, page_id: str) -> None:
        await self._json_call(
            "DELETE",
            f"/api/articles/{page_id}",
            {},
            referer=f"{self.base_url}/{page_id}",
        )
        self._article_cache.pop(f"{self.base_url}/{page_id}", None)
        logger.info("wiki_article_deleted page=%s", page_id)

    async def comment_thread_id(
        self,
        page_id: str,
        *,
        comment_path: str | None = None,
    ) -> int | None:
        paths: list[str] = []
        if comment_path:
            paths.append(comment_path.lstrip("/"))
        paths.append(f"{page_id}/comments/show")
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            try:
                html = await self.fetch_html(f"{self.base_url}/{path}")
            except WikiError:
                continue
            thread_id = self._thread_id_from_html(html)
            if thread_id is not None:
                return thread_id
        return None

    @staticmethod
    def _thread_id_from_html(html: str) -> int | None:
        soup = BeautifulSoup(html, "lxml")
        mount = soup.select_one(".w-forum-new-post[data-config]")
        if mount is not None:
            raw = mount.get("data-config")
            try:
                cfg = json.loads(html_lib.unescape(str(raw)))
            except json.JSONDecodeError:
                cfg = None
            if isinstance(cfg, dict) and cfg.get("threadId") is not None:
                try:
                    return int(str(cfg["threadId"]))
                except (TypeError, ValueError):
                    pass
        node = soup.select_one("[data-forum-thread-path-params]")
        raw = node.get("data-forum-thread-path-params") if node else None
        if raw:
            try:
                params = json.loads(html_lib.unescape(str(raw)))
            except json.JSONDecodeError:
                params = None
            if isinstance(params, dict) and params.get("t") is not None:
                try:
                    return int(str(params["t"]))
                except (TypeError, ValueError):
                    pass
        button = soup.select_one("#discuss-button")
        href = button.get("href") if button else None
        if href:
            match = re.search(r"/forum/t-(\d+)", str(href))
            if match:
                return int(match.group(1))
        match = re.search(r"/forum/t-(\d+)/", html)
        if match:
            return int(match.group(1))
        return None

    async def post_forum(
        self,
        thread_id: int,
        *,
        name: str,
        source: str,
        referer: str | None = None,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "name": name,
            "source": source,
        }
        if reply_to is not None:
            params["replyTo"] = reply_to
        data = await self._json_call(
            "POST",
            "/api/modules",
            {
                "module": "forumnewpost",
                "method": "submit",
                "params": params,
            },
            referer=referer or f"{self.base_url}/forum/t-{thread_id}",
        )
        logger.info("wiki_forum_posted thread=%s", thread_id)
        return data if isinstance(data, dict) else {}

    async def post_discussion(
        self,
        page_id: str,
        *,
        name: str,
        source: str,
        comment_path: str | None = None,
    ) -> None:
        candidates = []
        if comment_path:
            candidates.append(comment_path.lstrip("/"))
        candidates.append(f"{page_id}/comments/show")
        thread_id: int | None = None
        referer_path = candidates[0]
        compose = False
        for path in candidates:
            try:
                html = await self.fetch_html(f"{self.base_url}/{path}")
            except WikiError:
                continue
            soup = BeautifulSoup(html, "lxml")
            compose = soup.select_one(".w-forum-new-post") is not None
            thread_id = self._thread_id_from_html(html)
            if thread_id is not None:
                referer_path = path
                break
        if thread_id is None:
            logger.warning("wiki_discussion_missing page=%s", page_id)
            return
        if not compose:
            logger.warning(
                "wiki_discussion_no_compose page=%s thread=%s path=%s",
                page_id,
                thread_id,
                referer_path,
            )
        await self.post_forum(
            thread_id,
            name=name,
            source=source,
            referer=f"{self.base_url}/{referer_path}",
        )
        logger.info(
            "wiki_discussion_posted page=%s thread=%s path=%s",
            page_id,
            thread_id,
            referer_path,
        )

    async def list_category_pages(
        self,
        categories: Iterable[str],
    ) -> list[tuple[str, str]]:
        """List pages whose URL category matches, e.g. /sandbox:page-name."""
        wanted = {item.casefold().strip() for item in categories if item.strip()}
        listing_urls = [self.all_pages_url]
        if wanted & {"sandbox"}:
            listing_urls = [
                f"{self.base_url}/cauldron-articles",
                self.all_pages_url,
            ]
        if wanted & {"draft"}:
            listing_urls[0:0] = [
                f"{self.base_url}/system:page-tags/tag/статус:черновик",
                f"{self.base_url}/system:my-drafts",
            ]
        collected: list[tuple[str, str]] = []
        seen: set[str] = set()

        for listing in listing_urls:
            try:
                html = await self.fetch_html(listing)
            except WikiError:
                continue
            for title, url in self._parse_list_links(html):
                if self.page_category(self._page_id_from_url(url)) not in wanted:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                collected.append((title, url))

        if collected:
            return collected

        for title, url in await self.all_links():
            if self.page_category(self._page_id_from_url(url)) in wanted:
                collected.append((title, url))
        return collected

    async def find_by_author(
        self,
        query: str,
        *,
        limit: int = 50,
    ) -> list[Article]:
        """Find public articles by author name."""
        normalized = query.casefold().strip()
        if not normalized:
            return []

        result_limit = max(0, limit)
        cache_key = f"author:{normalized}"
        cached = self._search_cache.get(cache_key)
        if cached and cached.is_fresh():
            return list(cached.value)[:result_limit]

        candidates: list[tuple[str, str]] = []
        author_url = (
            f"{self.base_url}/system:articles-by-author/created_by/{query.strip()}"
        )
        try:
            html = await self.fetch_html(author_url)
            candidates = [
                item
                for item in self._parse_list_links(html)
                if self._is_public_candidate(*item)
            ]
        except WikiError:
            candidates = []

        if not candidates:
            corpus = await self.public_articles()
            found = [
                article
                for article in corpus
                if normalized in (article.author or "").casefold()
            ]
        else:
            corpus = self._fresh_corpus()
            if corpus is not None:
                wanted = {url for _, url in candidates}
                found = [
                    article
                    for article in corpus
                    if article.url in wanted
                    or normalized in (article.author or "").casefold()
                ]
                if wanted:
                    found = [
                        article
                        for article in found
                        if article.url in wanted
                    ]
            else:
                articles = await self._get_articles_in_batches(candidates)
                found = [
                    article
                    for article in articles
                    if (
                        normalized in (article.author or "").casefold()
                        and not (article.tags & SYSTEM_TAGS)
                    )
                ]

        found.sort(key=lambda article: article.title.casefold())

        self._store_cache(
            self._search_cache,
            cache_key,
            found,
            self.SEARCH_CACHE_TTL,
            self.MAX_SEARCH_CACHE_ENTRIES,
        )
        return found[:result_limit]
