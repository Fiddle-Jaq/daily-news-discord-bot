from collections.abc import Sequence
from datetime import timedelta
from itertools import count

import pytest
from support import NOW

from daily_news.curation import fallback_digest
from daily_news.filtering import filter_candidates
from daily_news.models import (
    Article,
    DigestItem,
    FeedConfig,
    FiltersConfig,
    Region,
    SelectionConfig,
)

SELECTION = SelectionConfig(
    japan_max=3,
    world_max=2,
    window_hours=30,
    max_window_hours=48,
    max_candidates=120,
    per_feed_max=20,
    stale_feed_hours=72,
    description_max_chars=200,
)


def feed(
    name: str, region: Region | None = None, priority: int | None = None, source: str = "S"
) -> FeedConfig:
    return FeedConfig.model_validate(
        {
            "name": name,
            "url": f"https://{name.lower().replace(' ', '-')}.example/rss",
            "lang": "ja",
            "source": source,
            "fallback_region": region,
            "fallback_priority": priority,
        }
    )


NHK = feed("NHK Top", "japan", 1, source="NHK")
BBC_JA = feed("BBC Japanese", "japan", 2, source="BBC")
BBC_WORLD = feed("BBC World", "world", 1, source="BBC")
GUARDIAN = feed("Guardian", "world", 2, source="Guardian")
NHK_SOCIETY = feed("NHK Society", source="NHK")
FEEDS = (GUARDIAN, BBC_JA, NHK_SOCIETY, BBC_WORLD, NHK)

_ids = count(1)


def article(f: FeedConfig, title: str, *, hours_ago: float = 1, url: str = "") -> Article:
    n = next(_ids)
    return Article(
        id=f"a{n:03d}",
        feed_name=f.name,
        source=f.source,
        lang=f.lang,
        title=title,
        description="",
        url=url or f"https://news.example/{n}",
        published_at=NOW - timedelta(hours=hours_ago),
    )


def titles(items: Sequence[DigestItem]) -> list[str]:
    return [i.title for i in items]


def test_takes_feeds_by_priority_then_newest_first() -> None:
    eligible = [
        article(BBC_JA, "bbc-new", hours_ago=1),
        article(NHK, "nhk-old", hours_ago=5),
        article(NHK, "nhk-new", hours_ago=2),
        article(GUARDIAN, "guardian", hours_ago=1),
        article(BBC_WORLD, "world-old", hours_ago=9),
        article(BBC_WORLD, "world-new", hours_ago=3),
    ]

    digest = fallback_digest(eligible, FEEDS, SELECTION, "reason")

    assert titles(digest.japan) == ["nhk-new", "nhk-old", "bbc-new"]
    assert titles(digest.world) == ["world-new", "world-old"]
    assert digest.mode == "fallback"
    assert digest.model is None
    assert digest.fallback_reason == "reason"


def test_lower_priority_feed_fills_the_rest() -> None:
    eligible = [article(BBC_WORLD, "bbc"), article(GUARDIAN, "g1"), article(GUARDIAN, "g2")]

    digest = fallback_digest(eligible, FEEDS, SELECTION, "r")

    assert titles(digest.world) == ["bbc", "g1"]


def test_feeds_without_fallback_priority_are_not_used() -> None:
    eligible = [article(NHK_SOCIETY, "society")]

    digest = fallback_digest(eligible, FEEDS, SELECTION, "r")

    assert digest.japan == ()
    assert digest.world == ()


def test_items_carry_url_and_source_without_summary() -> None:
    (only,) = fallback_digest(
        [article(NHK, "t", url="https://nhk.example/1")], FEEDS, SELECTION, "r"
    ).japan

    assert only.url == "https://nhk.example/1"
    assert only.source == "NHK"
    assert only.summary is None
    assert only.importance is None


def test_same_url_is_listed_once() -> None:
    url = "https://news.example/same"
    eligible = [article(NHK, "a", url=url), article(BBC_JA, "b", url=url)]

    assert titles(fallback_digest(eligible, FEEDS, SELECTION, "r").japan) == ["a"]


@pytest.mark.parametrize(("length", "expected"), [(100, 100), (101, 100), (250, 100), (5, 5)])
def test_titles_are_truncated_to_100_chars(length: int, expected: int) -> None:
    (only,) = fallback_digest([article(BBC_WORLD, "x" * length)], FEEDS, SELECTION, "r").world

    assert len(only.title) == expected
    assert only.title.endswith("…") == (length > 100)


def test_excluded_and_out_of_window_articles_never_reach_the_fallback() -> None:
    articles = [
        article(BBC_WORLD, "sport", url="https://www.bbc.co.uk/sport/football/1"),
        article(BBC_WORLD, "too old", hours_ago=40),
        article(BBC_WORLD, "posted", url="https://www.bbc.co.uk/news/posted"),
        article(BBC_WORLD, "kept"),
    ]
    filtered = filter_candidates(
        articles,
        now=NOW,
        last_posted_at=None,
        posted_urls=("https://www.bbc.co.uk/news/posted",),
        selection=SELECTION,
        filters=FiltersConfig(exclude_url_patterns=("/sport",)),
    )

    digest = fallback_digest(filtered.eligible, FEEDS, SELECTION, "r")

    assert titles(digest.world) == ["kept"]


def test_logs_a_warning_with_the_reason(caplog: pytest.LogCaptureFixture) -> None:
    fallback_digest([], FEEDS, SELECTION, "--no-llm 指定")

    assert any(
        r.levelname == "WARNING" and "--no-llm 指定" in r.getMessage() for r in caplog.records
    )
