from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from cogs.constants import WikiConfig
from cogs.page_parsing import (
    Article,
    UpstreamAccessError,
    UpstreamContentError,
    UpstreamNotFoundError,
    WikiClient,
)


def make_client() -> WikiClient:
    return WikiClient(
        WikiConfig(
            base_url="https://castopia.site",
            user_agent="CastopiaBot test",
            max_concurrent_requests=2,
        )
    )


class FakeResponse:
    def __init__(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def text(self, **_: object) -> str:
        return self._body


class FakeSession:
    closed = False

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.calls = 0

    def get(self, *_: object, **__: object) -> FakeResponse:
        self.calls += 1
        return next(self.responses)


class WikiClientTests(unittest.IsolatedAsyncioTestCase):
    def test_list_parser_uses_nonempty_box_and_skips_edit_links(self) -> None:
        client = make_client()
        html = """
        <div id="page-content">
          <div class="list-pages-box w-list-pages"></div>
          <div class="list-pages-box w-list-pages">
            <a href="/alpha">Alpha</a>
            <a href="/alpha/edit/true">Редактировать</a>
            <a href="/beta/edit/true">Not an article</a>
            <a href="/beta">Beta</a>
          </div>
        </div>
        """
        self.assertEqual(
            client._parse_list_links(html),
            [
                ("Alpha", "https://castopia.site/alpha"),
                ("Beta", "https://castopia.site/beta"),
            ],
        )

    def test_pagination_parser_uses_last_number(self) -> None:
        self.assertEqual(
            WikiClient._parse_total_pages('<span class="pager-no">page 1 of 5</span>'), 5
        )

    async def test_empty_article_listing_is_diagnostic_error(self) -> None:
        client = make_client()
        client.fetch_html = AsyncMock(return_value='<div id="page-content"></div>')
        with self.assertRaises(UpstreamContentError):
            await client.all_links()

    async def test_429_retries_and_403_never_retries(self) -> None:
        client = make_client()
        retry_session = FakeSession(
            [
                FakeResponse(429, headers={"Retry-After": "0"}),
                FakeResponse(200, "<html>ok</html>"),
            ]
        )
        client._session = retry_session  # type: ignore[assignment]
        self.assertEqual(
            await client._request_html("https://castopia.site/example"), "<html>ok</html>"
        )
        self.assertEqual(retry_session.calls, 2)

        blocked_client = make_client()
        blocked_session = FakeSession([FakeResponse(403)])
        blocked_client._session = blocked_session  # type: ignore[assignment]
        with self.assertRaises(UpstreamAccessError):
            await blocked_client._request_html("https://castopia.site/example")
        self.assertEqual(blocked_session.calls, 1)

    async def test_page_response_is_cached(self) -> None:
        client = make_client()
        session = FakeSession([FakeResponse(200, "<html>ok</html>")])
        client._session = session  # type: ignore[assignment]
        self.assertEqual(await client.fetch_html("/cached"), "<html>ok</html>")
        self.assertEqual(await client.fetch_html("/cached"), "<html>ok</html>")
        self.assertEqual(session.calls, 1)

    async def test_random_article_skips_stale_listing_link(self) -> None:
        client = make_client()
        client.all_links = AsyncMock(
            return_value=[
                ("Missing", "https://castopia.site/missing"),
                ("Present", "https://castopia.site/present"),
            ]
        )
        present = Article(
            title="Present",
            url="https://castopia.site/present",
            text="Article text",
            tags=frozenset(),
        )
        client.get_article = AsyncMock(side_effect=[UpstreamNotFoundError(), present])
        self.assertEqual((await client.random_article()).title, "Present")

    async def test_article_uses_full_tag_identifier_from_tag_link(self) -> None:
        client = make_client()
        client.fetch_html = AsyncMock(
            return_value="""
            <div id="page-content">Article text</div>
            <div class="page-tags">
              <a href="/system:page-tags/tag/%D1%81%D1%82%D0%B0%D1%82%D1%83%D1%81%3A%D0%BE%D1%81%D0%BD%D0%BE%D0%B2%D0%BD%D0%BE%D0%B5">основное</a>
            </div>
            """
        )
        article = await client.get_article("Article", "/article")
        self.assertEqual(article.tags, frozenset({"статус:основное"}))

    def test_list_parser_collects_from_all_boxes(self) -> None:
        """Parser should collect links from ALL .list-pages-box elements, not just the first."""
        client = make_client()
        html = """
        <div id="page-content">
          <div class="list-pages-box"></div>
          <div class="list-pages-box">
            <a href="/article1">Article 1</a>
          </div>
          <div class="list-pages-box">
            <a href="/article2">Article 2</a>
            <a href="/article3">Article 3</a>
          </div>
        </div>
        """
        links = client._parse_list_links(html)
        self.assertEqual(len(links), 3)
        self.assertIn(("Article 1", "https://castopia.site/article1"), links)
        self.assertIn(("Article 2", "https://castopia.site/article2"), links)
        self.assertIn(("Article 3", "https://castopia.site/article3"), links)

    def test_list_parser_handles_russian_edit_links(self) -> None:
        """Parser should skip both 'edit' and 'редактировать' links."""
        client = make_client()
        html = """
        <div id="page-content">
          <div class="list-pages-box">
            <a href="/article">Article</a>
            <a href="/article/edit/true">edit</a>
            <a href="/article/edit">Редактировать</a>
            <a href="/paths/edit/other">edit in path</a>
          </div>
        </div>
        """
        links = client._parse_list_links(html)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0], ("Article", "https://castopia.site/article"))

    async def test_all_links_validates_page_content_block(self) -> None:
        """Should raise UpstreamContentError if #page-content block is missing."""
        client = make_client()
        client.fetch_html = AsyncMock(return_value='<html><body>No page content</body></html>')
        with self.assertRaises(UpstreamContentError) as ctx:
            await client.all_links()
        self.assertIn("#page-content", str(ctx.exception))

    async def test_all_links_validates_list_boxes_exist(self) -> None:
        """Should raise UpstreamContentError if no .list-pages-box elements found."""
        client = make_client()
        client.fetch_html = AsyncMock(return_value='<div id="page-content"><p>Only text</p></div>')
        with self.assertRaises(UpstreamContentError) as ctx:
            await client.all_links()
        self.assertIn(".list-pages-box", str(ctx.exception))

    async def test_get_article_validates_content_block(self) -> None:
        """Should raise UpstreamContentError if article has no #page-content."""
        client = make_client()
        client.fetch_html = AsyncMock(return_value='<html><body>Article deleted</body></html>')
        with self.assertRaises(UpstreamContentError) as ctx:
            await client.get_article("Title", "/article")
        self.assertIn("#page-content", str(ctx.exception))

    async def test_get_article_validates_non_empty_text(self) -> None:
        """Should raise UpstreamContentError if article has no extractable text."""
        client = make_client()
        client.fetch_html = AsyncMock(
            return_value='<div id="page-content"><script>js</script></div>'
        )
        with self.assertRaises(UpstreamContentError) as ctx:
            await client.get_article("Title", "/article")
        self.assertIn("доступного текста", str(ctx.exception))

    async def test_search_content_uses_full_search_lock(self) -> None:
        """Only one fulltext search should execute at a time."""
        client = make_client()
        all_links_calls = 0

        async def counting_all_links() -> list[tuple[str, str]]:
            nonlocal all_links_calls
            all_links_calls += 1
            return [("Article", "/article")]

        client.all_links = counting_all_links  # type: ignore[assignment]
        client.get_article = AsyncMock(
            return_value=Article("Article", "https://castopia.site/article", "Query text", frozenset())
        )

        # Sequential searches should both use cache or run one at a time
        await client.search_content("query1")
        await client.search_content("query1")  # Same query, should use cache
        self.assertEqual(all_links_calls, 1)

    async def test_find_by_tags_does_not_fetch_articles(self) -> None:
        client = make_client()
        client.fetch_html = AsyncMock(
            return_value=(
                '<div id="tagged-pages-list">'
                '<a href="/alpha">Alpha</a>'
                '<a href="/system:help">Help</a>'
                "</div>"
            )
        )
        client._resolve_tags = AsyncMock(
            return_value=[
                type(
                    "ref",
                    (),
                    {
                        "identifier": "tag1",
                        "url": "https://castopia.site/tag1",
                    },
                )()
            ]
        )
        client.get_article = AsyncMock(side_effect=AssertionError("should not fetch"))
        result = await client.find_by_tags(["tag1"])
        self.assertEqual([article.url for article in result], ["https://castopia.site/alpha"])
        client.get_article.assert_not_called()
