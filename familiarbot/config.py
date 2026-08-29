"""FamiliarBot thresholds and Castopia tag names."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().casefold()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class FamiliarConfig:
    name: str = "FamiliarBot"
    work_period: timedelta = timedelta(minutes=10)
    min_votes: int = 4
    critical_rating: float = 3.0
    approval_rating: float = 4.0
    critical_delay: timedelta = timedelta(days=1)
    approval_delay: timedelta = timedelta(weeks=1)
    tagging_delay: timedelta = timedelta(days=30)
    stale_check_delay: timedelta = timedelta(days=30)
    categories: tuple[str, ...] = ("sandbox",)
    draft_categories: tuple[str, ...] = ("draft",)
    deleted_category: str = "deleted"
    skip_tags: tuple[str, ...] = (
        "основное_пространство",
        "архив",
        "удалено",
    )
    exclude_tags: tuple[str, ...] = (
        "18+",
        "гайд",
        "компонент",
        "навигация",
        "поиск",
        "системный",
        "структура_сайта",
    )
    tag_check: str = "статус:проверка"
    tag_deletion: str = "котел:к_удалению"
    tag_whitemark: str = "котел:рейтинг_набран"
    tag_approved: str = "котел:к_переносу"
    tag_tagging: str = "котел:к_тегованию"
    tag_deleted: str = "статус:удалено"
    tag_main: str = "статус:основное_пространство"
    forum_post_title: str = "Системное уведомление"
    journal_thread_id: int = 100
    journal_path: str = "/forum/t-100/zurnal-udalenii"
    dry_run: bool = field(
        default_factory=lambda: _env_flag("FAMILIARBOT_DRY_RUN", False)
    )
    enabled: bool = field(
        default_factory=lambda: _env_flag("FAMILIARBOT_ENABLED", True)
    )


def load_familiar_config() -> FamiliarConfig:
    return FamiliarConfig()


def tag_leaf(tag: str) -> str:
    value = tag.casefold().strip()
    return value.split(":", 1)[-1] if ":" in value else value


def has_any_tag(tags: Iterable[str], names: tuple[str, ...]) -> bool:
    leaves = {tag_leaf(str(tag)) for tag in tags}
    full = {str(tag).casefold() for tag in tags}
    needles = {name.casefold() for name in names}
    return bool(leaves & needles or full & needles)


def has_named_tag(tags: Iterable[str], name: str) -> bool:
    return has_any_tag(tags, (name,))


def drop_named_tag(tags: Iterable[str], name: str) -> set[str]:
    leaf = tag_leaf(name)
    return {
        str(tag)
        for tag in tags
        if str(tag).casefold() != name.casefold() and tag_leaf(str(tag)) != leaf
    }
