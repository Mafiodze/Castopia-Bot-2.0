from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from cogs.page_parsing import Article, HistoryEntry, WikiClient
from familiarbot.config import FamiliarConfig, drop_named_tag, has_named_tag
from familiarbot.moderator import Moderator

import unittest


def art(**kwargs: object) -> Article:
    data = dict(
        title="Ритуал",
        url="https://castopia.site/sandbox:ritual",
        text="Текст.",
        tags=frozenset({"статус:проверка"}),
        author="A",
        page_id="sandbox:ritual",
        rating=2.0,
        votes_count=4,
        popularity=40,
    )
    data.update(kwargs)
    return Article(**data)  # type: ignore[arg-type]


class FakeWiki:
    base_url = "https://castopia.site"

    def __init__(self) -> None:
        self.saved: list[tuple[str, set[str]]] = []
        self.moved: list[tuple[str, str, set[str]]] = []
        self.discussed: list[str] = []
        self.forum: list[tuple[int, str]] = []
        self.history: list[HistoryEntry] = []
        recategorize = WikiClient.recategorize_page_id
        self.recategorize_page_id = staticmethod(recategorize)

    def tag_added_at(self, entries, tag):
        return WikiClient.tag_added_at(self, entries, tag)

    def last_category_move(self, entries, page_id):
        return WikiClient.last_category_move(self, entries, page_id)

    async def set_article_tags(self, page_id: str, tags: set[str]) -> None:
        self.saved.append((page_id, set(tags)))

    async def move_article(self, page_id: str, dest: str, tags: set[str]) -> None:
        self.moved.append((page_id, dest, set(tags)))

    async def post_discussion(self, page_id: str, **_: object) -> None:
        self.discussed.append(page_id)

    async def post_forum(self, thread_id: int, **kwargs: object) -> dict:
        self.forum.append((thread_id, str(kwargs.get("source", ""))))
        return {}

    async def fetch_history(self, _page_id: str) -> list[HistoryEntry]:
        return list(self.history)


class ModeratorLogicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.wiki = FakeWiki()
        self.cfg = FamiliarConfig(dry_run=False)
        self.mod = Moderator(self.wiki, self.cfg)  # type: ignore[arg-type]

    def test_bands(self) -> None:
        self.assertTrue(self.mod._is_critical(art(rating=2.9, votes_count=4)))
        self.assertFalse(self.mod._is_critical(art(rating=3.0, votes_count=4)))
        self.assertTrue(self.mod._is_grayzone(art(rating=3.0, votes_count=4)))
        self.assertTrue(self.mod._is_grayzone(art(rating=3.9, votes_count=4)))
        self.assertFalse(self.mod._is_grayzone(art(rating=4.0, votes_count=4)))
        self.assertTrue(self.mod._is_approval(art(rating=4.0, votes_count=4)))
        self.assertFalse(self.mod._is_approval(art(rating=5.0, votes_count=3)))
        self.assertFalse(self.mod._is_critical(art(rating=1.0, votes_count=3)))

    def test_skip_archive_and_main(self) -> None:
        self.assertTrue(self.mod._should_skip(art(tags=frozenset({"архив"}))))
        self.assertTrue(
            self.mod._should_skip(art(tags=frozenset({"основное_пространство"})))
        )
        self.assertFalse(
            self.mod._should_skip(art(tags=frozenset({"статус:проверка"})))
        )

    async def test_mark_for_three_paths(self) -> None:
        cases = [
            (art(rating=2.5, votes_count=4), "котел:к_удалению"),
            (art(rating=3.5, votes_count=4), "котел:к_тегованию"),
            (art(rating=4.2, votes_count=4), "котел:рейтинг_набран"),
        ]
        for article, tag in cases:
            self.wiki.saved.clear()
            self.mod._load_cauldron = AsyncMock(return_value=[article])  # type: ignore[method-assign]
            await self.mod.mark_for()
            self.assertEqual(self.wiki.saved[0][0], article.page_id)
            self.assertTrue(has_named_tag(self.wiki.saved[0][1], tag))
            self.assertEqual(self.wiki.discussed, [article.page_id])
            self.wiki.discussed.clear()

    async def test_mark_for_skips_low_votes_and_existing_decision(self) -> None:
        self.mod._load_cauldron = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                art(rating=1.0, votes_count=3),
                art(
                    rating=1.0,
                    votes_count=10,
                    tags=frozenset({"статус:проверка", "котел:к_удалению"}),
                ),
            ]
        )
        await self.mod.mark_for()
        self.assertEqual(self.wiki.saved, [])

    async def test_unmark_when_rating_recovers(self) -> None:
        article = art(
            rating=4.5,
            votes_count=8,
            tags=frozenset({"статус:проверка", "котел:к_удалению"}),
        )
        self.mod._load_cauldron = AsyncMock(return_value=[article])  # type: ignore[method-assign]
        await self.mod.resolve_deletion()
        saved_tags = self.wiki.saved[0][1]
        self.assertFalse(has_named_tag(saved_tags, "котел:к_удалению"))
        self.assertEqual(self.wiki.moved, [])

    async def test_archive_writes_deleted_and_journal(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=2)
        article = art(
            rating=1.5,
            votes_count=6,
            tags=frozenset({"статус:проверка", "котел:к_удалению", "тип:ритуал"}),
        )
        self.wiki.history = [
            HistoryEntry(
                kind="tags",
                created_at=old,
                user="FamiliarBot",
                meta={"added_tags": ["котел:к_удалению"]},
            )
        ]
        self.mod._load_cauldron = AsyncMock(return_value=[article])  # type: ignore[method-assign]
        await self.mod.resolve_deletion()
        self.assertEqual(self.wiki.moved[0][1], "deleted:ritual")
        self.assertTrue(has_named_tag(self.wiki.moved[0][2], "статус:удалено"))
        self.assertFalse(has_named_tag(self.wiki.moved[0][2], "котел:к_удалению"))
        self.assertTrue(has_named_tag(self.wiki.moved[0][2], "тип:ритуал"))
        self.assertEqual(self.wiki.forum[0][0], 100)
        self.assertIn("deleted:ritual", self.wiki.forum[0][1])

    async def test_transfer_never_renames_to_main(self) -> None:
        article = art(
            rating=2.0,
            votes_count=4,
            tags=frozenset(
                {"статус:проверка", "котел:рейтинг_набран", "котел:к_переносу"}
            ),
        )
        self.mod._load_cauldron = AsyncMock(return_value=[article])  # type: ignore[method-assign]
        await self.mod.resolve_transfer()
        self.assertEqual(self.wiki.moved, [])
        saved = self.wiki.saved[0][1]
        self.assertFalse(has_named_tag(saved, "котел:к_переносу"))

    async def test_ensure_check_tag(self) -> None:
        article = art(tags=frozenset())
        self.mod._load_cauldron = AsyncMock(return_value=[article])  # type: ignore[method-assign]
        await self.mod.ensure_check_tag()
        self.assertTrue(has_named_tag(self.wiki.saved[0][1], "статус:проверка"))

    def test_drop_named_tag_matches_leaf(self) -> None:
        tags = drop_named_tag({"котел:к_удалению", "тип:ритуал"}, "к_удалению")
        self.assertEqual(tags, {"тип:ритуал"})
