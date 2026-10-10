"""Typed data models shared across the pipeline."""

from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    model_validator,
)

Lang = Literal["ja", "en"]
Region = Literal["japan", "world"]
Importance = Literal[1, 2, 3]
Mode = Literal["llm", "fallback"]

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FeedConfig(FrozenModel):
    name: str = Field(min_length=1)
    url: HttpUrl
    lang: Lang
    source: str = Field(min_length=1)
    fallback_region: Region | None = None
    fallback_priority: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _fallback_fields_together(self) -> Self:
        if (self.fallback_region is None) != (self.fallback_priority is None):
            raise ValueError("fallback_region and fallback_priority must be set together")
        return self


class SelectionConfig(FrozenModel):
    japan_max: int = Field(ge=0)
    world_max: int = Field(ge=0)
    window_hours: int = Field(gt=0)
    max_window_hours: int = Field(gt=0)
    max_candidates: int = Field(gt=0)
    per_feed_max: int = Field(gt=0)
    stale_feed_hours: int = Field(gt=0)
    description_max_chars: int = Field(gt=1)


class FiltersConfig(FrozenModel):
    exclude_url_patterns: tuple[str, ...] = ()


class FeedsFile(FrozenModel):
    selection: SelectionConfig
    filters: FiltersConfig = FiltersConfig()
    feeds: tuple[FeedConfig, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_feed_names(self) -> Self:
        names = [feed.name for feed in self.feeds]
        if len(names) != len(set(names)):
            raise ValueError("feed names must be unique")
        return self


class Article(FrozenModel):
    id: str
    feed_name: str
    source: str
    lang: Lang
    title: str
    description: str
    url: str
    published_at: AwareDatetime


class FeedResult(FrozenModel):
    """Outcome of fetching one feed. ``error`` is a secret-free summary (SPEC §9.7)."""

    feed: FeedConfig
    status_code: int | None = None
    articles: tuple[Article, ...] = ()
    latest_at: AwareDatetime | None = None
    stale: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.stale


class Settings(FrozenModel):
    """Environment settings (SPEC §4.1). Secrets are ``SecretStr`` so repr/str never show them."""

    discord_webhook_url: SecretStr
    gemini_api_key: SecretStr | None = None
    gemini_model: str = DEFAULT_GEMINI_MODEL


class Pick(BaseModel):
    """One selected story as returned by Gemini (SPEC §7.4). Lengths are checked in Python."""

    article_id: str
    related_ids: list[str]
    region: Region
    importance: Importance
    headline_ja: str
    summary_ja: str
    reason: str


class Curation(BaseModel):
    picks: list[Pick]


class DigestItem(FrozenModel):
    """One line of the Discord post: an LLM pick or a fallback headline."""

    title: str
    url: str
    source: str
    summary: str | None = None
    importance: Importance | None = None
    related_urls: tuple[str, ...] = ()
    """URLs of the other articles on the same story (recorded as posted in stage 6)."""


class Digest(FrozenModel):
    """What is posted, in display order per region."""

    mode: Mode
    model: str | None = None
    fallback_reason: str | None = None
    japan: tuple[DigestItem, ...] = ()
    world: tuple[DigestItem, ...] = ()
