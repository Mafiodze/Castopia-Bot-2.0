from __future__ import annotations

import unittest

from discord.ext import commands

from cogs.dsc import DscCog, SearchResultsView, _RateLimit, _RateLimiter
from cogs.page_parsing import Article


class DiscordUiTests(unittest.IsolatedAsyncioTestCase):
    def test_public_commands_are_hybrid(self) -> None:
        for command in (
            DscCog.show_help,
            DscCog.random_page,
            DscCog.search_title,
            DscCog.search_tags,
            DscCog.search_content,
            DscCog.search_author,
        ):
            self.assertIsInstance(command, commands.HybridCommand)
            self.assertIsNotNone(command.app_command)

    async def test_search_view_limits_results_to_owner_and_page_size(self) -> None:
        articles = [
            Article(title=f"Article {number}", url=f"https://castopia.site/{number}", text="Text", tags=frozenset())
            for number in range(6)
        ]
        view = SearchResultsView(owner_id=42, results=articles, query="text")
        self.assertEqual(view.total_pages, 2)
        self.assertTrue(view.previous_page.disabled)
        self.assertFalse(view.next_page.disabled)
        self.assertEqual(len(view.create_embed().fields), 5)

    async def test_rate_limiter_is_shared_by_invocation_style(self) -> None:
        limiter = _RateLimiter({"search": _RateLimit(1, 60)})
        self.assertEqual(await limiter.retry_after(42, "search"), 0)
        self.assertGreater(await limiter.retry_after(42, "search"), 0)

    async def test_rate_limiter_different_users_independent(self) -> None:
        """Rate limits should be per-user."""
        limiter = _RateLimiter({"search": _RateLimit(1, 60)})
        user1, user2 = 111, 222
        # User 1 uses first request
        self.assertEqual(await limiter.retry_after(user1, "search"), 0)
        # User 1 is now limited
        self.assertGreater(await limiter.retry_after(user1, "search"), 0)
        # User 2 should be independent - can use one request
        self.assertEqual(await limiter.retry_after(user2, "search"), 0)
        # User 2 is now limited
        self.assertGreater(await limiter.retry_after(user2, "search"), 0)

    async def test_rate_limiter_different_commands_independent(self) -> None:
        """Rate limits should be per-command."""
        limiter = _RateLimiter({
            "search": _RateLimit(1, 60),
            "randompage": _RateLimit(2, 60),
        })
        user = 123
        self.assertEqual(await limiter.retry_after(user, "search"), 0)
        self.assertGreater(await limiter.retry_after(user, "search"), 0)
        # Different command should not be limited
        self.assertEqual(await limiter.retry_after(user, "randompage"), 0)
        self.assertEqual(await limiter.retry_after(user, "randompage"), 0)
        self.assertGreater(await limiter.retry_after(user, "randompage"), 0)
