"""Validated shared configuration for the Castopia bots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_WIKI_BASE_URL = "https://castopia.site"
DEFAULT_USER_AGENT = "CastopiaBot/2.0 (community reader; configure WIKI_USER_AGENT)"

# Pages with these tags are hidden from public random and search results.
SYSTEM_TAGS = frozenset(
    {
        "структура:компонент",
        "структура:навигация",
        "структура:поиск",
        "структура:системный",
        "структура:тест",
        "структура:структура_сайта",
        "компонент",
        "навигация",
        "поиск",
        "системный",
        "структура_сайта",
        "гайд",
    }
)

FOOTER_TEXT = "Содержимое распространяется по лицензии CC BY-SA 3.0"

_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 10
_DEFAULT_CONCURRENCY = 2
_MIN_TIMEOUT = 10
_MAX_TIMEOUT = 120
_DEFAULT_TIMEOUT = 45


class ConfigurationError(ValueError):
    """Raised when a required environment configuration value is invalid."""


@dataclass(frozen=True, slots=True)
class WikiConfig:
    """Validated, non-secret settings for the public wiki source."""

    base_url: str
    user_agent: str
    max_concurrent_requests: int
    timeout_seconds: float = 45.0
    username: str = ""
    password: str = ""
    anubis_cookie: str = ""

    @property
    def all_pages_url(self) -> str:
        """Return the configured all-pages endpoint."""
        return f"{self.base_url}/system:all-pages"

    @property
    def tags_url(self) -> str:
        """Return the configured tag catalogue endpoint."""
        return f"{self.base_url}/system:page-tags"

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/-/login"

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)


def _load_https_base_url() -> str:
    """Read and validate the public wiki base URL from the environment."""
    value = os.getenv("WIKI_BASE_URL", DEFAULT_WIKI_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(value)

    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
    ):
        raise ConfigurationError(
            "WIKI_BASE_URL must be an absolute HTTPS URL without path, "
            "query, fragment or embedded credentials, for example "
            "https://castopia.site"
        )

    return value


def _load_concurrency() -> int:
    """Read and validate the maximum number of concurrent wiki requests."""
    raw_value = os.getenv(
        "WIKI_MAX_CONCURRENCY",
        str(_DEFAULT_CONCURRENCY),
    ).strip()

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(
            "WIKI_MAX_CONCURRENCY must be an integer"
        ) from exc

    if not _MIN_CONCURRENCY <= value <= _MAX_CONCURRENCY:
        raise ConfigurationError(
            "WIKI_MAX_CONCURRENCY must be between "
            f"{_MIN_CONCURRENCY} and {_MAX_CONCURRENCY}"
        )

    return value


def _load_timeout() -> float:
    raw_value = os.getenv("WIKI_TIMEOUT", str(_DEFAULT_TIMEOUT)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ConfigurationError("WIKI_TIMEOUT must be a number of seconds") from exc
    if not _MIN_TIMEOUT <= value <= _MAX_TIMEOUT:
        raise ConfigurationError(
            "WIKI_TIMEOUT must be between "
            f"{_MIN_TIMEOUT} and {_MAX_TIMEOUT} seconds"
        )
    return value


def _load_user_agent() -> str:
    """Read and validate the HTTP User-Agent used for public requests."""
    value = os.getenv(
        "WIKI_USER_AGENT",
        DEFAULT_USER_AGENT,
    ).strip()

    if not value:
        raise ConfigurationError("WIKI_USER_AGENT cannot be empty")

    return value


def load_wiki_config() -> WikiConfig:
    """Load, validate and return the public wiki configuration."""
    return WikiConfig(
        base_url=_load_https_base_url(),
        user_agent=_load_user_agent(),
        max_concurrent_requests=_load_concurrency(),
        timeout_seconds=_load_timeout(),
        username=os.getenv("WIKI_USERNAME", "").strip(),
        password=os.getenv("WIKI_PASSWORD", "").strip(),
        anubis_cookie=os.getenv("WIKI_ANUBIS_COOKIE", "").strip(),
    )
