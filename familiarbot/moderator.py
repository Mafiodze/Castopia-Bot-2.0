"""Cauldron moderation: tags, discussion, archive to deleted:."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from cogs.page_parsing import Article, WikiClient

from .config import FamiliarConfig, drop_named_tag, has_any_tag, has_named_tag, tag_leaf
from . import phrases

logger = logging.getLogger(__name__)


class Moderator:
    def __init__(self, wiki: WikiClient, config: FamiliarConfig) -> None:
        self.wiki = wiki
        self.config = config

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _enough_votes(self, article: Article) -> bool:
        votes = article.votes_count
        return votes is not None and votes >= self.config.min_votes

    def _should_skip(self, article: Article) -> bool:
        return has_any_tag(article.tags, self.config.skip_tags) or has_any_tag(
            article.tags, self.config.exclude_tags
        )

    def _is_critical(self, article: Article) -> bool:
        rating = article.rating
        if rating is None or not self._enough_votes(article):
            return False
        return rating < self.config.critical_rating

    def _is_approval(self, article: Article) -> bool:
        rating = article.rating
        if rating is None or not self._enough_votes(article):
            return False
        return rating >= self.config.approval_rating

    def _is_grayzone(self, article: Article) -> bool:
        rating = article.rating
        if rating is None or not self._enough_votes(article):
            return False
        return self.config.critical_rating <= rating < self.config.approval_rating

    def _has_decision(self, tags: set[str]) -> bool:
        return any(
            has_named_tag(tags, name)
            for name in (
                self.config.tag_deletion,
                self.config.tag_whitemark,
                self.config.tag_approved,
                self.config.tag_tagging,
            )
        )

    def _carry_tags(self, tags: set[str], extra: str) -> set[str]:
        drop = {
            tag_leaf(self.config.tag_check),
            tag_leaf(self.config.tag_deletion),
            tag_leaf(self.config.tag_whitemark),
            tag_leaf(self.config.tag_approved),
            tag_leaf(self.config.tag_tagging),
            tag_leaf(self.config.tag_deleted),
            tag_leaf(self.config.tag_main),
            "проверка",
        }
        kept = {
            tag
            for tag in tags
            if tag_leaf(tag) not in drop and not tag.casefold().startswith("котел:")
        }
        kept.add(extra)
        return kept

    async def _load_cauldron(self) -> list[Article]:
        links = await self.wiki.list_category_pages(self.config.categories)
        articles = await self.wiki._get_articles_in_batches(links)
        return [article for article in articles if not self._should_skip(article)]

    async def _discuss(self, article: Article, source: str) -> None:
        if self.config.dry_run:
            logger.info("dry_run discuss page=%s", article.page_id)
            return
        try:
            await self.wiki.post_discussion(
                article.page_id,
                name=self.config.forum_post_title,
                source=source,
                comment_path=article.comment_thread or None,
            )
        except Exception:
            logger.exception("wiki_discussion_failed page=%s", article.page_id)

    async def _save_tags(
        self,
        article: Article,
        tags: set[str],
        action: str,
        phrase: str | None = None,
    ) -> None:
        current = set(article.tags)
        if current == set(tags):
            return
        added = set(tags) - current
        removed = current - set(tags)
        source = phrases.discussion_report(phrase, added, removed)
        if self.config.dry_run:
            logger.info(
                "dry_run action=%s page=%s added=%s removed=%s",
                action,
                article.page_id,
                sorted(added),
                sorted(removed),
            )
            return
        await self.wiki.set_article_tags(article.page_id, tags)
        await self._discuss(article, source)

    async def _journal_deleted(self, prepend: str, pages: list[Article]) -> None:
        if not pages:
            return
        source = phrases.journal_body(prepend, pages)
        if self.config.dry_run:
            logger.info(
                "dry_run journal thread=%s pages=%s",
                self.config.journal_thread_id,
                [page.page_id for page in pages],
            )
            return
        try:
            await self.wiki.post_forum(
                self.config.journal_thread_id,
                name=phrases.JOURNAL_TITLE,
                source=source,
                referer=f"{self.wiki.base_url}{self.config.journal_path}",
            )
            logger.info(
                "wiki_journal_posted thread=%s count=%s",
                self.config.journal_thread_id,
                len(pages),
            )
        except Exception:
            logger.exception(
                "wiki_journal_failed thread=%s",
                self.config.journal_thread_id,
            )

    async def _archive(
        self,
        article: Article,
        *,
        discuss: str,
        action: str,
    ) -> None:
        dest = self.wiki.recategorize_page_id(
            article.page_id,
            self.config.deleted_category,
        )
        tags = self._carry_tags(set(article.tags), self.config.tag_deleted)
        head = {
            "archive_critical": phrases.JOURNAL_CRITICAL,
            "archive_tagging": phrases.JOURNAL_GRAY,
        }.get(action, phrases.JOURNAL_STALE)
        logged = Article(
            title=article.title,
            url=f"{self.wiki.base_url}/{dest}",
            text=article.text,
            tags=frozenset(tags),
            author=article.author,
            last_edit=article.last_edit,
            last_edit_at=article.last_edit_at,
            page_id=dest,
            rating=article.rating,
            votes_count=article.votes_count,
            popularity=article.popularity,
            version=article.version,
            comment_thread=article.comment_thread,
        )
        if self.config.dry_run:
            logger.info(
                "dry_run action=%s from=%s to=%s",
                action,
                article.page_id,
                dest,
            )
            await self._journal_deleted(head, [logged])
            return
        await self._discuss(
            article,
            phrases.discussion_report(
                discuss,
                {self.config.tag_deleted},
                {
                    self.config.tag_check,
                    self.config.tag_deletion,
                    self.config.tag_tagging,
                    self.config.tag_whitemark,
                    self.config.tag_approved,
                },
            ),
        )
        await self.wiki.move_article(article.page_id, dest, tags)
        await self._journal_deleted(head, [logged])
        logger.info("wiki_archived from=%s to=%s", article.page_id, dest)

    async def ensure_check_tag(self) -> None:
        for article in await self._load_cauldron():
            tags = set(article.tags)
            if has_named_tag(tags, self.config.tag_check):
                continue
            tags.add(self.config.tag_check)
            await self._save_tags(
                article,
                tags,
                "ensure_check",
                phrases.TAG_ADDED.format(tag=self.config.tag_check),
            )

    async def mark_for(self) -> None:
        for article in await self._load_cauldron():
            tags = set(article.tags)
            if self._has_decision(tags):
                continue
            if self._is_critical(article):
                tags.add(self.config.tag_deletion)
                await self._save_tags(
                    article,
                    tags,
                    "mark_deletion",
                    phrases.deletion_phrase(self._now()),
                )
                continue
            if self._is_approval(article):
                tags.add(self.config.tag_whitemark)
                await self._save_tags(
                    article,
                    tags,
                    "whitemark",
                    phrases.WHITMARK,
                )
                continue
            if self._is_grayzone(article):
                tags.add(self.config.tag_tagging)
                await self._save_tags(
                    article,
                    tags,
                    "mark_tagging",
                    phrases.grayzone_phrase(article),
                )

    async def resolve_deletion(self) -> None:
        archived: list[Article] = []
        for article in await self._load_cauldron():
            tags = set(article.tags)
            if not has_named_tag(tags, self.config.tag_deletion):
                continue
            if not self._is_critical(article):
                tags = drop_named_tag(tags, self.config.tag_deletion)
                await self._save_tags(
                    article,
                    tags,
                    "unmark_deletion",
                    phrases.UNMARK.format(tag=self.config.tag_deletion),
                )
                continue
            history = await self.wiki.fetch_history(article.page_id)
            tagged_at = self.wiki.tag_added_at(history, self.config.tag_deletion)
            if tagged_at and self._now() - tagged_at >= self.config.critical_delay:
                await self._archive(
                    article,
                    discuss=phrases.deletion_phrase(self._now()),
                    action="archive_critical",
                )
                archived.append(article)
        if archived:
            logger.info("archived_critical count=%s", len(archived))

    async def resolve_whitemark(self) -> None:
        for article in await self._load_cauldron():
            tags = set(article.tags)
            if not has_named_tag(tags, self.config.tag_whitemark):
                continue
            if has_named_tag(tags, self.config.tag_approved):
                continue
            if not self._is_approval(article):
                tags = drop_named_tag(tags, self.config.tag_whitemark)
                await self._save_tags(
                    article,
                    tags,
                    "whitemark_lost",
                    phrases.UNMARK.format(tag=self.config.tag_whitemark),
                )
                continue
            history = await self.wiki.fetch_history(article.page_id)
            tagged_at = self.wiki.tag_added_at(history, self.config.tag_whitemark)
            if tagged_at and self._now() - tagged_at >= self.config.approval_delay:
                tags.add(self.config.tag_approved)
                await self._save_tags(
                    article,
                    tags,
                    "mark_transfer",
                    phrases.TRANSFER_READY,
                )

    async def resolve_transfer(self) -> None:
        """Admins move to main space. Bot only drops к_переносу if rating is lost."""
        for article in await self._load_cauldron():
            tags = set(article.tags)
            if not has_named_tag(tags, self.config.tag_approved):
                continue
            if self._is_approval(article):
                continue
            tags = drop_named_tag(tags, self.config.tag_approved)
            tags = drop_named_tag(tags, self.config.tag_whitemark)
            await self._save_tags(
                article,
                tags,
                "transfer_lost",
                phrases.UNMARK.format(tag=self.config.tag_approved),
            )

    async def resolve_tagging(self) -> None:
        for article in await self._load_cauldron():
            tags = set(article.tags)
            if not has_named_tag(tags, self.config.tag_tagging):
                continue
            if self._is_critical(article):
                tags = drop_named_tag(tags, self.config.tag_tagging)
                tags.add(self.config.tag_deletion)
                await self._save_tags(
                    article,
                    tags,
                    "tagging_to_deletion",
                    phrases.deletion_phrase(self._now()),
                )
                continue
            if self._is_approval(article):
                tags = drop_named_tag(tags, self.config.tag_tagging)
                tags.add(self.config.tag_whitemark)
                await self._save_tags(
                    article,
                    tags,
                    "tagging_to_whitemark",
                    phrases.WHITMARK,
                )
                continue
            if not self._is_grayzone(article):
                tags = drop_named_tag(tags, self.config.tag_tagging)
                await self._save_tags(
                    article,
                    tags,
                    "unmark_tagging",
                    phrases.UNMARK.format(tag=self.config.tag_tagging),
                )
                continue
            history = await self.wiki.fetch_history(article.page_id)
            tagged_at = self.wiki.tag_added_at(history, self.config.tag_tagging)
            if tagged_at and self._now() - tagged_at >= self.config.tagging_delay:
                await self._archive(
                    article,
                    discuss=phrases.grayzone_phrase(article),
                    action="archive_tagging",
                )

    async def stale_check(self) -> None:
        for article in await self._load_cauldron():
            tags = set(article.tags)
            if self._has_decision(tags):
                continue
            history = await self.wiki.fetch_history(article.page_id)
            stamp = (
                self.wiki.last_category_move(history, article.page_id)
                or article.last_edit_at
            )
            if not stamp or self._now() - stamp < self.config.stale_check_delay:
                continue
            tags.add(self.config.tag_tagging)
            await self._save_tags(
                article,
                tags,
                "stale_check",
                phrases.TOO_LONG,
            )

    async def untag_drafts(self) -> None:
        links = await self.wiki.list_category_pages(self.config.draft_categories)
        articles = await self.wiki._get_articles_in_batches(links)
        for article in articles:
            if self._should_skip(article):
                continue
            if not article.tags:
                continue
            await self._save_tags(
                article,
                set(),
                "draft_untag",
                phrases.TAGS_PROHIBITED,
            )

    async def run_cycle(self) -> None:
        await self.ensure_check_tag()
        await self.mark_for()
        await self.resolve_whitemark()
        await self.resolve_transfer()
        await self.resolve_deletion()
        await self.resolve_tagging()
        await self.stale_check()
        await self.untag_drafts()
